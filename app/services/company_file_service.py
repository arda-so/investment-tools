from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import DATA_DIR, ROOT
from app.core.date import parse_datetime_flexible
from app.core.filing_text import resolve_filing_path, normalize_filing_rel_path, filing_path_available, read_filing_text_any
from app.core import cloud_files
from app.core.proposal_text import clean_task_text, is_ai_task_text
from app.core.ticker import normalize_ticker as _normalize_ticker
from app.services.postgres_core_service import (
    add_company_reminder_pg,
    add_todo_pg,
    add_workspace_journal_note_pg,
    core_backend,
    delete_company_reminder_pg,
    delete_todo_pg,
    delete_workspace_journal_note_pg,
    list_action_proposals_pg,
    list_company_reminders_pg,
    list_todos_pg,
    list_recent_notes_pg,
    pg_connect,
    toggle_company_reminder_pg,
    toggle_todo_pg,
    update_company_reminder_pg,
    update_todo_pg,
    update_workspace_journal_note_pg,
)
from app.services.price_metrics_service import get_price_metrics
from app.services.mini_statements_service import get_mini_statements, compute_financial_deltas
from app.services.company_intel_service import get_company_intel
from app.services.earnings_transcript_service import (
    list_earnings_analysis,
    list_earnings_transcripts,
    list_quarterly_result_signals,
    list_sec_earnings_releases,
)
from app.services.company_lookup_service import market_cap_map, prefetch_market_cap_async


INDEX_FILES: list[tuple[str, str, str]] = [
    ("sp500", "S&P 500", "sp500.txt"),
    ("russell1000", "Russell 1000", "russell1000.txt"),
    ("russell2000", "Russell 2000", "russell2000.txt"),
    ("ftse100", "FTSE 100", "ftse100.txt"),
    ("dax40", "DAX 40", "dax40.txt"),
    ("cac40", "CAC 40", "cac40.txt"),
    ("kospi200", "KOSPI 200", "kospi200.txt"),
    ("tsx60", "S&P/TSX 60", "tsx60.txt"),
    ("asx200", "S&P/ASX 200", "asx200.txt"),
]

MOAT_OPTIONS: list[tuple[str, str]] = [
    ("network_effect", "Network Effect"),
    ("switching_costs", "Switching Costs"),
    ("cost_advantage", "Cost Advantage"),
    ("intangible_assets", "Intangible Assets"),
    ("efficient_scale", "Efficient Scale"),
    ("brand", "Brand"),
    ("distribution", "Distribution"),
    ("regulatory_license", "Regulatory / License"),
    ("ip_patents", "IP / Patents"),
    ("ecosystem_lockin", "Ecosystem Lock-in"),
]

FILING_CATEGORY_ORDER: list[tuple[str, str]] = [
    ("annual", "Annual Reports"),
    ("quarterly", "Quarterly Reports"),
    ("proxy", "Proxy / Shareholder"),
    ("current", "Current Reports"),
    ("ownership", "Ownership / Beneficial"),
    ("other", "Other Filings"),
]

# Counterparty role is from the anchor ticker perspective.
# Example: ("NVDA", "TSM", "supplier", ...) means TSM supplies NVDA.
SUPPLY_CHAIN_SEED_RELATIONSHIPS: list[tuple[str, str, str, str, float]] = [
    ("NVDA", "TSM", "supplier", "Advanced wafer foundry partner for flagship GPUs.", 0.95),
    ("NVDA", "ASML", "supplier", "Lithography equipment provider critical to advanced node capacity.", 0.74),
    ("NVDA", "AMAT", "supplier", "Semiconductor equipment exposure via advanced packaging and process tools.", 0.66),
    ("NVDA", "KLAC", "supplier", "Process control/metrology tooling in leading-edge chip manufacturing.", 0.64),
    ("NVDA", "LRCX", "supplier", "Etch/deposition equipment supplier in fabrication stack.", 0.63),
    ("NVDA", "VRT", "supplier", "Power/cooling infrastructure tied to AI datacenter buildouts.", 0.72),
    ("NVDA", "COHR", "supplier", "Optical components exposure for AI network interconnect demand.", 0.71),
    ("NVDA", "ANET", "partner", "AI networking partner in hyperscale cluster deployments.", 0.68),
    ("AAPL", "TSM", "supplier", "Primary advanced-node manufacturing partner for key SoCs.", 0.95),
    ("AAPL", "QCOM", "supplier", "Modem and connectivity silicon supplier.", 0.82),
    ("AAPL", "SWKS", "supplier", "RF front-end component supplier for wireless devices.", 0.76),
    ("AAPL", "QRVO", "supplier", "RF component supplier with handset exposure.", 0.74),
    ("AAPL", "AVGO", "supplier", "Custom connectivity and RF chips supplier.", 0.78),
    ("AAPL", "GLW", "supplier", "Specialty glass/materials supplier exposure.", 0.69),
    ("MSFT", "NVDA", "supplier", "AI accelerator supplier for cloud AI workloads.", 0.88),
    ("GOOGL", "NVDA", "supplier", "AI accelerator supplier for hyperscale compute.", 0.84),
    ("AMZN", "NVDA", "supplier", "AI accelerator supplier for cloud capacity expansion.", 0.84),
    ("META", "NVDA", "supplier", "AI accelerator supplier for large training clusters.", 0.83),
    ("TSLA", "ON", "supplier", "Power semiconductor supplier exposure for EV systems.", 0.74),
    ("TSLA", "STM", "supplier", "Semiconductor and control component supplier exposure.", 0.72),
    ("LLY", "DHR", "supplier", "Life-science tools exposure through bioprocess instrumentation.", 0.64),
    ("LLY", "TMO", "supplier", "Bioprocess and analytical tools supplier exposure.", 0.66),
]

_LOOKUP_CACHE_LOCK = threading.Lock()
_LOOKUP_CACHE_TTL_SEC = 45.0
_PROFILES_CACHE: tuple[float, dict[str, dict[str, str]]] = (0.0, {})
_NAMES_CACHE: tuple[float, dict[str, str]] = (0.0, {})
_UNIVERSE_CACHE_TTL_SEC = 45.0
_UNIVERSE_ALL_CACHE: tuple[float, list[str]] = (0.0, [])
_UNIVERSE_REG_CACHE: dict[str, tuple[float, list[str]]] = {}
_SUPPLY_LINK_ROLES = {"supplier", "customer", "partner"}
_PROFILE_PREFETCH_LOCK = threading.Lock()
_PROFILE_PREFETCH_INFLIGHT: set[str] = set()


def _fetch_profile_live_one(ticker: str) -> dict[str, str]:
    tk = _normalize_ticker(ticker)
    if not tk:
        return {}
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}
    try:
        obj = yf.Ticker(tk)
        info = obj.info or {}
        name = str(info.get("longName") or info.get("shortName") or "").strip()
        country = str(info.get("country") or "").strip()
        industry = str(info.get("industry") or "").strip()
        sector = str(info.get("sector") or "").strip()
        if not (name or country or industry or sector):
            return {}
        return {
            "ticker": tk,
            "name": name,
            "country": country,
            "industry": industry,
            "sector": sector,
            "updated_at": dt.datetime.now().isoformat(),
        }
    except Exception:
        return {}


