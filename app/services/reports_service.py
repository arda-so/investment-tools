from __future__ import annotations

import datetime as dt
import json
import re

from app.core import cloud_files
from app.core.config import ROOT
from app.services.portfolio_state_service import read_portfolio_rows_state, read_watchlist_rows_state


REPORTS_DIR = ROOT / "reports"
READ_STATE_PATH = ROOT / "data" / "reports_read_state.json"


def _kind_for_name(name: str) -> str:
    n = str(name or "").lower()
    if n.startswith("terminal_daily_brief_") or n.startswith("daily_brief_"):
        return "Daily Brief"
    if n.startswith("terminal_appendix_"):
        return "Appendix"
    if n.startswith("morning_intelligence_"):
        return "Morning Intelligence"
    if n.startswith("signal_tracker_"):
        return "Signal Tracker"
    if n.startswith("earnings_radar_"):
        return "Earnings Radar"
    if n.startswith("market_scanner_"):
        return "Market Scanner"
    if n.startswith("weekly_report_") or n.startswith("terminal_weekly_"):
        return "Weekly"
    if n.startswith("terminal_monthly_ic_memo_") or n.startswith("monthly_report_"):
        return "Monthly"
    if n.startswith("quarterly_report_"):
        return "Quarterly"
    if n.startswith("l2_digest_"):
        return "L2 Digest"
    if n.startswith("deep_dive_"):
        return "Deep Dive"
    if n.startswith("red_flag_alert_"):
        return "Red Flag Alert"
    return "Other"


def _fmt_date(tag: str) -> str:
    s = str(tag or "").strip()
    try:
        if re.match(r"^\d{8}$", s):
            d = dt.datetime.strptime(s, "%Y%m%d")
            return d.strftime("%b %d, %Y")
        if re.match(r"^\d{8}_\d{4}$", s):
            d = dt.datetime.strptime(s, "%Y%m%d_%H%M")
            return d.strftime("%b %d, %Y • %H:%M")
    except Exception:
        pass
    return s or "-"


def _display_title(name: str, kind: str) -> str:
    n = str(name or "").strip()
    k = str(kind or "Report").strip()
    # Daily / appendix / weekly / monthly / quarterly
    m = re.search(r"_(\d{8})(?:\D|$)", n)
    date_tag = m.group(1) if m else ""
    # L2 with timestamp.
    m2 = re.search(r"_(\d{8}_\d{4})(?:\D|$)", n)
    if m2:
        return f"{k} — {_fmt_date(m2.group(1))}"
    if date_tag:
        return f"{k} — {_fmt_date(date_tag)}"
    # Deep dives: Deep Dive — TICKER • Date
    md = re.match(r"^deep_dive_([A-Za-z0-9.\-]+)_(\d{8})(?:_(\d{4}))?", n)
    if md:
        tk = str(md.group(1) or "").upper()
        d = str(md.group(2) or "")
        tm = str(md.group(3) or "")
        tag = d + ("_" + tm if tm else "")
        return f"Deep Dive — {tk} • {_fmt_date(tag)}"
    return k


