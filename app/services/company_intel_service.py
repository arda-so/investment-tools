from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

from app.services.postgres_core_service import get_company_intel_pg, upsert_company_intel_pg

_CACHE_TTL_SEC = 3600
_MEM_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_EDGAR_IDENTITY_READY = False


def _now_ts() -> float:
    return dt.datetime.now().timestamp()


def _ticker(raw: str) -> str:
    return "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum() or ch in {".", "-"})[:16]


def _empty_row(tk: str) -> dict[str, Any]:
    return {
        "ticker": tk,
        "asof": "",
        "source": "edgartools",
        "revenue_segments": {"product": [], "geography": []},
        "buyback": {"ttm_value": None, "quarters": []},
        "insider_trades": [],
        "status": {"revenue_segments": "unavailable", "buybacks": "unavailable", "insider": "unavailable", "error": ""},
        "updated_at": "",
    }


def _coerce_num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:
            return None
        return x
    except Exception:
        return None


def _is_blank(v: Any) -> bool:
    s = str(v or "").strip()
    if not s:
        return True
    return s.lower() in {"nan", "none", "null", "nat"}


def _clean_segment_label(label: str) -> str:
    s = str(label or "").strip()
    if not s:
        return "Other"
    # Drop namespace prefix (e.g., nvda:DataCenterMember -> DataCenterMember)
    if ":" in s:
        s = s.split(":", 1)[1]
    # Remove common suffix tokens
    s = re.sub(r"(Member|Axis)$", "", s, flags=re.IGNORECASE)
    # Split camel/pascal case into words
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = s.replace("_", " ").replace("-", " ")
    # Rejoin common Apple-style product labels.
    s = re.sub(r"\bi Phone\b", "iPhone", s, flags=re.IGNORECASE)
    s = re.sub(r"\bi Pad\b", "iPad", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] if s else "Other"


def _looks_like_geography_label(label: str) -> bool:
    s = str(label or "").strip().lower()
    if not s or s in {"nan"}:
        return False
    deny_tokens = (
        "summary of",
        "combined",
        "all other",
        "revenue",
        "net sales",
        "operating segment",
        "segments",
        "current",
        "noncurrent",
        "total",
        "service",
        "product",
        "less than",
        "due after",
        "parent company",
        "medicare",
        "securit",
        "debt",
        "marketable",
        "fair value",
        "adjustment",
        "agency",
        "treasury",
        "customer",
        "schedule",
        "specialized market",
        "revenue recognized",
        "advances included",
        "revenues by geographic region",
        "revenue by geographic region",
        "geographic region",
        "segment information",
        "segment reporting",
        "table",
        "schedule",
    )
    if any(t in s for t in deny_tokens):
        return False
    geo_tokens = (
        "u.s",
        "us",
        "united states",
        "u.s. and canada",
        "us and canada",
        "canada",
        "china",
        "taiwan",
        "hong kong",
        "japan",
        "india",
        "europe",
        "european",
        "asia",
        "asia pacific",
        "apac",
        "americas",
        "north america",
        "latin america",
        "international",
        "other countries",
        "rest of world",
        "emea",
        "emeia",
        "middle east",
        "africa",
        "uk",
        "united kingdom",
        "region",
        "country",
    )
    return any(t in s for t in geo_tokens)


