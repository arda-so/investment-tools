#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import socket
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from faster_whisper import WhisperModel

from app.services.earnings_transcript_service import refresh_earnings_transcripts_from_sec
from app.services.ir_audio_ingest_service import discover_latest_ir_audio
from app.services.postgres_core_service import (
    claim_next_earnings_call_ingest_run_pg,
    ensure_earnings_call_artifacts_schema_pg,
    get_ir_source_registry_pg,
    upsert_earnings_transcript_pg,
    upsert_earnings_call_artifact_pg,
    update_earnings_call_ingest_run_status_pg,
)


def _run_sec_refresh(ticker: str) -> dict[str, int | str]:
    return refresh_earnings_transcripts_from_sec(ticker, max_filings=500, lookback_years=10)


def _ir_fallback_enabled() -> bool:
    return str(os.getenv("ENABLE_IR_AUDIO_TRANSCRIBE", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _is_non_retryable_error(err: str) -> bool:
    s = str(err or "").lower()
    needles = (
        "no module named faster_whisper",
        "unsupported_stage",
        "invalid_run_payload",
    )
    return any(n in s for n in needles)


def _guess_audio_ext(audio_url: str) -> str:
    path = str(urllib.parse.urlparse(str(audio_url or "")).path or "").lower()
    for ext in (".mp3", ".wav", ".m4a", ".mp4", ".aac", ".flac", ".ogg"):
        if path.endswith(ext):
            return ext
    return ".mp3"


def _download_audio_file(audio_url: str, out_dir: Path) -> Path:
    ext = _guess_audio_ext(audio_url)
    dst = out_dir / f"input_audio{ext}"
    req = urllib.request.Request(str(audio_url), headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
    if not raw:
        raise RuntimeError("empty_audio_download")
    dst.write_bytes(raw)
    return dst


# Webcast platform event page hostnames — these are JS-rendered player pages, NOT direct audio URLs.
# Never try to direct-download them; they return HTML.
_WEBCAST_PLATFORM_HOSTS = (
    "event.webcasts.com",
    "edge.media-server.com",
    "on24.com",
    "notified.com",
    "viavid.com",
    "chorus.ai",
    "limelight.com",
)

_AUDIO_EXTS = (".m3u8", ".mp3", ".mp4", ".wav", ".aac", ".ogg", ".flac")

# CSS selectors tried in order to find a play button on webcast player pages.
_PLAY_BUTTON_SELECTORS = [
    "button[aria-label*='play' i]",
    "button[title*='play' i]",
    "[class*='play' i][role='button']",
    "[id*='play' i][role='button']",
    "button.play",
    "#play",
    ".play-button",
    "[data-action='play']",
    "button:has-text('Play')",
    "button:has-text('Listen')",
    "button:has-text('Watch')",
]


def _is_webcast_platform_page(url: str) -> bool:
    """True when the URL points to a webcast player page (not a direct audio file)."""
    low = str(url or "").lower()
    if any(ext in low for ext in _AUDIO_EXTS):
        return False  # Direct audio file — not a platform page
    return any(host in low for host in _WEBCAST_PLATFORM_HOSTS)


def _extract_url_via_playwright(url: str) -> str | None:
    """Intercept the actual audio stream URL from a JS-based webcast player page.

    Navigates the page, tries to click a play button, waits for network requests,
    and returns the first m3u8/mp3/mp4 URL captured, or None.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        return None

    captured: list[str] = []

    def _on_request(request: object) -> None:
        req_url = str(getattr(request, "url", "") or "")
        if any(ext in req_url.lower() for ext in _AUDIO_EXTS):
            captured.append(req_url)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.on("request", _on_request)
            try:
                page.goto(url, timeout=45000, wait_until="networkidle")
            except Exception:
                pass
            # Try clicking a play button to trigger stream loading.
            for selector in _PLAY_BUTTON_SELECTORS:
                try:
                    btn = page.query_selector(selector)
                    if btn:
                        btn.click()
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue
            # Wait for stream URLs to appear in network requests.
            page.wait_for_timeout(12000)
            browser.close()
    except Exception:
        return None

    # Prefer m3u8 (HLS stream) > mp3 > mp4 > wav
    for ext in (".m3u8", ".mp3", ".mp4", ".wav"):
        for u in captured:
            if ext in u.lower():
                return u
    return captured[0] if captured else None


def _resolve_audio_input(audio_url: str, tmp_dir: Path) -> str:
    """Return a local file path or stream URL ready for faster-whisper.

    Priority: HLS passthrough → yt-dlp → Playwright (JS webcast platforms) → direct download.
    Raises RuntimeError for webcast platform pages when all methods fail (avoids downloading HTML).
    """
    if ".m3u8" in str(audio_url or "").lower():
        return str(audio_url)
    is_platform_page = _is_webcast_platform_page(audio_url)
    try:
        import yt_dlp  # noqa: PLC0415
        with yt_dlp.YoutubeDL({
            "format": "bestaudio/best",
            "outtmpl": str(tmp_dir / "input_ytdlp.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
        }) as ydl:
            ydl.download([audio_url])
        for f in sorted(tmp_dir.glob("input_ytdlp.*")):
            if f.stat().st_size > 4096:
                return str(f)
    except Exception:
        pass
    stream_url = _extract_url_via_playwright(audio_url)
    if stream_url:
        return stream_url
    if is_platform_page:
        # Never download a webcast platform player page — it would just be HTML.
        raise RuntimeError(f"webcast_stream_not_captured: {audio_url}")
    return str(_download_audio_file(audio_url, tmp_dir))


def _transcribe_audio_with_whisper(*, audio_url: str, ticker: str, run_id: int) -> dict[str, Any]:
    model = str(os.getenv("WHISPER_MODEL", "small")).strip() or "small"
    language = str(os.getenv("WHISPER_LANGUAGE", "en")).strip() or "en"
    compute_type = str(os.getenv("WHISPER_COMPUTE_TYPE", "int8")).strip() or "int8"
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"earnings_whisper_{str(ticker).lower()}_{int(run_id)}_"))
    try:
        input_arg = _resolve_audio_input(audio_url, tmp_dir)
        model_obj = WhisperModel(model, device="cpu", compute_type=compute_type)
        seg_iter, info = model_obj.transcribe(input_arg, language=language, vad_filter=True)
        segments = list(seg_iter)
        txt = " ".join((str(s.text or "").strip() for s in segments if str(s.text or "").strip())).strip()
        if not txt:
            raise RuntimeError("whisper_empty_transcript")
        avg_lp = 0.0
        if segments:
            avg_lp = sum(float(getattr(s, "avg_logprob", -1.0) or -1.0) for s in segments) / float(len(segments))
        conf = max(0.0, min(1.0, (avg_lp + 1.5) / 1.5))
        return {
            "ok": True,
            "transcript_text": txt,
            "char_count": len(txt),
            "confidence": float(conf),
            "whisper_model": f"{model}:{compute_type}",
            "language_detected": str(getattr(info, "language", "") or ""),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:2000]}
    finally:
        # Keep temp files only if explicitly requested for debugging.
        if str(os.getenv("WHISPER_KEEP_TMP", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
            try:
                for p in tmp_dir.glob("*"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                tmp_dir.rmdir()
            except Exception:
                pass


def _run_ir_audio_discovery(ticker: str, idempotency_key: str) -> dict[str, Any]:
    reg = get_ir_source_registry_pg(ticker)
    out = discover_latest_ir_audio(
        ticker=ticker,
        ir_home_url=str((reg or {}).get("ir_home_url") or ""),
        rss_url=str((reg or {}).get("rss_url") or ""),
        last_good_event_url=str((reg or {}).get("last_good_event_url") or ""),
        last_good_audio_url=str((reg or {}).get("last_good_audio_pattern") or ""),
    )
    if not bool(out.get("ok")):
        return {"ok": False, "error": str(out.get("error") or "audio_not_found"), "registry_present": bool(reg)}
    best = dict(out.get("best") or {})
    audio_url = str(best.get("audio_url") or "").strip()
    if not audio_url:
        return {"ok": False, "error": "audio_not_found", "registry_present": bool(reg)}
    event_dt = str(best.get("event_datetime") or "").strip()[:40] or dt.datetime.now().isoformat()
    title = str(best.get("title") or f"{ticker} earnings call")[:320]
    src_url = str(best.get("event_url") or "")[:2000]
    checksum = hashlib.sha256(audio_url.encode("utf-8", errors="ignore")).hexdigest()[:64]
    key = str(idempotency_key or "").strip()[:200] or f"{str(ticker).upper()}:{event_dt}:{checksum}"
    ok = upsert_earnings_call_artifact_pg(
        ticker=ticker,
        event_datetime=event_dt,
        title=title,
        audio_uri=audio_url,
        audio_source_url=src_url,
        audio_checksum=checksum,
        transcript_text="",
        transcript_source="pending_whisper_local",
        transcript_confidence=0.0,
        ingest_status="audio_discovered",
        idempotency_key=key,
        meta={
            "discovery_source": str(best.get("source") or ""),
            "candidates": list(out.get("candidates") or [])[:12],
            "crawled_urls": list(out.get("crawled_urls") or [])[:12],
        },
    )
    if not ok:
        return {"ok": False, "error": "artifact_upsert_failed"}
    return {
        "ok": True,
        "audio_url": audio_url,
        "event_datetime": event_dt,
        "title": title,
        "audio_source_url": src_url,
        "idempotency_key": key,
        "source": str(best.get("source") or ""),
        "candidates": int(len(list(out.get("candidates") or []))),
    }


def _run_ir_audio_transcribe(ticker: str, idempotency_key: str, run_id: int) -> dict[str, Any]:
    disc = _run_ir_audio_discovery(ticker, idempotency_key)
    if not bool(disc.get("ok")):
        return disc
    audio_url = str(disc.get("audio_url") or "").strip()
    if not audio_url:
        return {"ok": False, "error": "audio_not_found"}
    tr = _transcribe_audio_with_whisper(audio_url=audio_url, ticker=ticker, run_id=run_id)
    if not bool(tr.get("ok")):
        return {"ok": False, "error": str(tr.get("error") or "whisper_failed")}
    txt = str(tr.get("transcript_text") or "")
    conf = float(tr.get("confidence") or 0.0)
    min_chars = max(200, int(float(os.getenv("EARNINGS_MIN_TRANSCRIPT_CHARS", "1200"))))
    min_conf = max(0.0, min(1.0, float(os.getenv("EARNINGS_MIN_TRANSCRIPT_CONF", "0.55"))))
    quality_ok = bool(len(txt) >= min_chars and conf >= min_conf)
    event_dt = str(disc.get("event_datetime") or "")[:40]
    title = str(disc.get("title") or f"{ticker} earnings call")[:320]
    checksum = hashlib.sha256(audio_url.encode("utf-8", errors="ignore")).hexdigest()[:64]
    key = str(disc.get("idempotency_key") or idempotency_key or f"{str(ticker).upper()}:{event_dt}:{checksum}")[:200]
    ok_art = upsert_earnings_call_artifact_pg(
        ticker=ticker,
        event_datetime=event_dt,
        title=title,
        audio_uri=audio_url,
        audio_source_url=str(disc.get("audio_source_url") or ""),
        audio_checksum=checksum,
        transcript_text=txt,
        transcript_source="whisper_local",
        transcript_confidence=conf,
        ingest_status=("ready" if quality_ok else "needs_review"),
        idempotency_key=key,
        meta={
            "discovery_source": str(disc.get("source") or ""),
            "whisper_model": str(tr.get("whisper_model") or ""),
            "quality_gate": {
                "min_chars": min_chars,
                "min_conf": min_conf,
                "chars": len(txt),
                "conf": conf,
                "passed": quality_ok,
            },
        },
    )
    if not ok_art:
        return {"ok": False, "error": "artifact_upsert_failed"}
    # Also persist into existing transcript table so current downstream logic continues to work.
    _ = upsert_earnings_transcript_pg(
        ticker=ticker,
        call_date=str(event_dt or "")[:10],
        fiscal_year=None,
        fiscal_quarter="",
        title=title,
        source_type="ir_audio_whisper",
        source_url=str(audio_url or "")[:1200],
        filing_id=0,
        accession="",
        excerpt=txt[:700],
        transcript_text=txt,
        char_count=int(len(txt)),
        quality_score=float(conf),
        is_partial=bool((len(txt) < 2200) or (not quality_ok)),
    )
    return {
        "ok": True,
        "audio_url": audio_url,
        "char_count": int(len(txt)),
        "confidence": conf,
        "quality_ok": quality_ok,
        "ingest_status": ("ready" if quality_ok else "needs_review"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Background worker for earnings call ingest runs.")
    p.add_argument("--once", action="store_true", help="Process at most one queued run, then exit.")
    p.add_argument("--poll-sec", type=float, default=float(os.getenv("EARNINGS_INGEST_POLL_SEC", "1.0")))
    args = p.parse_args()

    ensure_earnings_call_artifacts_schema_pg()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    poll_sec = max(0.2, min(10.0, float(args.poll_sec or 1.0)))
    print(f"[earnings-ingest-worker] started worker_id={worker_id} poll_sec={poll_sec}")

    while True:
        run = claim_next_earnings_call_ingest_run_pg(worker_id=worker_id)
        if not isinstance(run, dict):
            if args.once:
                print("[earnings-ingest-worker] no queued jobs; exiting (--once)")
                return 0
            time.sleep(poll_sec)
            continue

        rid = int(run.get("id") or 0)
        ticker = str(run.get("ticker") or "").strip().upper()
        stage = str(run.get("stage") or "sec_refresh").strip().lower()
        started = dt.datetime.now().isoformat()
        print(f"[earnings-ingest-worker] claimed run_id={rid} ticker={ticker} stage={stage}")
        if rid <= 0 or not ticker:
            if rid > 0:
                update_earnings_call_ingest_run_status_pg(
                    run_id=rid,
                    status="failed",
                    error_text="invalid_run_payload",
                    detail={"worker_id": worker_id, "started_at": started},
                )
            if args.once:
                return 1
            continue

        try:
            if stage in {"sec_refresh", "transcript_refresh"}:
                out = _run_sec_refresh(ticker)
                ok = bool(int(out.get("ok") or 0))
                saved = int(out.get("saved") or 0)
                # If SEC text extraction found nothing, immediately try IR audio->text fallback.
                if ok and saved == 0 and _ir_fallback_enabled():
                    fb = _run_ir_audio_transcribe(ticker, str(run.get("idempotency_key") or ""), rid)
                    out = dict(out)
                    out["ir_fallback"] = fb
                    if bool(fb.get("ok")):
                        out["saved"] = 1
            elif stage in {"ir_audio_transcribe", "audio_transcribe"}:
                out = _run_ir_audio_transcribe(ticker, str(run.get("idempotency_key") or ""), rid)
                ok = bool(out.get("ok"))
            elif stage in {"ir_audio_discovery", "audio_discovery"}:
                out = _run_ir_audio_discovery(ticker, str(run.get("idempotency_key") or ""))
                ok = bool(out.get("ok"))
            else:
                raise RuntimeError(f"unsupported_stage:{stage}")
            if ok:
                if stage in {"sec_refresh", "transcript_refresh"}:
                    fb = out.get("ir_fallback") if isinstance(out, dict) else None
                    if isinstance(fb, dict):
                        print(
                            f"[earnings-ingest-worker] done run_id={rid} ticker={ticker} "
                            f"saved={int(out.get('saved') or 0)} "
                            f"ir_fallback_ok={1 if bool(fb.get('ok')) else 0}"
                        )
                    else:
                        print(f"[earnings-ingest-worker] done run_id={rid} ticker={ticker} saved={int(out.get('saved') or 0)}")
                elif stage in {"ir_audio_transcribe", "audio_transcribe"}:
                    print(f"[earnings-ingest-worker] done run_id={rid} ticker={ticker} transcript_chars={int(out.get('char_count') or 0)}")
                else:
                    print(f"[earnings-ingest-worker] done run_id={rid} ticker={ticker} audio={str(out.get('audio_url') or '')[:120]}")
                update_earnings_call_ingest_run_status_pg(
                    run_id=rid,
                    status="done",
                    error_text="",
                    detail={
                        "worker_id": worker_id,
                        "started_at": started,
                        "finished_at": dt.datetime.now().isoformat(),
                        "result": out,
                    },
                )
            else:
                raise RuntimeError(str(out.get("error") or "sec_refresh_failed"))
        except Exception as exc:
            max_retry = max(0, int(float(os.getenv("EARNINGS_INGEST_MAX_RETRY", "3"))))
            attempt = int(run.get("attempt") or 0)
            retry_delay_sec = min(3600, max(10, (2 ** max(0, attempt - 1)) * 30))
            should_retry = (attempt < max_retry) and (not _is_non_retryable_error(str(exc)))
            next_retry_at = (dt.datetime.now() + dt.timedelta(seconds=retry_delay_sec)).isoformat() if should_retry else ""
            next_status = "retry" if should_retry else "failed"
            print(f"[earnings-ingest-worker] failed run_id={rid} ticker={ticker} error={str(exc)}")
            update_earnings_call_ingest_run_status_pg(
                run_id=rid,
                status=next_status,
                error_text=str(exc)[:4000],
                detail={
                    "worker_id": worker_id,
                    "started_at": started,
                    "finished_at": dt.datetime.now().isoformat(),
                    "attempt": attempt,
                    "max_retry": max_retry,
                    "retry_delay_sec": retry_delay_sec,
                },
                next_retry_at=next_retry_at,
            )

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