def _load_read_state() -> dict[str, str]:
    try:
        raw_txt = cloud_files.read_text("data/reports_read_state.json")
        if not raw_txt:
            return {}
        obj = json.loads(raw_txt)
        if not isinstance(obj, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in obj.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip()
            if kk:
                out[kk] = vv
        return out
    except Exception:
        return {}


def _write_read_state(data: dict[str, str]) -> None:
    cloud_files.write_text("data/reports_read_state.json", json.dumps(data, ensure_ascii=True, indent=2))


def mark_report_read(name: str) -> None:
    n = str(name or "").strip()
    if not n:
        return
    st = _load_read_state()
    st[n] = dt.datetime.now().isoformat()
    _write_read_state(st)


def mark_reports_read(names: list[str]) -> int:
    clean = [str(x or "").strip() for x in (names or []) if str(x or "").strip()]
    if not clean:
        return 0
    st = _load_read_state()
    now = dt.datetime.now().isoformat()
    cnt = 0
    for n in clean:
        st[n] = now
        cnt += 1
    _write_read_state(st)
    return cnt


def _portfolio_watchlist_tickers() -> tuple[set[str], set[str]]:
    pset: set[str] = set()
    wset: set[str] = set()
    for r in read_portfolio_rows_state():
        t = re.sub(r"[^A-Z0-9.\-]", "", str(r.get("ticker") or "").strip().upper())
        if t:
            pset.add(t)
    for r in read_watchlist_rows_state():
        t = re.sub(r"[^A-Z0-9.\-]", "", str(r.get("ticker") or "").strip().upper())
        if t:
            wset.add(t)
    return pset, wset


def _read_report_text(name: str, max_chars: int = 220_000) -> str:
    n = str(name or "").strip()
    if not n:
        return ""
    txt = cloud_files.read_text(f"reports/{n}")
    return txt[:max_chars]


def _extract_headlines(txt: str, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ln in str(txt or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        elif s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        else:
            if not re.search(r"\b(risk|guidance|margin|debt|downgrade|upgrade|beat|miss|warning|alert)\b", s, flags=re.I):
                continue
        if len(s) < 12:
            continue
        key = " ".join(s.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:220])
        if len(out) >= max(1, int(limit)):
            break
    return out


def _brief_points_from_text(text: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        else:
            continue
        if len(s) < 8:
            continue
        key = " ".join(s.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:220])
        if len(out) >= max(1, int(limit)):
            break
    return out


def _score_report(name: str, kind: str, modified: str, pset: set[str], wset: set[str]) -> tuple[int, str, list[str]]:
    txt = _read_report_text(name, max_chars=140_000)
    up = txt.upper()
    p_hits = sorted({t for t in pset if t and re.search(rf"\b{re.escape(t)}\b", up)})
    w_hits = sorted({t for t in wset if t and re.search(rf"\b{re.escape(t)}\b", up)})
    score = 0
    reasons: list[str] = []
    if p_hits:
        score += 4
        reasons.append("portfolio mention")
    if w_hits:
        score += 2
        reasons.append("watchlist mention")
    if kind in {"Daily Brief", "Appendix"}:
        score += 2
    elif kind in {"Weekly", "Monthly", "Quarterly"}:
        score += 1
    if re.search(r"\b(risk|warning|guidance|downgrade|debt|liquidity|declines?)\b", txt, flags=re.I):
        score += 2
        reasons.append("risk keywords")
    if re.search(r"\b(beat|upside|improvement|upgrade)\b", txt, flags=re.I):
        score += 1
    return score, ", ".join(reasons[:3]) or "fresh report", (p_hits + w_hits)[:8]


def list_reports(limit: int = 300) -> list[dict[str, str]]:
    read_state = _load_read_state()
    pset, wset = _portfolio_watchlist_tickers()
    out: list[dict[str, str]] = []
    files = cloud_files.list_files("reports", suffixes={".md", ".txt", ".json", ".html"})
    for f in files[: max(1, min(2000, int(limit)))]:
        kind = _kind_for_name(f.name)
        modified = f.modified.strftime("%Y-%m-%d %H:%M:%S")
        score, reason, hits = _score_report(
            name=f.name,
            kind=kind,
            modified=modified,
            pset=pset,
            wset=wset,
        )
        out.append(
            {
                "name": f.name,
                "kind": kind,
                "title": _display_title(f.name, kind),
                "size_kb": f"{float(f.size)/1024.0:.1f}",
                "modified": modified,
                "unread": "1" if not read_state.get(f.name) else "0",
                "priority_score": str(score),
                "priority_reason": reason,
                "ticker_hits": ", ".join(hits),
            }
        )
    return out


def latest_by_kind(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for r in rows:
        k = str(r.get("kind") or "Other")
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _latest_for(rows: list[dict[str, str]], kind: str) -> dict[str, str] | None:
    for r in rows:
        if str(r.get("kind") or "") == kind:
            return r
    return None


def report_intelligence(rows: list[dict[str, str]], allow_ai: bool = False) -> dict[str, object]:
    # Build from latest key reports.
    latest_daily = _latest_for(rows, "Daily Brief")
    latest_appendix = _latest_for(rows, "Appendix")
    latest_l2 = _latest_for(rows, "L2 Digest")
    latest_weekly = _latest_for(rows, "Weekly")
    sources = [x for x in [latest_daily, latest_appendix, latest_l2, latest_weekly] if x]

    headline_rows: list[dict[str, str]] = []
    for src in sources:
        nm = str(src.get("name") or "")
        kd = str(src.get("kind") or "")
        txt = _read_report_text(nm, max_chars=180_000)
        for h in _extract_headlines(txt, limit=6):
            headline_rows.append({"file": nm, "kind": kd, "headline": h})
            if len(headline_rows) >= 20:
                break
        if len(headline_rows) >= 20:
            break

    must_read: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        n = str(r.get("name") or "")
        k = str(r.get("kind") or "")
        if k not in {"Daily Brief", "Appendix", "L2 Digest", "Weekly", "Monthly", "Quarterly"}:
            continue
        key = n
        if key in seen:
            continue
        seen.add(key)
        must_read.append(
            {
                "name": n,
                "title": str(r.get("title") or n),
                "kind": k,
                "reason": str(r.get("priority_reason") or "important update"),
                "hits": str(r.get("ticker_hits") or ""),
                "score": str(r.get("priority_score") or "0"),
                "modified": str(r.get("modified") or ""),
                "unread": str(r.get("unread") or "0"),
            }
        )
    must_read.sort(key=lambda x: (int(x.get("score") or "0"), x.get("modified") or ""), reverse=True)
    must_read = must_read[:8]

    queue: list[dict[str, str]] = []
    for r in must_read:
        score = int(r.get("score") or "0")
        pri = "High" if score >= 6 else ("Medium" if score >= 3 else "Low")
        if pri == "Low":
            continue
        queue.append(
            {
                "priority": pri,
                "name": r["name"],
                "title": str(r.get("title") or r["name"]),
                "kind": r["kind"],
                "reason": r["reason"],
                "hits": r["hits"],
                "unread": r["unread"],
            }
        )
    queue.sort(key=lambda x: (0 if x["priority"] == "High" else 1, x["name"]))
    queue = queue[:12]

    def _parse_mod(s: str) -> dt.datetime:
        try:
            return dt.datetime.strptime(str(s or ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return dt.datetime.fromtimestamp(0)

    cutoff = dt.datetime.now() - dt.timedelta(days=1)
    new_items: list[dict[str, str]] = []
    for r in rows:
        mod = _parse_mod(str(r.get("modified") or ""))
        if mod < cutoff:
            continue
        score = int(str(r.get("priority_score") or "0") or "0")
        new_items.append(
            {
                "name": str(r.get("name") or ""),
                "title": str(r.get("title") or r.get("name") or ""),
                "kind": str(r.get("kind") or ""),
                "modified": str(r.get("modified") or ""),
                "reason": str(r.get("priority_reason") or ""),
                "hits": str(r.get("ticker_hits") or ""),
                "unread": str(r.get("unread") or "0"),
                "score": str(score),
            }
        )
    new_items.sort(key=lambda x: (x.get("unread") == "1", int(x.get("score") or "0"), x.get("modified") or ""), reverse=True)
    new_items = new_items[:16]

    ai_brief = ""
    if bool(allow_ai):
        try:
            from tools.llm_engine import ask_ai  # type: ignore

            if sources and headline_rows:
                lines = [f"- [{h['kind']}] {h['headline']}" for h in headline_rows[:12]]
                prompt = (
                    "Create a concise investor triage brief from report headlines.\n"
                    "Output:\n"
                    "1) Top 5 must-read items\n"
                    "2) What could be missed today\n"
                    "3) Suggested read order (max 5)\n\n"
                    f"Headlines:\n{chr(10).join(lines)}\n"
                )
                ai_brief = str(ask_ai(prompt, "You are an investment research chief of staff.", mode="smart") or "").strip()
        except Exception:
            ai_brief = ""

    if not ai_brief:
        bullets = [f"- [{h['kind']}] {h['headline']}" for h in headline_rows[:8]]
        ai_brief = (
            "Top items to review now:\n"
            + ("\n".join(bullets) if bullets else "- No strong headlines extracted yet.")
        )

    brief_points = _brief_points_from_text(ai_brief, limit=8)
    if not brief_points:
        brief_points = [str(h.get("headline") or "") for h in headline_rows[:8] if str(h.get("headline") or "").strip()]

    read_now = [x for x in queue if str(x.get("priority") or "") == "High"][:6]
    read_later = [x for x in queue if str(x.get("priority") or "") != "High"][:8]

    return {
        "brief": ai_brief,
        "brief_points": brief_points,
        "must_read": must_read,
        "queue": queue,
        "read_now": read_now,
        "read_later": read_later,
        "headlines": headline_rows,
        "new_items": new_items,
    }


def read_report_file(name: str, max_chars: int = 500_000) -> tuple[str, str]:
    n = str(name or "").strip()
    if not n:
        return "", "Report name is required."
    if "/" in n or "\\" in n or n.startswith("."):
        return "", "Invalid report name."
    if not cloud_files.exists(f"reports/{n}"):
        return "", "Report not found."
    try:
        txt = cloud_files.read_text(f"reports/{n}")
    except Exception as e:
        return "", f"Could not read report: {str(e)[:120]}"
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "\n\n[Truncated for viewer]"
    return txt, ""