def _parse_records(obj: Any) -> list[dict[str, Any]]:
    if obj is None:
        return []
    if isinstance(obj, list):
        return [dict(x) for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        return [dict(obj)]
    try:
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            data = to_dict("records")
            if isinstance(data, list):
                return [dict(x) for x in data if isinstance(x, dict)]
    except Exception:
        pass
    try:
        out: list[dict[str, Any]] = []
        for x in list(obj):  # type: ignore[arg-type]
            if isinstance(x, dict):
                out.append(dict(x))
        return out
    except Exception:
        return []


def _has_raw_segmentation_labels(row: dict[str, Any]) -> bool:
    try:
        seg = dict((row or {}).get("revenue_segments") or {})
        rows = list(seg.get("product") or []) + list(seg.get("geography") or [])
        for r in rows:
            lbl = str((r or {}).get("label") or "").strip()
            if not lbl:
                continue
            # Legacy/raw labels like "us-gaap:ProductMember" or "aapl:IPhoneMember".
            if ":" in lbl or lbl.endswith("Member"):
                return True
    except Exception:
        return False
    return False


def _normalize_segments(segments: dict[str, Any]) -> dict[str, Any]:
    src = dict(segments or {})
    out_product: list[dict[str, Any]] = []
    out_geo: list[dict[str, Any]] = []
    for rr in list(src.get("product") or []):
        if not isinstance(rr, dict):
            continue
        lbl = _clean_segment_label(str(rr.get("label") or ""))
        out_product.append(
            {
                "label": lbl,
                "value": _coerce_num(rr.get("value")),
                "pct": _coerce_num(rr.get("pct")) or 0.0,
            }
        )
    for rr in list(src.get("geography") or []):
        if not isinstance(rr, dict):
            continue
        lbl = _clean_segment_label(str(rr.get("label") or ""))
        if not _looks_like_geography_label(lbl):
            continue
        pct = _coerce_num(rr.get("pct")) or 0.0
        if pct <= 0:
            continue
        out_geo.append(
            {
                "label": lbl,
                "value": _coerce_num(rr.get("value")),
                "pct": pct,
            }
        )
    # Recompute percentages from value when possible for consistency.
    def _re_pct(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        vals = [float(r.get("value") or 0.0) for r in rows if (r.get("value") is not None)]
        total = sum(v for v in vals if v > 0)
        if total <= 0:
            return rows
        out: list[dict[str, Any]] = []
        for r in rows:
            v = float(r.get("value") or 0.0)
            out.append({"label": str(r.get("label") or ""), "value": v, "pct": round((v / total) * 100.0, 2)})
        return out

    out_product = _re_pct(out_product)
    out_geo = _re_pct(out_geo)
    return {"product": out_product[:8], "geography": out_geo[:8]}


def _ensure_edgar_identity() -> str | None:
    global _EDGAR_IDENTITY_READY
    if _EDGAR_IDENTITY_READY:
        return None
    identity = (
        os.getenv("SEC_EDGAR_IDENTITY")
        or os.getenv("EDGAR_IDENTITY")
        or os.getenv("SEC_USER_AGENT")
        or ""
    ).strip()
    if not identity:
        return "missing_sec_identity_env"
    try:
        import edgar  # type: ignore

        set_identity = getattr(edgar, "set_identity", None)
        if callable(set_identity):
            set_identity(identity)
            _EDGAR_IDENTITY_READY = True
            return None
        return "set_identity_unavailable"
    except Exception as exc:
        return f"set_identity_failed:{exc}"


def _latest_filing(company: Any, forms: tuple[str, ...]) -> tuple[Any | None, str]:
    for form in forms:
        try:
            filings = company.get_filings(form=form)
        except Exception as exc:
            return None, f"get_filings_failed:{exc}"
        if filings is None:
            continue
        if hasattr(filings, "latest"):
            try:
                one = filings.latest()
                if one is not None:
                    if isinstance(one, list):
                        return (one[0] if one else None), ("ok" if one else "no_filing")
                    return one, "ok"
            except Exception:
                try:
                    one = filings.latest(1)
                    if isinstance(one, list):
                        return (one[0] if one else None), ("ok" if one else "no_filing")
                    if one is not None:
                        return one, "ok"
                except Exception:
                    pass
        if isinstance(filings, list) and filings:
            return filings[0], "ok"
        if hasattr(filings, "__iter__"):
            try:
                items = list(filings)
                if items:
                    return items[0], "ok"
            except Exception:
                continue
    return None, "no_filing"


def _extract_revenue_segments(company: Any) -> tuple[dict[str, Any], str]:
    status = "unavailable"
    out = {"product": [], "geography": []}
    try:
        filing, filing_status = _latest_filing(company, ("10-K", "20-F", "40-F"))
        if filing is None:
            return out, filing_status
        xbrl = None
        for meth in ("xbrl", "get_xbrl", "to_xbrl"):
            fn = getattr(filing, meth, None)
            if callable(fn):
                try:
                    xbrl = fn()
                    if xbrl is not None:
                        break
                except Exception:
                    continue
        if xbrl is None:
            return out, "xbrl_missing"
        facts = getattr(xbrl, "facts", None) or xbrl
        by_dim = getattr(facts, "by_dimension", None)
        pivot_by_dimension = getattr(facts, "pivot_by_dimension", None)
        get_facts_with_dimensions = getattr(facts, "get_facts_with_dimensions", None)

        def _dim_extract(dim_key: str) -> list[dict[str, Any]]:
            recs: list[dict[str, Any]] = []
            used_pivot = False
            if callable(by_dim):
                try:
                    recs = _parse_records(by_dim(dim_key))
                except Exception:
                    recs = []
            if not recs and callable(pivot_by_dimension):
                try:
                    piv = _parse_records(pivot_by_dimension(dim_key, concept_pattern="Revenue|Sales"))
                except Exception:
                    piv = []
                # Prefer pivot output when present; it is dimension-oriented and cleaner.
                if piv:
                    recs = piv
                    used_pivot = True
            if not recs and callable(get_facts_with_dimensions):
                try:
                    all_recs = _parse_records(get_facts_with_dimensions())
                    needle = dim_key.lower().replace("axis", "")
                    recs = [
                        r
                        for r in all_recs
                        if needle in str(r.get("dimension") or r.get("dimension_label") or "").lower()
                    ]
                except Exception:
                    recs = []
            # Last-resort fallback: use all facts rows and match dynamic dimension columns.
            if not recs:
                to_dataframe = getattr(facts, "to_dataframe", None)
                if callable(to_dataframe):
                    try:
                        recs = _parse_records(to_dataframe())
                    except Exception:
                        recs = []
            agg: dict[str, float] = {}
            needle = dim_key.lower().replace("axis", "")
            is_geo_dim = "geograph" in needle
            meta_cols = {
                "concept",
                "name",
                "label",
                "member",
                "dimension",
                "dimension_label",
                "dimension_value",
                "segment",
                "period",
                "period_end",
                "end",
                "date",
                "period_key",
                "unit",
                "decimals",
                "fy",
                "fp",
                "form",
                "accn",
                "filed",
                "frame",
            }
            for r in recs:
                concept = str(r.get("concept") or r.get("name") or "").lower()
                if "revenue" not in concept and "sales" not in concept:
                    continue
                label = str(r.get("label") or r.get("member") or r.get("dimension_value") or r.get("segment") or "").strip()
                if not label:
                    for k, v in r.items():
                        kk = str(k or "").lower()
                        if not kk.startswith("dim_"):
                            continue
                        if needle in kk or ("geograph" in needle and "geograph" in kk) or ("product" in needle and "product" in kk):
                            if _is_blank(v):
                                continue
                            label = str(v).strip()
                            break
                if not label:
                    label = "Other"
                value = _coerce_num(r.get("value"))
                if value is None:
                    value = _coerce_num(r.get("numeric_value"))
                # Pivot-style rows: numeric values live in dynamic member columns.
                if value is None:
                    for k, v in r.items():
                        kk = str(k or "").lower()
                        if kk in meta_cols:
                            continue
                        if kk.startswith("dim_"):
                            continue
                        cand = _coerce_num(v)
                        if cand is not None:
                            value = cand
                            break
                # Some XBRL pivot rows place each geography member in its own column
                # (e.g., Americas/Europe/APAC). Expand those columns directly.
                if is_geo_dim and used_pivot:
                    expanded = False
                    for k, v in r.items():
                        kk = str(k or "").strip()
                        low = kk.lower()
                        if not kk or low in meta_cols or low.startswith("dim_") or re.match(r"^\d{4}$", kk):
                            continue
                        cand = _coerce_num(v)
                        if cand is None:
                            continue
                        member_lbl = _clean_segment_label(kk)
                        if not _looks_like_geography_label(member_lbl):
                            continue
                        agg[member_lbl] = agg.get(member_lbl, 0.0) + float(cand)
                        expanded = True
                    if expanded:
                        continue
                if not label or value is None:
                    continue
                key = _clean_segment_label(label)
                if is_geo_dim and not _looks_like_geography_label(key):
                    continue
                agg[key] = agg.get(key, 0.0) + float(value)
            total = sum(v for v in agg.values() if v and v > 0)
            rows: list[dict[str, Any]] = []
            for k, v in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:12]:
                pct = (v / total * 100.0) if total > 0 else 0.0
                rows.append({"label": k[:60], "value": float(v), "pct": round(pct, 2)})
            return rows

        product = (
            _dim_extract("ProductOrServiceAxis")
            or _dim_extract("srt_ProductOrServiceAxis")
            or _dim_extract("ProductAxis")
        )
        geo = (
            _dim_extract("GeographicalAreasAxis")
            or _dim_extract("srt_StatementGeographicalAxis")
            or _dim_extract("GeographyAxis")
        )
        out = {"product": product[:8], "geography": geo[:8]}
        status = "ok" if (out["product"] or out["geography"]) else "empty"
    except Exception as exc:
        status = f"error:{exc}"
    return out, status


def _extract_buybacks(company: Any) -> tuple[dict[str, Any], str]:
    out = {"ttm_value": None, "quarters": []}
    try:
        filing, filing_status = _latest_filing(company, ("10-K", "20-F", "40-F"))
        if filing is None:
            return out, filing_status
        xbrl = None
        for meth in ("xbrl", "get_xbrl", "to_xbrl"):
            fn = getattr(filing, meth, None)
            if callable(fn):
                try:
                    xbrl = fn()
                    if xbrl is not None:
                        break
                except Exception:
                    continue
        if xbrl is None:
            return out, "xbrl_missing"
        facts = getattr(xbrl, "facts", None) or xbrl
        by_concept = getattr(facts, "by_concept", None)
        get_facts_by_concept = getattr(facts, "get_facts_by_concept", None)

        concepts = [
            "TreasuryStockAcquiredDuringPeriodValue",
            "PaymentsForRepurchaseOfCommonStock",
            "RepurchaseOfCommonStock",
        ]
        recs: list[dict[str, Any]] = []
        for c in concepts:
            if callable(by_concept):
                try:
                    recs = _parse_records(by_concept(c))
                except Exception:
                    recs = []
            if not recs and callable(get_facts_by_concept):
                try:
                    recs = _parse_records(get_facts_by_concept(c, exact=False))
                except Exception:
                    recs = []
            if recs:
                break
        if not recs:
            return out, "concept_missing"
        rows: list[dict[str, Any]] = []
        for r in recs:
            val = _coerce_num(r.get("value"))
            if val is None:
                val = _coerce_num(r.get("numeric_value"))
            if val is None:
                continue
            period = str(
                r.get("period")
                or r.get("period_end")
                or r.get("end")
                or r.get("date")
                or r.get("period_key")
                or ""
            ).strip()[:20]
            rows.append({"period": period or "-", "value": float(val)})
        # Deduplicate same period from multiple XBRL contexts/taxonomy mappings.
        # Keep the maximum absolute value for each period to avoid double-counting.
        dedup: dict[str, float] = {}
        for rr in rows:
            p = str(rr.get("period") or "").strip() or "-"
            v = float(rr.get("value") or 0.0)
            prev = dedup.get(p)
            if prev is None or abs(v) > abs(prev):
                dedup[p] = v
        rows = [{"period": p, "value": float(v)} for p, v in dedup.items()]
        rows = sorted(rows, key=lambda x: str(x.get("period") or ""), reverse=True)[:4]
        ttm = sum(float(x.get("value") or 0.0) for x in rows)
        out = {"ttm_value": round(ttm, 2), "quarters": list(reversed(rows))}
        return out, "ok" if rows else "empty"
    except Exception as exc:
        return out, f"error:{exc}"


def _extract_insider_trades(company: Any) -> tuple[list[dict[str, Any]], str]:
    out: list[dict[str, Any]] = []
    try:
        filings = company.get_filings(form="4")
        items: list[Any] = []
        if isinstance(filings, list):
            items = filings[:20]
        elif hasattr(filings, "__iter__"):
            try:
                items = list(filings)[:20]
            except Exception:
                items = []
        for f in items:
            owner = str(getattr(f, "reporting_owner", None) or getattr(f, "owner", None) or "").strip()
            title = str(getattr(f, "reporting_owner_title", None) or getattr(f, "owner_title", None) or "").strip()
            date = str(getattr(f, "filing_date", None) or getattr(f, "date", None) or "").strip()
            tx_type = str(getattr(f, "transaction_type", None) or getattr(f, "code", None) or "").strip().upper()
            net = _coerce_num(getattr(f, "net_shares", None) or getattr(f, "shares", None))
            if not owner and hasattr(f, "to_dict"):
                try:
                    d = dict(f.to_dict())
                    owner = str(d.get("reporting_owner") or d.get("owner") or owner).strip()
                    title = str(d.get("title") or d.get("owner_title") or title).strip()
                    date = str(d.get("filing_date") or d.get("date") or date).strip()
                    tx_type = str(d.get("transaction_type") or d.get("code") or tx_type).strip().upper()
                    net = _coerce_num(d.get("net_shares") or d.get("shares") or net)
                except Exception:
                    pass
            if not owner:
                continue
            signed = float(net or 0.0)
            if tx_type in {"S", "SELL"} and signed > 0:
                signed = -signed
            if tx_type in {"P", "BUY"} and signed < 0:
                signed = -signed
            tx_label = "BUY" if signed > 0 else ("SELL" if signed < 0 else "OTHER")
            out.append(
                {
                    "owner": owner[:80],
                    "title": title[:80],
                    "date": date[:20],
                    "tx_type": tx_label,
                    "net_shares": round(signed, 2),
                }
            )
        out = out[:12]
        return out, ("ok" if out else "empty")
    except Exception as exc:
        return out, f"error:{exc}"


def refresh_company_intel(ticker: str) -> dict[str, Any]:
    tk = _ticker(ticker)
    if not tk:
        return {}
    source = "edgartools"
    status = {"revenue_segments": "unavailable", "buybacks": "unavailable", "insider": "unavailable", "error": ""}
    segments = {"product": [], "geography": []}
    buyback = {"ttm_value": None, "quarters": []}
    insider: list[dict[str, Any]] = []
    try:
        from edgar import Company  # type: ignore
    except Exception as exc:
        status["error"] = f"edgartools_unavailable:{exc}"
        row = {
            "ticker": tk,
            "asof": dt.datetime.now().date().isoformat(),
            "source": source,
            "revenue_segments": segments,
            "buyback": buyback,
            "insider_trades": insider,
            "status": status,
            "updated_at": dt.datetime.now().isoformat(),
        }
        _MEM_CACHE[tk] = (_now_ts(), dict(row))
        upsert_company_intel_pg(
            ticker=tk,
            asof=row["asof"],
            source=source,
            revenue_segments=segments,
            buyback=buyback,
            insider_trades=insider,
            status=status,
        )
        return row

    identity_err = _ensure_edgar_identity()
    if identity_err:
        status["error"] = identity_err
        row = {
            "ticker": tk,
            "asof": dt.datetime.now().date().isoformat(),
            "source": source,
            "revenue_segments": segments,
            "buyback": buyback,
            "insider_trades": insider,
            "status": status,
            "updated_at": dt.datetime.now().isoformat(),
        }
        _MEM_CACHE[tk] = (_now_ts(), dict(row))
        upsert_company_intel_pg(
            ticker=tk,
            asof=row["asof"],
            source=source,
            revenue_segments=segments,
            buyback=buyback,
            insider_trades=insider,
            status=status,
        )
        return row

    company = None
    try:
        company = Company(tk)
    except Exception as exc:
        status["error"] = f"company_lookup_failed:{exc}"
        company = None
    if company is None:
        if not status["error"]:
            status["error"] = "company_lookup_failed"
    else:
        segments, status["revenue_segments"] = _extract_revenue_segments(company)
        buyback, status["buybacks"] = _extract_buybacks(company)
        insider, status["insider"] = _extract_insider_trades(company)
    segments = _normalize_segments(segments)
    row = {
        "ticker": tk,
        "asof": dt.datetime.now().date().isoformat(),
        "source": source,
        "revenue_segments": segments,
        "buyback": buyback,
        "insider_trades": insider,
        "status": status,
        "updated_at": dt.datetime.now().isoformat(),
    }
    _MEM_CACHE[tk] = (_now_ts(), dict(row))
    upsert_company_intel_pg(
        ticker=tk,
        asof=row["asof"],
        source=source,
        revenue_segments=segments,
        buyback=buyback,
        insider_trades=insider,
        status=status,
    )
    return row


def get_company_intel(ticker: str, *, refresh: bool = False) -> dict[str, Any]:
    tk = _ticker(ticker)
    if not tk:
        return {}
    if refresh:
        return refresh_company_intel(tk)
    now = _now_ts()
    # Prefer DB as source of truth to avoid stale in-memory segment views.
    db = get_company_intel_pg(tk)
    if db:
        db["revenue_segments"] = _normalize_segments(dict(db.get("revenue_segments") or {}))
        _MEM_CACHE[tk] = (now, dict(db))
        return db
    # Fallback to cache only when DB has no row.
    hit = _MEM_CACHE.get(tk)
    if hit and now - hit[0] <= _CACHE_TTL_SEC:
        cached = dict(hit[1] or {})
        if not _has_raw_segmentation_labels(cached):
            return cached
    return _empty_row(tk)


def maybe_refresh_company_intel_on_filing(
    ticker: str,
    form: str,
    filing_date: str = "",
    *,
    max_stale_days: int = 14,
) -> dict[str, Any]:
    """
    Auto-refresh helper for filing ingestion.
    Refresh only when:
    - form is annual/quarterly filing, and
    - intel is missing or stale.
    """
    tk = _ticker(ticker)
    fm = str(form or "").strip().upper()
    if not tk:
        return {}
    if fm not in {"10-K", "10-Q", "20-F", "40-F"}:
        return get_company_intel(tk, refresh=False)

    cur = get_company_intel(tk, refresh=False)
    seg = dict((cur or {}).get("revenue_segments") or {})
    buy = dict((cur or {}).get("buyback") or {})
    has_any = bool(seg.get("product") or seg.get("geography") or buy.get("ttm_value") is not None or buy.get("quarters"))

    asof_s = str((cur or {}).get("asof") or "").strip()
    stale = True
    if asof_s:
        try:
            asof_d = dt.date.fromisoformat(asof_s[:10])
            stale = (dt.date.today() - asof_d).days > max(1, int(max_stale_days or 14))
        except Exception:
            stale = True

    # If filing_date is newer than asof, treat as stale.
    fd = str(filing_date or "").strip()
    if fd and asof_s:
        try:
            fdd = dt.date.fromisoformat(fd[:10])
            asof_d2 = dt.date.fromisoformat(asof_s[:10])
            if fdd > asof_d2:
                stale = True
        except Exception:
            pass

    if has_any and not stale:
        return cur
    return refresh_company_intel(tk)