def _upsert_profile_cache_rows(rows: list[dict[str, str]]) -> None:
    clean = [r for r in rows if str(r.get("ticker") or "").strip()]
    if not clean:
        return
    con_pg = pg_connect()
    if con_pg is None:
        return
    try:
        for r in clean:
            cur = con_pg.cursor()
            cur.execute(
                """
                INSERT INTO company_profile_cache_core (ticker, name, country, industry, sector, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(ticker) DO UPDATE SET
                  name=CASE WHEN trim(COALESCE(EXCLUDED.name,''))<>'' THEN EXCLUDED.name ELSE company_profile_cache_core.name END,
                  country=CASE WHEN trim(COALESCE(EXCLUDED.country,''))<>'' THEN EXCLUDED.country ELSE company_profile_cache_core.country END,
                  industry=CASE WHEN trim(COALESCE(EXCLUDED.industry,''))<>'' THEN EXCLUDED.industry ELSE company_profile_cache_core.industry END,
                  sector=CASE WHEN trim(COALESCE(EXCLUDED.sector,''))<>'' THEN EXCLUDED.sector ELSE company_profile_cache_core.sector END,
                  updated_at=EXCLUDED.updated_at
                """,
                (
                    str(r.get("ticker") or ""),
                    str(r.get("name") or ""),
                    str(r.get("country") or ""),
                    str(r.get("industry") or ""),
                    str(r.get("sector") or ""),
                    str(r.get("updated_at") or dt.datetime.now().isoformat()),
                ),
            )
        con_pg.commit()
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
    finally:
        con_pg.close()


def prefetch_company_profiles_async(tickers: list[str] | None = None, *, limit: int = 40) -> None:
    wanted = sorted({_normalize_ticker(t) for t in (tickers or []) if _normalize_ticker(t)})
    if not wanted:
        return
    wanted = wanted[: max(1, min(120, int(limit or 40)))]
    with _PROFILE_PREFETCH_LOCK:
        batch = [t for t in wanted if t not in _PROFILE_PREFETCH_INFLIGHT][: max(1, min(80, int(limit or 40)))]
        for t in batch:
            _PROFILE_PREFETCH_INFLIGHT.add(t)
    if not batch:
        return

    def _worker(items: list[str]) -> None:
        rows: list[dict[str, str]] = []
        try:
            for tk in items:
                r = _fetch_profile_live_one(tk)
                if r:
                    rows.append(r)
            if rows:
                _upsert_profile_cache_rows(rows)
                with _LOOKUP_CACHE_LOCK:
                    global _PROFILES_CACHE
                    _PROFILES_CACHE = (0.0, {})
        finally:
            with _PROFILE_PREFETCH_LOCK:
                for tk in items:
                    _PROFILE_PREFETCH_INFLIGHT.discard(tk)

    th = threading.Thread(target=_worker, args=(batch,), daemon=True)
    th.start()


def _normalize_supply_role(role: str) -> str:
    r = str(role or "").strip().lower()
    if r in {"suppliers", "supplier"}:
        return "supplier"
    if r in {"customers", "customer"}:
        return "customer"
    if r in {"partners", "partner"}:
        return "partner"
    return "partner"


def _ensure_supply_chain_table_pg() -> None:
    con_pg = pg_connect()
    if con_pg is None:
        return
    try:
        cur = con_pg.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS company_supply_chain_links_core (
               id BIGSERIAL PRIMARY KEY,
               anchor_ticker TEXT NOT NULL,
               counterparty_ticker TEXT NOT NULL DEFAULT '',
               counterparty_name TEXT NOT NULL DEFAULT '',
               relationship_type TEXT NOT NULL DEFAULT 'supplier',
               evidence TEXT NOT NULL DEFAULT '',
               confidence DOUBLE PRECISION NOT NULL DEFAULT 0.6,
               source TEXT NOT NULL DEFAULT 'manual',
               status TEXT NOT NULL DEFAULT 'active',
               updated_at TEXT NOT NULL
            )"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_supply_chain_core_anchor ON company_supply_chain_links_core(anchor_ticker, status, id DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_supply_chain_core_counterparty ON company_supply_chain_links_core(counterparty_ticker, status)"
        )
        con_pg.commit()
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
    finally:
        con_pg.close()


def list_manual_supply_chain_links(anchor_ticker: str) -> list[dict[str, object]]:
    t = _normalize_ticker(anchor_ticker)
    if not t:
        return []
    out: list[dict[str, object]] = []
    _ensure_supply_chain_table_pg()
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT id, counterparty_ticker, counterparty_name, relationship_type, evidence, confidence
               FROM company_supply_chain_links_core
               WHERE anchor_ticker = %s AND status = 'active'
               ORDER BY id DESC
               LIMIT 500""",
            (t,),
        )
        for r in cur.fetchall() or []:
            out.append(
                {
                    "id": int((r[0] if isinstance(r, (tuple, list)) else r["id"]) or 0),
                    "counterparty_ticker": _normalize_ticker(str((r[1] if isinstance(r, (tuple, list)) else r["counterparty_ticker"]) or "")),
                    "counterparty_name": str((r[2] if isinstance(r, (tuple, list)) else r["counterparty_name"]) or "").strip(),
                    "relationship_type": _normalize_supply_role(str((r[3] if isinstance(r, (tuple, list)) else r["relationship_type"]) or "")),
                    "evidence": str((r[4] if isinstance(r, (tuple, list)) else r["evidence"]) or "").strip(),
                    "confidence": max(0.0, min(1.0, float((r[5] if isinstance(r, (tuple, list)) else r["confidence"]) or 0.6))),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con_pg.close()


def _supply_chain_for_ticker(
    ticker: str,
    *,
    profiles: dict[str, dict[str, str]],
    names: dict[str, str],
    manual_links: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    tk = _normalize_ticker(ticker)
    if not tk:
        return {"suppliers": [], "customers": [], "partners": [], "total": 0}
    mcap_map = market_cap_map([x[1] for x in SUPPLY_CHAIN_SEED_RELATIONSHIPS])
    seen: set[tuple[str, str]] = set()
    buckets: dict[str, list[dict[str, object]]] = {"suppliers": [], "customers": [], "partners": []}
    for raw in list(manual_links or []):
        cp = _normalize_ticker(str(raw.get("counterparty_ticker") or ""))
        if not cp:
            continue
        role_norm = _normalize_supply_role(str(raw.get("relationship_type") or ""))
        bucket = "suppliers" if role_norm == "supplier" else ("customers" if role_norm == "customer" else "partners")
        key = (bucket, cp)
        if key in seen:
            continue
        seen.add(key)
        prof = profiles.get(cp, {})
        buckets[bucket].append(
            {
                "id": int(raw.get("id") or 0),
                "source": "manual",
                "ticker": cp,
                "name": str(raw.get("counterparty_name") or prof.get("name") or names.get(cp) or cp).strip(),
                "industry": str(prof.get("industry") or "").strip(),
                "market_cap": str(mcap_map.get(cp) or "-").strip() or "-",
                "evidence": str(raw.get("evidence") or "").strip(),
                "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0.6))),
            }
        )

    for anchor, counterparty, role, evidence, confidence in SUPPLY_CHAIN_SEED_RELATIONSHIPS:
        if _normalize_ticker(anchor) != tk:
            continue
        cp = _normalize_ticker(counterparty)
        if not cp:
            continue
        role_norm = _normalize_supply_role(role)
        if role_norm == "supplier":
            bucket = "suppliers"
        elif role_norm == "customer":
            bucket = "customers"
        else:
            bucket = "partners"
        key = (bucket, cp)
        if key in seen:
            continue
        seen.add(key)
        prof = profiles.get(cp, {})
        buckets[bucket].append(
            {
                "id": 0,
                "source": "seed",
                "ticker": cp,
                "name": str(prof.get("name") or names.get(cp) or cp).strip(),
                "industry": str(prof.get("industry") or "").strip(),
                "market_cap": str(mcap_map.get(cp) or "-").strip() or "-",
                "evidence": str(evidence or "").strip(),
                "confidence": float(confidence or 0.0),
            }
        )
    for rows in buckets.values():
        rows.sort(key=lambda x: (-float(x.get("confidence") or 0.0), str(x.get("ticker") or "")))
    total = len(buckets["suppliers"]) + len(buckets["customers"]) + len(buckets["partners"])
    return {
        "suppliers": buckets["suppliers"],
        "customers": buckets["customers"],
        "partners": buckets["partners"],
        "total": total,
    }


def add_supply_chain_link(
    anchor_ticker: str,
    counterparty_ticker: str,
    counterparty_name: str,
    relationship_type: str,
    evidence: str = "",
    confidence: float = 0.7,
) -> bool:
    anchor = _normalize_ticker(anchor_ticker)
    cp = _normalize_ticker(counterparty_ticker)
    name = str(counterparty_name or "").strip()
    if not anchor or not cp:
        return False
    if not name:
        name = cp
    role = _normalize_supply_role(relationship_type)
    ev = str(evidence or "").strip()[:1200]
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    if conf <= 0:
        conf = 0.7
    now = dt.datetime.now().isoformat()

    _ensure_supply_chain_table_pg()
    con_pg = pg_connect()
    if con_pg is None:
        return False
    try:
        cur = con_pg.cursor()
        cur.execute(
            """INSERT INTO company_supply_chain_links_core
               (anchor_ticker, counterparty_ticker, counterparty_name, relationship_type, evidence, confidence, source, status, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'manual', 'active', %s)""",
            (anchor, cp, name[:160], role, ev, conf, now),
        )
        con_pg.commit()
        return True
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return False
    finally:
        con_pg.close()


def remove_supply_chain_link(row_id: int) -> bool:
    rid = int(row_id or 0)
    if rid <= 0:
        return False
    _ensure_supply_chain_table_pg()
    con_pg = pg_connect()
    if con_pg is None:
        return False
    try:
        cur = con_pg.cursor()
        cur.execute("UPDATE company_supply_chain_links_core SET status = 'removed' WHERE id = %s", (rid,))
        con_pg.commit()
        return bool(cur.rowcount and int(cur.rowcount) > 0)
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return False
    finally:
        con_pg.close()


def _clean_seg_label(label: str) -> str:
    s = str(label or "").strip()
    if not s:
        return "Other"
    if ":" in s:
        s = s.split(":", 1)[1]
    s = re.sub(r"(Member|Axis)$", "", s, flags=re.I)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\bi Phone\b", "iPhone", s, flags=re.I)
    s = re.sub(r"\bi Pad\b", "iPad", s, flags=re.I)
    s = re.sub(r"\bU S\b", "U.S.", s, flags=re.I)
    s = re.sub(r"\bUsa\b", "U.S.", s, flags=re.I)
    s = re.sub(r"\bUs And Canada\b", "U.S. and Canada", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] if s else "Other"


def _normalize_segments_for_view(seg: dict[str, object]) -> dict[str, object]:
    out_product: list[dict[str, object]] = []
    out_geo: list[dict[str, object]] = []
    for row in list((seg or {}).get("product") or []):
        if not isinstance(row, dict):
            continue
        lbl = _clean_seg_label(str(row.get("label") or ""))
        out_product.append({"label": lbl, "value": row.get("value"), "pct": row.get("pct")})
    for row in list((seg or {}).get("geography") or []):
        if not isinstance(row, dict):
            continue
        lbl = _clean_seg_label(str(row.get("label") or ""))
        pct = float(row.get("pct") or 0.0)
        if pct <= 0:
            continue
        out_geo.append({"label": lbl, "value": row.get("value"), "pct": pct})
    return {"product": out_product[:8], "geography": out_geo[:8]}


@dataclass
class CompanyRow:
    ticker: str
    name: str
    country: str
    industry: str
    market_cap: str


def safe_resolve_filing_path(path_s: str) -> Path | None:
    return resolve_filing_path(path_s)


def _canonical_filing_path(path_s: str) -> str:
    p = safe_resolve_filing_path(path_s)
    if p is not None:
        return str(p)
    rel = normalize_filing_rel_path(path_s)
    if rel and cloud_files.exists(rel):
        return rel
    return ""


def _filing_category(form: str) -> str:
    f = str(form or "").strip().upper()
    if f in {"10-K", "20-F", "40-F"}:
        return "annual"
    if f in {"10-Q"}:
        return "quarterly"
    if "DEF 14A" in f or "DEFA14A" in f:
        return "proxy"
    if f in {"8-K", "6-K"}:
        return "current"
    if f in {"3", "4", "5"} or f.startswith("SC 13"):
        return "ownership"
    return "other"


def _parse_mcap_num(text: str) -> float:
    s = str(text or "").strip().upper().replace("$", "").replace(",", "")
    if not s or s == "-":
        return -1.0
    mult = 1.0
    if s.endswith("T"):
        mult = 1e12
        s = s[:-1]
    elif s.endswith("B"):
        mult = 1e9
        s = s[:-1]
    elif s.endswith("M"):
        mult = 1e6
        s = s[:-1]
    elif s.endswith("K"):
        mult = 1e3
        s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return -1.0


def _load_index_tickers(index_key: str) -> list[str]:
    k = str(index_key or "").strip().lower()
    file_name = ""
    for kk, _lbl, fn in INDEX_FILES:
        if kk == k:
            file_name = fn
            break
    if not file_name:
        return []
    p = DATA_DIR / "index_lists" / file_name
    if not p.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = _normalize_ticker(ln)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _company_profiles() -> dict[str, dict[str, str]]:
    global _PROFILES_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _PROFILES_CACHE
        if cached and (now - float(ts)) <= _LOOKUP_CACHE_TTL_SEC:
            return dict(cached)
    out: dict[str, dict[str, str]] = {}
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT ticker, name, country, industry, sector FROM company_profile_cache_core")
            for r in cur.fetchall() or []:
                t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
                if not t:
                    continue
                out[t] = {
                    "name": str((r[1] if isinstance(r, (tuple, list)) else r["name"]) or "").strip(),
                    "country": str((r[2] if isinstance(r, (tuple, list)) else r["country"]) or "").strip(),
                    "industry": str((r[3] if isinstance(r, (tuple, list)) else r["industry"]) or "").strip(),
                    "sector": str((r[4] if isinstance(r, (tuple, list)) else r["sector"]) or "").strip(),
                }
            if not out:
                cur.execute("SELECT ticker, name FROM companies_core")
                for r in cur.fetchall() or []:
                    t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
                    if not t:
                        continue
                    out[t] = {
                        "name": str((r[1] if isinstance(r, (tuple, list)) else r["name"]) or "").strip(),
                        "country": "",
                        "industry": "",
                        "sector": "",
                    }
        except Exception:
            out = {}
        finally:
            con_pg.close()
    with _LOOKUP_CACHE_LOCK:
        _PROFILES_CACHE = (now, dict(out))
    return out


def _similar_companies_for(
    ticker: str,
    *,
    industry: str,
    market_cap: str,
    profiles: dict[str, dict[str, str]],
    names: dict[str, str],
    existing_competitors: list[dict[str, str | int]],
    limit: int = 8,
) -> list[dict[str, str]]:
    tk = _normalize_ticker(ticker)
    if not tk:
        return []
    ind = str(industry or "").strip()
    base_mcap = _parse_mcap_num(market_cap)
    existing = {
        _normalize_ticker(str(x.get("ticker") or ""))
        for x in list(existing_competitors or [])
        if _normalize_ticker(str(x.get("ticker") or ""))
    }

    candidates: list[str] = []
    for t, p in profiles.items():
        ct = _normalize_ticker(t)
        if not ct or ct == tk or ct in existing:
            continue
        if ind and str(p.get("industry") or "").strip() != ind:
            continue
        candidates.append(ct)
    if not candidates:
        return []

    mcap_map = market_cap_map(candidates)
    scored: list[tuple[float, str]] = []
    for ct in candidates:
        cmp_mcap = _parse_mcap_num(str(mcap_map.get(ct) or "-"))
        if base_mcap > 0 and cmp_mcap > 0:
            score = abs((cmp_mcap - base_mcap) / base_mcap)
        elif cmp_mcap > 0:
            score = 10.0
        else:
            score = 99.0
        scored.append((score, ct))
    scored.sort(key=lambda x: (x[0], x[1]))

    out: list[dict[str, str]] = []
    for _score, ct in scored[: max(1, min(20, int(limit or 8)))]:
        p = profiles.get(ct, {})
        out.append(
            {
                "ticker": ct,
                "name": str(p.get("name") or names.get(ct) or ct).strip(),
                "industry": str(p.get("industry") or "").strip(),
                "market_cap": str(mcap_map.get(ct) or "-").strip() or "-",
            }
        )
    return out


def _companies_name_map() -> dict[str, str]:
    global _NAMES_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _NAMES_CACHE
        if cached and (now - float(ts)) <= _LOOKUP_CACHE_TTL_SEC:
            return dict(cached)
    out: dict[str, str] = {}
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            try:
                cur.execute("SELECT ticker, name FROM companies_core")
            except Exception:
                cur.execute("SELECT ticker, name FROM company_profile_cache_core")
            for r in cur.fetchall() or []:
                t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
                if not t:
                    continue
                out[t] = str((r[1] if isinstance(r, (tuple, list)) else r["name"]) or "").strip()
        except Exception:
            out = {}
        finally:
            con_pg.close()
    with _LOOKUP_CACHE_LOCK:
        _NAMES_CACHE = (now, dict(out))
    return out


def _live_price(ticker: str) -> str:
    t = _normalize_ticker(ticker)
    if not t:
        return "-"
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return "-"
    try:
        obj = yf.Ticker(t)
        fi = (obj.fast_info or {})
        px = float(fi.get("lastPrice") or 0.0)
        if px <= 0:
            px = float(fi.get("regularMarketPrice") or 0.0)
        if px <= 0:
            info = (obj.info or {})
            px = float(info.get("currentPrice") or 0.0)
        if px <= 0:
            hist = obj.history(period="5d", interval="1d")
            if not hist.empty:
                px = float(hist["Close"].dropna().iloc[-1] or 0.0)
        if px > 0:
            return f"{px:.2f}"
    except Exception:
        return "-"
    return "-"


def _saved_lists() -> list[dict[str, str | int]]:
    return []


def _saved_list_tickers(list_name: str) -> list[str]:
    n = str(list_name or "").strip()
    if not n:
        return []
    return []


def _moat_tickers(moat_key: str) -> list[str]:
    mk = str(moat_key or "").strip().lower()
    if not mk:
        return []
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute("SELECT ticker FROM company_moat_tags_core WHERE moat_key = %s", (mk,))
        out = []
        seen: set[str] = set()
        for r in cur.fetchall() or []:
            t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out
    except Exception:
        return []
    finally:
        con_pg.close()


def _all_universe() -> list[str]:
    global _UNIVERSE_ALL_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _UNIVERSE_ALL_CACHE
        if cached and (now - float(ts)) <= _UNIVERSE_CACHE_TTL_SEC:
            return list(cached)
    seen: set[str] = set()
    out: list[str] = []
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            for sql in (
                "SELECT ticker FROM company_profile_cache_core",
                "SELECT ticker FROM companies_core",
                "SELECT ticker FROM universe_registry_core WHERE is_us_listed = TRUE",
            ):
                try:
                    cur.execute(sql)
                    rows = cur.fetchall() or []
                except Exception:
                    continue
                for r in rows:
                    t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
                    if not t or t in seen:
                        continue
                    seen.add(t)
                    out.append(t)
        finally:
            con_pg.close()
    with _LOOKUP_CACHE_LOCK:
        _UNIVERSE_ALL_CACHE = (now, list(out))
    return out


def _registry_universe(mode: str) -> list[str]:
    global _UNIVERSE_REG_CACHE
    m = str(mode or "").strip().lower()
    if m not in {"us_listed", "us_otc", "otc_only"}:
        return []
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        row = _UNIVERSE_REG_CACHE.get(m)
        if row:
            ts, cached = row
            if cached and (now - float(ts)) <= _UNIVERSE_CACHE_TTL_SEC:
                return list(cached)
    seen: set[str] = set()
    out: list[str] = []
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        if m == "us_listed":
            sql = "SELECT ticker FROM universe_registry_core WHERE is_us_listed = TRUE"
        elif m == "otc_only":
            sql = "SELECT ticker FROM universe_registry_core WHERE is_otc = TRUE"
        else:
            sql = "SELECT ticker FROM universe_registry_core WHERE is_us_listed = TRUE OR is_otc = TRUE"
        cur.execute(sql)
        rows = cur.fetchall() or []
        for r in rows:
            t = _normalize_ticker(str((r[0] if isinstance(r, (tuple, list)) else r["ticker"]) or ""))
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
    except Exception:
        return []
    finally:
        con_pg.close()
    with _LOOKUP_CACHE_LOCK:
        _UNIVERSE_REG_CACHE[m] = (now, list(out))
    return out


def _read_my_companies() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    p = DATA_DIR / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in ln.split(",")]
            t = _normalize_ticker(parts[0] if parts else "")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    w = DATA_DIR / "my_watchlist.txt"
    if w.exists():
        for ln in w.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = [x.strip() for x in s.split(",")]
            t = _normalize_ticker(parts[0] if parts else "")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def index_filters() -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for key, label, _fn in INDEX_FILES:
        cnt = len(_load_index_tickers(key))
        rows.append({"key": key, "label": label, "count": cnt})
    return rows


def list_filters() -> list[dict[str, str | int]]:
    return _saved_lists()


def moat_filters() -> list[dict[str, str | int]]:
    con_pg = pg_connect()
    cnt_map: dict[str, int] = {}
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT moat_key, COUNT(*) AS cnt FROM company_moat_tags_core GROUP BY moat_key")
            for r in cur.fetchall() or []:
                key = str((r[0] if isinstance(r, (tuple, list)) else r["moat_key"]) or "").strip().lower()
                val = int((r[1] if isinstance(r, (tuple, list)) else r["cnt"]) or 0)
                cnt_map[key] = val
        finally:
            con_pg.close()
    out: list[dict[str, str | int]] = []
    for key, label in MOAT_OPTIONS:
        out.append({"key": key, "label": label, "count": int(cnt_map.get(key, 0))})
    return out


def list_companies(
    query: str = "",
    industry: str = "",
    index_key: str = "",
    list_name: str = "",
    moat_key: str = "",
    market: str = "",
    size: str = "",
    scope: str = "all",
    sort: str = "mcap_desc",
    page: int = 1,
    page_size: int = 80,
) -> dict[str, object]:
    q = str(query or "").strip().lower()
    ind_raw = str(industry or "").strip()
    ind_set = {x.strip() for x in ind_raw.split(",") if x.strip()}
    idx = str(index_key or "").strip().lower()
    lst = str(list_name or "").strip()
    moat = str(moat_key or "").strip().lower()
    market_key = str(market or "").strip().lower()
    size_key = str(size or "").strip().lower()
    if sort not in {"mcap_desc", "mcap_asc", "name_asc", "ticker_asc"}:
        sort = "mcap_desc"
    ps = max(20, min(200, int(page_size or 80)))
    pg = max(1, int(page or 1))

    sc = str(scope or "all").strip().lower()
    if sc not in {"all", "my", "us_listed", "us_otc"}:
        sc = "all"
    if sc == "my":
        base = _read_my_companies()
    elif sc in {"us_listed", "us_otc"}:
        reg = _registry_universe(sc)
        base = reg if reg else _all_universe()
    else:
        base = _all_universe()
    if idx:
        idx_set = set(_load_index_tickers(idx))
        base = [t for t in base if t in idx_set]
    if lst:
        lst_set = set(_saved_list_tickers(lst))
        base = [t for t in base if t in lst_set]
    if moat:
        moat_set = set(_moat_tickers(moat))
        base = [t for t in base if t in moat_set]
    if market_key in {"us_listed", "us_otc", "otc_only"}:
        market_set = set(_registry_universe(market_key))
        if market_set:
            base = [t for t in base if t in market_set]

    profiles = _company_profiles()
    names = _companies_name_map()

    # First pass: cheap text filters before market-cap lookup.
    pre_rows: list[tuple[str, str, str, str]] = []
    for t in base:
        p = profiles.get(t, {})
        nm = str(p.get("name") or names.get(t) or t).strip()
        ctry = str(p.get("country") or "-").strip() or "-"
        ind_txt = str(p.get("industry") or p.get("sector") or "Unknown").strip() or "Unknown"
        if ind_set and ind_txt not in ind_set:
            continue
        if q:
            hay = f"{t} {nm} {ind_txt}".lower()
            if q not in hay:
                continue
        pre_rows.append((t, nm, ctry, ind_txt))

    # Fetch market caps only for prefiltered candidates (large speedup on search).
    pre_tickers = [r[0] for r in pre_rows]
    mcap_map = market_cap_map(pre_tickers, live_fetch=False)

    rows: list[CompanyRow] = []
    for t, nm, ctry, ind_txt in pre_rows:
        mcap = str(mcap_map.get(t) or "-").strip() or "-"
        row = CompanyRow(ticker=t, name=nm, country=ctry, industry=ind_txt, market_cap=mcap)
        mcap_num = _parse_mcap_num(mcap)
        if size_key == "xlarge":
            if mcap_num <= 200_000_000_000:
                continue
        elif size_key == "large":
            if not (10_000_000_000 < mcap_num <= 200_000_000_000):
                continue
        elif size_key == "medium":
            if not (2_000_000_000 < mcap_num <= 10_000_000_000):
                continue
        elif size_key == "small":
            if not (300_000_000 < mcap_num <= 2_000_000_000):
                continue
        elif size_key == "micro":
            if not (50_000_000 < mcap_num <= 300_000_000):
                continue
        elif size_key == "nano":
            if not (0 <= mcap_num <= 50_000_000):
                continue
        rows.append(row)

    if sort == "name_asc":
        rows.sort(key=lambda r: (r.name.lower(), r.ticker))
    elif sort == "ticker_asc":
        rows.sort(key=lambda r: r.ticker)
    elif sort == "mcap_asc":
        rows.sort(key=lambda r: _parse_mcap_num(r.market_cap))
    else:
        rows.sort(key=lambda r: _parse_mcap_num(r.market_cap), reverse=True)

    industries: list[str] = sorted({r.industry for r in rows if r.industry})

    total = len(rows)
    pages = max(1, (total + ps - 1) // ps)
    if pg > pages:
        pg = pages
    start = (pg - 1) * ps
    end = start + ps
    page_rows = rows[start:end]
    # Ensure visible rows have market cap populated even when global fallback
    # budget is exhausted for very large universes.
    visible_tickers = [r.ticker for r in page_rows if r.ticker and (not str(r.market_cap or "").strip() or str(r.market_cap).strip() == "-")]
    if visible_tickers:
        # Read cache only for visible rows, then fetch missing caps in background.
        page_mcap = market_cap_map(visible_tickers, live_fetch=False)
        if page_mcap:
            refreshed: list[CompanyRow] = []
            for r in page_rows:
                m = str(page_mcap.get(r.ticker) or "").strip()
                if m and m != "-":
                    refreshed.append(CompanyRow(ticker=r.ticker, name=r.name, country=r.country, industry=r.industry, market_cap=m))
                else:
                    refreshed.append(r)
            page_rows = refreshed
        prefetch_market_cap_async(visible_tickers, limit=80)
    unknown_tickers = [r.ticker for r in page_rows if str(r.industry or "").strip().lower() == "unknown"]
    if unknown_tickers:
        prefetch_company_profiles_async(unknown_tickers, limit=40)
    return {
        "rows": page_rows,
        "total": total,
        "page": pg,
        "pages": pages,
        "page_size": ps,
        "industries": industries,
        "sort": sort,
        "scope": sc,
    }


def _parse_date_token(raw: str) -> dt.date | None:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    p = parse_datetime_flexible(
        s,
        formats=("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"),
    )
    if p is not None:
        return p.date()
    return None


def _extract_announced_next_earnings_date(text: str, filing_date: str = "") -> str:
    txt = str(text or "")
    if not txt:
        return ""
    low = txt.lower()
    if not re.search(r"\b(earnings|financial results|conference call|quarterly results)\b", low):
        return ""
    base = _parse_date_token(str(filing_date or "")) or dt.date.today()
    date_tokens = re.findall(
        r"\b(?:\d{4}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
        txt,
        flags=re.IGNORECASE,
    )
    future: list[dt.date] = []
    for tok in date_tokens:
        d = _parse_date_token(tok)
        if d is None:
            continue
        delta = (d - base).days
        if 0 <= delta <= 500:
            future.append(d)
    if not future:
        return ""
    return min(future).isoformat()


def _beat_miss_from_text(text: str) -> str:
    low = str(text or "").lower()
    if not low:
        return "Unknown"
    beat_hits = len(re.findall(r"\b(beat|beats|beating|above expectations|ahead of expectations|exceeded|better than expected|surpass)\b", low))
    miss_hits = len(re.findall(r"\b(miss|missed|below expectations|under expectations|weaker than expected|shortfall)\b", low))
    if beat_hits > miss_hits and beat_hits > 0:
        return "Beat"
    if miss_hits > beat_hits and miss_hits > 0:
        return "Miss"
    if beat_hits > 0 and miss_hits > 0:
        return "Mixed"
    return "Unknown"


def _to_float_maybe(raw: object) -> float | None:
    s = str(raw or "").strip()
    if not s or s in {"-", "N/A", "n/a"}:
        return None
    s = s.replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _calendar_snapshot_fallback(ticker: str) -> dict[str, str]:
    tk = _normalize_ticker(ticker)
    if not tk:
        return {}
    con_pg = pg_connect()
    if con_pg is None:
        return {}
    rows: list[tuple] = []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """
            SELECT event_date, reported, verdict, event_status, eps_actual, eps_estimate, surprise_txt, result_source
            FROM earnings_calendar_snapshot_core
            WHERE symbol=%s
            ORDER BY event_date DESC
            LIMIT 60
            """,
            (tk,),
        )
        rows = cur.fetchall() or []
    except Exception:
        rows = []
    finally:
        try:
            con_pg.close()
        except Exception:
            pass
    if not rows:
        return {}

    today = dt.date.today()
    next_date = ""
    last_date = ""
    last_result = "Unknown"
    source = ""
    for r in rows:
        d = str(r[0] or "").strip()[:10]
        if not d:
            continue
        dd = _parse_date_token(d)
        if dd is None:
            continue
        if not next_date and dd >= today:
            rep = str(r[1] or "").strip()
            stat = str(r[3] or "").strip().lower()
            if rep != "1" and stat != "reported":
                next_date = d
        if not last_date and dd <= today:
            last_date = d
            verdict = str(r[2] or "").strip().lower()
            if "beat" in verdict:
                last_result = "Beat"
            elif "miss" in verdict:
                last_result = "Miss"
            elif verdict:
                last_result = "Mixed"
            if last_result == "Unknown":
                act = _to_float_maybe(r[4])
                est = _to_float_maybe(r[5])
                if act is not None and est is not None:
                    if act > est:
                        last_result = "Beat"
                    elif act < est:
                        last_result = "Miss"
                    else:
                        last_result = "Mixed"
            source = str(r[7] or "").strip() or "Earnings calendar snapshot"
        if next_date and last_date:
            break
    return {
        "next_announced_date": next_date,
        "last_release_date": last_date,
        "last_result": last_result,
        "brief": "",
        "brief_source": source,
    }


def _build_sec_earnings_brief(
    *,
    ticker: str,
    releases: list[dict[str, object]],
    quarterly_signals: list[dict[str, object]],
    analyses: list[dict[str, object]],
) -> dict[str, str]:
    rels = [dict(x or {}) for x in (releases or []) if isinstance(x, dict)]
    rels.sort(key=lambda r: str(r.get("call_date") or ""), reverse=True)
    latest_release = rels[0] if rels else {}
    last_release_date = str(latest_release.get("call_date") or "").strip()
    if not last_release_date and quarterly_signals:
        last_release_date = str(dict(quarterly_signals[0] or {}).get("date") or "").strip()[:10]
    if not last_release_date and analyses:
        last_release_date = str(dict(analyses[0] or {}).get("filing_date") or "").strip()[:10]

    next_date = ""
    for r in rels[:10]:
        next_date = _extract_announced_next_earnings_date(
            str(r.get("excerpt") or ""),
            filing_date=str(r.get("call_date") or ""),
        )
        if not next_date:
            p = str(r.get("path") or "").strip()
            if p:
                try:
                    txt = read_filing_text_any(p, max_chars=180000) or ""
                    next_date = _extract_announced_next_earnings_date(
                        txt,
                        filing_date=str(r.get("call_date") or ""),
                    )
                except Exception:
                    pass
        if next_date:
            break

    lead_text = ""
    lead_source = ""
    if latest_release:
        lead_text = str(latest_release.get("excerpt") or "").strip()
        lead_source = "SEC earnings release"
    if not lead_text and analyses:
        a0 = dict(analyses[0] or {})
        lead_text = str(a0.get("summary") or "").strip()
        lead_source = "Earnings analysis"
    if not lead_text and quarterly_signals:
        s0 = dict(quarterly_signals[0] or {})
        lead_text = str(s0.get("text") or "").strip()
        lead_source = str(s0.get("source") or "SEC facts").strip()

    last_result = _beat_miss_from_text(lead_text)
    out = {
        "next_announced_date": next_date,
        "last_release_date": last_release_date,
        "last_result": last_result,
        "brief": lead_text[:240],
        "brief_source": lead_source[:80],
    }
    fb = _calendar_snapshot_fallback(ticker)
    if fb:
        if not out.get("next_announced_date"):
            out["next_announced_date"] = str(fb.get("next_announced_date") or "")
        if not out.get("last_release_date"):
            out["last_release_date"] = str(fb.get("last_release_date") or "")
        if str(out.get("last_result") or "Unknown") == "Unknown":
            out["last_result"] = str(fb.get("last_result") or "Unknown")
        if not out.get("brief") and fb.get("brief_source"):
            out["brief"] = "Derived from earnings calendar snapshot."
            out["brief_source"] = str(fb.get("brief_source") or "")[:80]
    return out


def company_detail(ticker: str) -> dict[str, object]:
    t = _normalize_ticker(ticker)
    if not t:
        return {}
    profiles = _company_profiles()
    names = _companies_name_map()
    p = profiles.get(t, {})
    mcap = market_cap_map([t]).get(t, "-")

    moats: list[str] = []
    competitors: list[dict[str, str | int]] = []
    notes: list[dict[str, str | int]] = []
    system_logs: list[dict[str, str | int]] = []
    tasks: list[dict[str, str | int]] = []
    reminders: list[dict[str, str | int]] = []
    timeline_events: list[dict[str, object]] = []
    filings: list[dict[str, str]] = []
    active_proposal: dict[str, object] = {}
    for r in list_recent_notes_pg(limit=80):
        if str(r.get("source_table") or "") != "workspace_journal":
            continue
        if str(r.get("ticker") or "").strip().upper() != t:
            continue
        row = {
            "id": int(r.get("id") or 0),
            "created_at": str(r.get("date") or ""),
            "action": str(r.get("tag") or "Note"),
            "emotion": "",
            "note": str(r.get("text") or ""),
        }
        action_txt = str(row.get("action") or "").strip().lower()
        note_txt = str(row.get("note") or "").strip().lower()
        is_system = (
            str(r.get("created_by") or "human").strip().lower() != "human"
            or ("proposal" in action_txt)
            or ("proposal" in note_txt)
        )
        if not is_system:
            notes.append(row)
        else:
            system_logs.append(row)
        if len(notes) >= 120 and len(system_logs) >= 120:
            break
    for r in list_todos_pg(open_only=True, limit=50, ticker=t) + list_todos_pg(open_only=False, limit=50, ticker=t):
        tasks.append(
            {
                "id": int(r.get("id") or 0),
                "task": str(r.get("task") or ""),
                "status": str(r.get("status") or "open"),
                "priority": str(r.get("priority") or "P2"),
                "due_date": str(r.get("due_date") or ""),
                "created_at": str(r.get("created_at") or ""),
                "category": str(r.get("category") or "company"),
            }
        )
    reminders = list_company_reminders_pg(ticker=t, limit=160)

    cands = [x for x in list_action_proposals_pg(status="executed", limit=30) if str(x.get("ticker") or "").upper() == t]
    if not cands:
        cands = [x for x in list_action_proposals_pg(status="open", limit=30) if str(x.get("ticker") or "").upper() == t]
    pr = cands[0] if cands else {}
    if pr:
        active_proposal = {
            "id": int(pr.get("id") or 0),
            "status": str(pr.get("status") or "").strip(),
            "kind": str(pr.get("kind") or "").strip(),
            "title": str(pr.get("title") or "").strip(),
            "confidence": float(pr.get("confidence") or 0.0),
            "priority_score": float(pr.get("priority_score") or 0.0),
            "updated_at": str(pr.get("updated_at") or "").strip(),
            "reasoning": dict(pr.get("reasoning_json") or {}),
            "insights": [x for x in list(pr.get("insights_json") or []) if isinstance(x, dict)][:3],
            "citations": [x for x in list(pr.get("citations_json") or []) if isinstance(x, dict)][:6],
        }

    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute(
                "SELECT moat_key FROM company_moat_tags_core WHERE ticker = %s ORDER BY moat_key",
                (t,),
            )
            for r in cur.fetchall() or []:
                mk = str((r[0] if isinstance(r, (tuple, list)) else r["moat_key"]) or "").strip().lower()
                if mk:
                    moats.append(mk)
            cur.execute(
                """SELECT id, competitor_ticker, competitor_name, evidence, source_date
                   FROM company_sec_competitors_core
                   WHERE ticker = %s AND status = 'active'
                   ORDER BY confidence DESC, id DESC
                   LIMIT 50""",
                (t,),
            )
            for r in cur.fetchall() or []:
                rid = int((r[0] if isinstance(r, (tuple, list)) else r["id"]) or 0)
                ct = str((r[1] if isinstance(r, (tuple, list)) else r["competitor_ticker"]) or "")
                cn = str((r[2] if isinstance(r, (tuple, list)) else r["competitor_name"]) or "").strip()
                ev = str((r[3] if isinstance(r, (tuple, list)) else r["evidence"]) or "").strip()
                sd = str((r[4] if isinstance(r, (tuple, list)) else r["source_date"]) or "").strip()
                competitors.append(
                    {
                        "id": rid,
                        "ticker": _normalize_ticker(ct),
                        "name": cn,
                        "evidence": ev,
                        "date": sd,
                    }
                )
            cur.execute(
                """SELECT form, date, accession, doc_url, path,
                          (content IS NOT NULL AND content != '') AS has_content
                   FROM filings_core
                   WHERE ticker = %s
                   ORDER BY date DESC, id DESC
                   LIMIT 100""",
                (t,),
            )
            for r in cur.fetchall() or []:
                form = str((r[0] if isinstance(r, (tuple, list)) else r["form"]) or "").strip()
                fdate = str((r[1] if isinstance(r, (tuple, list)) else r["date"]) or "").strip()
                accession = str((r[2] if isinstance(r, (tuple, list)) else r["accession"]) or "").strip()
                doc_url = str((r[3] if isinstance(r, (tuple, list)) else r["doc_url"]) or "").strip()
                pth = str((r[4] if isinstance(r, (tuple, list)) else r["path"]) or "").strip()
                has_db_content = bool(r[5] if isinstance(r, (tuple, list)) else r.get("has_content"))
                cpath = _canonical_filing_path(pth)
                filings.append(
                    {
                        "form": form or "-",
                        "date": fdate or "-",
                        "accession": accession or "-",
                        "doc_url": doc_url,
                        "path": cpath,
                        "file_name": (Path(cpath).name if cpath else (Path(pth).name if pth else "")),
                        "has_local": "1" if (has_db_content or filing_path_available(pth)) else "0",
                    }
                )
        except Exception:
            pass
        finally:
            con_pg.close()

    filing_form_counts: dict[str, int] = {}
    for r in filings:
        fm = str(r.get("form") or "-")
        filing_form_counts[fm] = int(filing_form_counts.get(fm, 0)) + 1

    filing_group_map: dict[str, list[dict[str, str]]] = {}
    for r in filings:
        ck = _filing_category(str(r.get("form") or ""))
        filing_group_map.setdefault(ck, []).append(r)
    filing_groups: list[dict[str, object]] = []
    for key, label in FILING_CATEGORY_ORDER:
        rows = filing_group_map.get(key, [])
        if not rows:
            continue
        filing_groups.append(
            {
                "key": key,
                "label": label,
                "count": len(rows),
                "rows": rows,
            }
        )

    for n in notes:
        timeline_events.append(
            {
                "kind": "note",
                "icon": "📝",
                "created_at": str(n.get("created_at") or ""),
                "is_ai": False,
                "ticker": t,
                "title": str(n.get("action") or "Note"),
                "text": str(n.get("note") or ""),
                "note_id": int(n.get("id") or 0),
            }
        )
    for n in system_logs:
        timeline_events.append(
            {
                "kind": "log",
                "icon": "🤖",
                "created_at": str(n.get("created_at") or ""),
                "is_ai": True,
                "ticker": t,
                "title": str(n.get("action") or "System Log"),
                "text": str(n.get("note") or ""),
                "note_id": int(n.get("id") or 0),
            }
        )
    for r in tasks:
        is_ai_task = is_ai_task_text(str(r.get("task") or ""), str(r.get("category") or ""))
        title = clean_task_text(str(r.get("task") or "Task"))[:140]
        timeline_events.append(
            {
                "kind": "log" if is_ai_task else "task",
                "icon": "🤖" if is_ai_task else "✅",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_task,
                "ticker": t,
                "title": title or "Task",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'open')}"
                    if is_ai_task
                    else f"Task • {str(r.get('status') or 'open')}"
                ),
                "todo_id": int(r.get("id") or 0),
                "status": str(r.get("status") or "open"),
                "due_date": str(r.get("due_date") or ""),
            }
        )
    for r in reminders:
        note_txt = str(r.get("note") or "")
        low_note = note_txt.lower()
        is_ai_reminder = ("proposal" in low_note)
        timeline_events.append(
            {
                "kind": "log" if is_ai_reminder else "reminder",
                "icon": "🤖" if is_ai_reminder else "⏰",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_reminder,
                "ticker": t,
                "title": clean_task_text(note_txt)[:140] or "Reminder",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'open')}"
                    if is_ai_reminder
                    else f"Reminder • {str(r.get('status') or 'open')}"
                ),
                "reminder_id": int(r.get("id") or 0),
                "status": str(r.get("status") or "open"),
                "remind_at": str(r.get("remind_at") or ""),
            }
        )
    moat_map = {k: v for k, v in MOAT_OPTIONS}
    mini = get_mini_statements(t)
    intel = get_company_intel(t, refresh=False)
    revenue_segments = _normalize_segments_for_view(dict(intel.get("revenue_segments") or {}))
    buyback = dict(intel.get("buyback") or {})
    insider_trades = [x for x in list(intel.get("insider_trades") or []) if isinstance(x, dict)][:16]
    intel_status = dict(intel.get("status") or {})
    intel_asof = str(intel.get("asof") or "").strip()
    intel_source = str(intel.get("source") or "").strip()
    deltas = compute_financial_deltas(ticker=t, years=5)
    earnings_calls = list_earnings_transcripts(t, limit=36)
    earnings_releases = list_sec_earnings_releases(t, limit=12, lookback_years=10, max_filings=260)
    earnings_analysis = list_earnings_analysis(t, limit=8)
    earnings_call_source_counts: dict[str, int] = {}
    for tr in earnings_calls:
        src = str((tr or {}).get("source_type") or "").strip().lower() or "unknown"
        earnings_call_source_counts[src] = int(earnings_call_source_counts.get(src, 0)) + 1
    quarterly_signals = list_quarterly_result_signals(t, limit=10)
    earnings_sec_brief = _build_sec_earnings_brief(
        ticker=t,
        releases=earnings_releases,
        quarterly_signals=quarterly_signals,
        analyses=earnings_analysis,
    )

    def _days_old(asof_s: str) -> int | None:
        s = str(asof_s or "").strip()
        if not s:
            return None
        try:
            d = dt.date.fromisoformat(s[:10])
            return int((dt.date.today() - d).days)
        except Exception:
            return None

    mini_asof = str((mini or {}).get("asof") or "").strip()
    mini_days = _days_old(mini_asof)
    intel_days = _days_old(intel_asof)
    quant_ready = bool((mini or {}).get("years")) and bool(revenue_segments or buyback)
    qual_ready = bool((intel or {}).get("status"))
    stale = bool((mini_days is not None and mini_days > 3) or (intel_days is not None and intel_days > 3))
    data_coverage = "quant_ready" if quant_ready else ("qual_only" if qual_ready else "missing")
    data_coverage_label = "Quant Ready" if data_coverage == "quant_ready" else ("Qual Only" if data_coverage == "qual_only" else "Missing")
    data_coverage_color = "#166534" if data_coverage == "quant_ready" else ("#92400e" if data_coverage == "qual_only" else "#b91c1c")
    data_coverage_meta = {
        "state": data_coverage,
        "label": data_coverage_label,
        "color": data_coverage_color,
        "stale": stale,
        "mini_asof": mini_asof,
        "intel_asof": intel_asof,
        "mini_days_old": mini_days,
        "intel_days_old": intel_days,
        "sla_days": 3,
        "schema_version": "financials_schema_v1",
        "provenance": {
            "mini_statements_source": str((mini or {}).get("source") or ""),
            "intel_source": intel_source,
        },
    }

    timeline_events.sort(
        key=lambda e: (
            parse_datetime_flexible(str(e.get("created_at") or ""))
            or parse_datetime_flexible(
                str(e.get("remind_at") or ""),
                formats=("%Y-%m-%d %H:%M", "%Y-%m-%d"),
            )
            or dt.datetime.min
        ),
        reverse=True,
    )

    similar_companies = _similar_companies_for(
        t,
        industry=str(p.get("industry") or p.get("sector") or "Unknown").strip() or "Unknown",
        market_cap=str(mcap or "-"),
        profiles=profiles,
        names=names,
        existing_competitors=competitors,
        limit=8,
    )
    manual_supply_links = list_manual_supply_chain_links(t)
    supply_chain = _supply_chain_for_ticker(t, profiles=profiles, names=names, manual_links=manual_supply_links)
    return {
        "ticker": t,
        "name": str(p.get("name") or names.get(t) or t).strip(),
        "country": str(p.get("country") or "-").strip() or "-",
        "industry": str(p.get("industry") or p.get("sector") or "Unknown").strip() or "Unknown",
        "market_cap": str(mcap or "-").strip() or "-",
        "current_price": _live_price(t),
        "moat_keys": moats,
        "moats": [{"key": k, "label": moat_map.get(k, k)} for k in moats],
        "moat_options": [{"key": k, "label": v} for k, v in MOAT_OPTIONS],
        "competitors": competitors,
        "similar_companies": similar_companies,
        "supply_chain": supply_chain,
        "notes": notes,
        "system_logs": system_logs,
        "tasks": tasks,
        "reminders": reminders,
        "timeline_events": timeline_events,
        "active_proposal": active_proposal,
        "price_metrics": get_price_metrics(t),
        "mini_statements": mini,
        "financial_deltas": deltas,
        "revenue_segments": revenue_segments,
        "buyback": buyback,
        "insider_trades": insider_trades,
        "intel_status": intel_status,
        "intel_asof": intel_asof,
        "intel_source": intel_source,
        "data_coverage": data_coverage_meta,
        "earnings_calls": earnings_calls,
        "earnings_releases": earnings_releases,
        "earnings_analysis": earnings_analysis,
        "earnings_sec_brief": earnings_sec_brief,
        "earnings_call_source_counts": earnings_call_source_counts,
        "quarterly_signals": quarterly_signals,
        "filings": filings,
        "filing_groups": filing_groups,
        "filing_form_counts": filing_form_counts,
    }


def save_company_moats(ticker: str, moat_keys: list[str]) -> bool:
    t = _normalize_ticker(ticker)
    if not t:
        return False
    allowed = {k for k, _v in MOAT_OPTIONS}
    picked = sorted({str(x or "").strip().lower() for x in moat_keys if str(x or "").strip().lower() in allowed})
    try:
        from app.services.postgres_core_service import save_company_moats_pg
        save_company_moats_pg(t, picked)
        return True
    except Exception:
        return False


def add_competitor(ticker: str, competitor_ticker: str, competitor_name: str, evidence: str = "") -> bool:
    t = _normalize_ticker(ticker)
    ct = _normalize_ticker(competitor_ticker)
    name = str(competitor_name or "").strip()
    ev = str(evidence or "").strip()
    if not t or (not ct and not name):
        return False
    try:
        from app.services.postgres_core_service import add_company_competitor_pg
        add_company_competitor_pg(t, ct, name, evidence=ev)
        return True
    except Exception:
        return False


def update_competitor(row_id: int, competitor_ticker: str, competitor_name: str, evidence: str = "") -> bool:
    rid = int(row_id or 0)
    if rid <= 0:
        return False
    ct = _normalize_ticker(competitor_ticker)
    name = str(competitor_name or "").strip()[:160]
    ev = str(evidence or "").strip()[:1200]
    try:
        from app.services.postgres_core_service import update_company_competitor_pg
        return bool(update_company_competitor_pg(rid, competitor_ticker=ct, competitor_name=name, evidence=ev))
    except Exception:
        return False


def remove_competitor(row_id: int) -> bool:
    rid = int(row_id or 0)
    if rid <= 0:
        return False
    try:
        from app.services.postgres_core_service import remove_company_competitor_pg
        return bool(remove_company_competitor_pg(rid))
    except Exception:
        return False


def add_company_note(
    ticker: str,
    note: str,
    action: str = "Note",
    emotion: str = "Calm",
    created_by: str = "human",
) -> bool:
    t = _normalize_ticker(ticker)
    txt = str(note or "").strip()
    if not t or not txt:
        return False
    cb = str(created_by or "human").strip().lower()
    if cb not in {"human", "ai"}:
        cb = "human"
    return add_workspace_journal_note_pg(ticker=t, note=txt, action=action, emotion=emotion, created_by=cb)


def update_company_note(note_id: int, note: str) -> bool:
    rid = int(note_id or 0)
    txt = str(note or "").strip()
    if rid <= 0 or not txt:
        return False
    return update_workspace_journal_note_pg(note_id=rid, note=txt)


def delete_company_note(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    return delete_workspace_journal_note_pg(note_id=rid)


def add_company_task(ticker: str, task: str, due_date: str = "", priority: str = "P2") -> bool:
    t = _normalize_ticker(ticker)
    txt = str(task or "").strip()
    due = str(due_date or "").strip()
    p = str(priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    if not t or not txt:
        return False
    return add_todo_pg(task=txt[:1000], ticker=t, category="company", priority=p, due_date=due)


def toggle_company_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    return toggle_todo_pg(rid)


def delete_company_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    return delete_todo_pg(rid)


def update_company_task(todo_id: int, task: str, due_date: str = "", priority: str = "P2") -> bool:
    rid = int(todo_id or 0)
    txt = str(task or "").strip()
    due = str(due_date or "").strip()
    p = str(priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    if rid <= 0 or not txt:
        return False
    return update_todo_pg(todo_id=rid, task=txt[:1000], due_date=due, priority=p)


def add_company_reminder(ticker: str, remind_at: str, note: str) -> bool:
    t = _normalize_ticker(ticker)
    ra = str(remind_at or "").strip()
    txt = str(note or "").strip()
    if not t or not txt:
        return False
    return add_company_reminder_pg(ticker=t, remind_at=ra[:64], note=txt[:500])


def toggle_company_reminder(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    return toggle_company_reminder_pg(rid)


def update_company_reminder(reminder_id: int, remind_at: str, note: str) -> bool:
    rid = int(reminder_id or 0)
    ra = str(remind_at or "").strip()[:64]
    txt = str(note or "").strip()
    if rid <= 0 or not txt:
        return False
    return update_company_reminder_pg(reminder_id=rid, remind_at=ra, note=txt[:500])


def delete_company_reminder(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    return delete_company_reminder_pg(rid)
