"""financial_lab_service.py — Data assembly + scoring for the Financial Laboratory."""
from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


# ── Lab context assembly ─────────────────────────────────────────────────────

def get_lab_context(ticker: str) -> dict[str, Any]:
    """Assemble all data needed for the Financial Lab page."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {"ticker": tk, "error": "no ticker"}

    financials = _get_financials(tk)
    intel = _get_intel(tk)
    price = _get_price(tk)
    market_cap, current_price, shares_out = _get_yfinance_basics(tk)

    # Derive key metrics
    years = financials.get("years_json") or []
    fcf_list = financials.get("free_cf") or []
    revenue_list = financials.get("revenue") or []
    net_income_list = financials.get("net_income") or []
    equity_list = financials.get("stockholders_equity") or []
    debt_to_equity_list = financials.get("debt_to_equity") or []
    gross_profit_list = financials.get("gross_profit") or []

    latest_fcf = _last_valid(fcf_list)
    latest_revenue = _last_valid(revenue_list)
    latest_net_income = _last_valid(net_income_list)
    latest_equity = _last_valid(equity_list)

    fcf_per_share = (latest_fcf / shares_out) if shares_out and latest_fcf else None
    pe_ratio = (current_price / (latest_net_income / shares_out)) if shares_out and latest_net_income and current_price else None
    latest_de = _last_valid(debt_to_equity_list)

    # Revenue CAGR (5yr)
    rev_cagr = _cagr(revenue_list)

    # Insider conviction scoring
    insider_conviction = score_insider_conviction(tk, intel)

    # ROIC vs WACC
    roic_wacc = compute_roic_wacc(tk, financials)

    return {
        "ticker": tk,
        "market_cap": market_cap,
        "current_price": current_price,
        "shares_outstanding": shares_out,
        "fcf_per_share": fcf_per_share,
        "pe_ratio": pe_ratio,
        "debt_to_equity": latest_de,
        "rev_cagr_5yr": rev_cagr,
        "latest_fcf": latest_fcf,
        "latest_revenue": latest_revenue,
        "latest_net_income": latest_net_income,
        "latest_equity": latest_equity,
        "financials": {
            "years": years,
            "revenue": revenue_list,
            "gross_profit": gross_profit_list,
            "net_income": net_income_list,
            "free_cf": fcf_list,
            "stockholders_equity": equity_list,
            "debt_to_equity": debt_to_equity_list,
            "operating_cf": financials.get("operating_cf") or [],
            "capex": financials.get("capex") or [],
            "total_assets": financials.get("total_assets") or [],
            "total_liabilities": financials.get("total_liabilities") or [],
            "revenue_growth": financials.get("revenue_growth") or [],
        },
        "intel": {
            "segments": intel.get("revenue_segments") or {},
            "buyback": intel.get("buyback") or {},
            "executives": intel.get("executives") or [],
            "insider_trades": intel.get("insider_trades") or [],
        },
        "insider_conviction": insider_conviction,
        "roic_wacc": roic_wacc,
        "price": {
            "ytd_return": price.get("ytd_return"),
            "m12_return": price.get("m12_return"),
            "y5_return": price.get("y5_return"),
        },
    }


# ── Buffett / Munger checklist ───────────────────────────────────────────────

def score_buffett_checklist(ticker: str, ctx: dict | None = None) -> dict:
    """Auto-score 10 Buffett/Munger criteria. Returns {score, max, items}."""
    if ctx is None:
        ctx = get_lab_context(ticker)

    fin = ctx.get("financials") or {}
    intel = ctx.get("intel") or {}
    items: list[dict] = []

    revenue = fin.get("revenue") or []
    gp = fin.get("gross_profit") or []
    fcf = fin.get("free_cf") or []
    de = fin.get("debt_to_equity") or []
    ni = fin.get("net_income") or []
    equity = fin.get("stockholders_equity") or []

    # 1. Revenue growth > 5% CAGR
    cagr = _cagr(revenue)
    items.append({
        "label": "Revenue CAGR > 5%",
        "pass": cagr is not None and cagr > 5,
        "detail": f"{cagr:.1f}%" if cagr is not None else "N/A",
    })

    # 2. Stable/expanding gross margins
    gm_pass, gm_detail = _check_margin_stability(revenue, gp)
    items.append({"label": "Stable/expanding gross margins", "pass": gm_pass, "detail": gm_detail})

    # 3. FCF positive every year
    valid_fcf = [f for f in fcf if f is not None]
    all_positive = len(valid_fcf) >= 3 and all(f > 0 for f in valid_fcf)
    items.append({
        "label": "FCF positive every year",
        "pass": all_positive,
        "detail": f"{len([f for f in valid_fcf if f > 0])}/{len(valid_fcf)} years positive",
    })

    # 4. Debt/Equity < 1.0
    latest_de = _last_valid(de)
    items.append({
        "label": "Debt/Equity < 1.0",
        "pass": latest_de is not None and latest_de < 1.0,
        "detail": f"{latest_de:.2f}" if latest_de is not None else "N/A",
    })

    # 5. ROE > 15%
    roe = _compute_roe(ni, equity)
    items.append({
        "label": "ROE > 15%",
        "pass": roe is not None and roe > 15,
        "detail": f"{roe:.1f}%" if roe is not None else "N/A",
    })

    # 6. Buyback program active
    buyback = intel.get("buyback") or {}
    ttm_val = buyback.get("ttm_value")
    has_buyback = ttm_val is not None and ttm_val > 0
    items.append({
        "label": "Active buyback program",
        "pass": has_buyback,
        "detail": f"${_fmt_money(ttm_val)} TTM" if has_buyback else "None detected",
    })

    # 7. Revenue concentration < 50% single segment
    segments = intel.get("segments") or {}
    product_segs = segments.get("product") or []
    max_pct = max((s.get("pct_revenue") or 0 for s in product_segs), default=0)
    diversified = len(product_segs) >= 2 and max_pct < 50
    items.append({
        "label": "Revenue diversification (no >50% segment)",
        "pass": diversified,
        "detail": f"Largest segment: {max_pct:.0f}%" if product_segs else "No segment data",
    })

    # 8. Geographic diversification
    geo_segs = segments.get("geography") or []
    geo_regions = [s for s in geo_segs if (s.get("pct_revenue") or 0) > 20]
    geo_pass = len(geo_regions) >= 2
    items.append({
        "label": "Geographic diversification (>1 region >20%)",
        "pass": geo_pass,
        "detail": f"{len(geo_regions)} regions >20%" if geo_segs else "No geo data",
    })

    # 9. FCF/Revenue > 10%
    latest_fcf = _last_valid(fcf)
    latest_rev = _last_valid(revenue)
    fcf_margin = (latest_fcf / latest_rev * 100) if latest_fcf and latest_rev else None
    items.append({
        "label": "FCF margin > 10%",
        "pass": fcf_margin is not None and fcf_margin > 10,
        "detail": f"{fcf_margin:.1f}%" if fcf_margin is not None else "N/A",
    })

    # 10. No excessive dilution (equity not growing faster than earnings)
    eq_cagr = _cagr(equity)
    ni_cagr = _cagr(ni)
    dilution_ok = True
    dilution_detail = "N/A"
    if eq_cagr is not None and ni_cagr is not None:
        dilution_ok = eq_cagr <= ni_cagr + 3  # allow 3pp buffer
        dilution_detail = f"Equity CAGR {eq_cagr:.1f}% vs Earnings CAGR {ni_cagr:.1f}%"
    items.append({
        "label": "No excessive dilution",
        "pass": dilution_ok,
        "detail": dilution_detail,
    })

    score = sum(1 for it in items if it["pass"])
    return {"score": score, "max": len(items), "items": items}


# ── Insider Conviction Scoring ────────────────────────────────────────────────

def score_insider_conviction(ticker: str, intel: dict | None = None) -> dict:
    """Score insider trades for conviction (0-100). Returns {score, label, trades, summary}."""
    if intel is None:
        intel = _get_intel(ticker)
    trades = intel.get("insider_trades") or []
    if not trades:
        return {"score": 50, "label": "Neutral", "trades": [], "summary": "No insider trade data available"}

    buys = [t for t in trades if t.get("tx_type") == "BUY"]
    sells = [t for t in trades if t.get("tx_type") == "SELL"]
    total = len(buys) + len(sells)
    score = 50  # start neutral

    # Net buy ratio: more buys than sells = bullish (+30pts max)
    if total > 0:
        buy_ratio = len(buys) / total
        score += int((buy_ratio - 0.5) * 60)  # -30 to +30

    # Executive trades weighted 2x vs directors (+20pts max)
    exec_keywords = {"ceo", "cfo", "president", "chief", "officer"}
    exec_buys = sum(1 for t in buys if any(k in (t.get("title") or "").lower() for k in exec_keywords))
    exec_sells = sum(1 for t in sells if any(k in (t.get("title") or "").lower() for k in exec_keywords))
    if exec_buys > exec_sells:
        score += min(20, (exec_buys - exec_sells) * 10)
    elif exec_sells > exec_buys:
        score -= min(20, (exec_sells - exec_buys) * 10)

    # Recency: trades within 90 days (+20pts max)
    now = datetime.utcnow()
    recent_buys = 0
    recent_sells = 0
    for t in trades:
        try:
            d = datetime.strptime(t.get("date", "")[:10], "%Y-%m-%d")
            if (now - d).days <= 90:
                if t.get("tx_type") == "BUY":
                    recent_buys += 1
                elif t.get("tx_type") == "SELL":
                    recent_sells += 1
        except (ValueError, TypeError):
            pass
    if recent_buys > recent_sells:
        score += min(20, (recent_buys - recent_sells) * 7)
    elif recent_sells > recent_buys:
        score -= min(20, (recent_sells - recent_buys) * 7)

    # Cluster detection: 3+ buys within 30 days = strong signal (+30pts)
    buy_dates = []
    for t in buys:
        try:
            buy_dates.append(datetime.strptime(t.get("date", "")[:10], "%Y-%m-%d"))
        except (ValueError, TypeError):
            pass
    buy_dates.sort()
    cluster_found = False
    for i in range(len(buy_dates) - 2):
        if (buy_dates[i + 2] - buy_dates[i]).days <= 30:
            cluster_found = True
            break
    if cluster_found:
        score += 30

    score = max(0, min(100, score))
    label = "Bullish" if score >= 60 else ("Bearish" if score < 30 else "Neutral")

    parts = []
    parts.append(f"{len(buys)} buys, {len(sells)} sells")
    if cluster_found:
        parts.append("buy cluster detected")
    if recent_buys > 0:
        parts.append(f"{recent_buys} recent buys (90d)")
    summary = "; ".join(parts)

    return {"score": score, "label": label, "trades": trades[:12], "summary": summary}


# ── ROIC vs WACC ─────────────────────────────────────────────────────────────

def compute_roic_wacc(ticker: str, financials: dict | None = None) -> dict:
    """Compute ROIC, WACC, and value-creation spread. Fetches operating income from yfinance."""
    result = {"roic": None, "wacc": None, "spread": None, "creates_value": None, "details": {}}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # Get operating income and tax info from income statement
        inc = t.income_stmt
        operating_income = None
        tax_rate = 0.21  # default
        if inc is not None and not inc.empty:
            for label in ["Operating Income", "EBIT"]:
                if label in inc.index:
                    vals = inc.loc[label].dropna()
                    if len(vals):
                        operating_income = float(vals.iloc[0])
                        break
            # Effective tax rate
            for tax_label in ["Tax Provision", "Income Tax Expense"]:
                if tax_label in inc.index:
                    tax_vals = inc.loc[tax_label].dropna()
                    if len(tax_vals):
                        tax_expense = float(tax_vals.iloc[0])
                        break
            else:
                tax_expense = None
            for pretax_label in ["Pretax Income", "Income Before Tax"]:
                if pretax_label in inc.index:
                    pt_vals = inc.loc[pretax_label].dropna()
                    if len(pt_vals):
                        pretax = float(pt_vals.iloc[0])
                        if pretax > 0 and tax_expense is not None:
                            tax_rate = max(0, min(0.5, tax_expense / pretax))
                        break

        if operating_income is None:
            return result

        # Beta
        beta = getattr(t.fast_info, "beta", None) or (t.info or {}).get("beta") or 1.0

        # From mini_statements or financials dict
        if financials is None:
            financials = _get_financials(ticker)

        total_debt_list = financials.get("total_debt") or []
        total_equity_list = financials.get("total_equity") or financials.get("stockholders_equity") or []
        total_cash_list = financials.get("total_cash") or []

        # Parse JSON strings if needed
        if isinstance(total_debt_list, str):
            try: total_debt_list = json.loads(total_debt_list)
            except Exception: total_debt_list = []
        if isinstance(total_equity_list, str):
            try: total_equity_list = json.loads(total_equity_list)
            except Exception: total_equity_list = []
        if isinstance(total_cash_list, str):
            try: total_cash_list = json.loads(total_cash_list)
            except Exception: total_cash_list = []

        total_debt = _last_valid(total_debt_list)
        total_equity = _last_valid(total_equity_list)
        total_cash = _last_valid(total_cash_list)

        if total_equity is None or total_debt is None:
            return result
        total_cash = total_cash or 0

        # ROIC = NOPAT / Invested Capital
        nopat = operating_income * (1 - tax_rate)
        invested_capital = total_equity + total_debt - total_cash
        if invested_capital <= 0:
            return result
        roic = nopat / invested_capital

        # WACC
        risk_free = 0.045
        erp = 0.055
        cost_of_equity = risk_free + float(beta) * erp

        # Cost of debt: try interest expense / total debt
        cost_of_debt = 0.05  # default
        if inc is not None and not inc.empty:
            for ie_label in ["Interest Expense", "Interest Expense Non Operating"]:
                if ie_label in inc.index:
                    ie_vals = inc.loc[ie_label].dropna()
                    if len(ie_vals) and total_debt > 0:
                        ie = abs(float(ie_vals.iloc[0]))
                        cost_of_debt = min(0.15, ie / total_debt)
                        break

        total_capital = total_equity + total_debt
        if total_capital <= 0:
            return result
        weight_equity = total_equity / total_capital
        weight_debt = total_debt / total_capital
        wacc = cost_of_equity * weight_equity + cost_of_debt * (1 - tax_rate) * weight_debt

        spread = roic - wacc
        result = {
            "roic": round(roic * 100, 2),
            "wacc": round(wacc * 100, 2),
            "spread": round(spread * 100, 2),
            "creates_value": spread > 0,
            "details": {
                "nopat": round(nopat),
                "invested_capital": round(invested_capital),
                "cost_of_equity": round(cost_of_equity * 100, 2),
                "cost_of_debt": round(cost_of_debt * 100, 2),
                "tax_rate": round(tax_rate * 100, 1),
                "beta": round(float(beta), 2),
                "weight_equity": round(weight_equity * 100, 1),
                "weight_debt": round(weight_debt * 100, 1),
            },
        }
    except Exception:
        log.exception("Failed to compute ROIC/WACC for %s", ticker)
    return result


# ── Case study CRUD ──────────────────────────────────────────────────────────

def save_case_study(ticker: str, title: str, module: str,
                    params: dict, result: dict, notes: str = "") -> int:
    from app.services.postgres_core_service import save_lab_case_study_pg
    tk = str(ticker or "").strip().upper()
    return save_lab_case_study_pg(
        tk, title, module,
        json.dumps(params), json.dumps(result), notes,
    )


def list_case_studies(ticker: str) -> list[dict]:
    from app.services.postgres_core_service import list_lab_case_studies_pg
    tk = str(ticker or "").strip().upper()
    return list_lab_case_studies_pg(tk)


def delete_case_study(case_id: int) -> bool:
    from app.services.postgres_core_service import delete_lab_case_study_pg
    return delete_lab_case_study_pg(case_id)


# ── Risk Factors / MD&A Diff Tracker ─────────────────────────────────────


def diff_risk_factors(ticker: str) -> dict:
    """Compare risk_factors and mda sections between consecutive same-type filings."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {"available": False, "reason": "No ticker provided"}

    filings = _query_filings_for_ticker(tk, ["10-K", "10-Q", "20-F"])
    if len(filings) < 2:
        return {"available": False, "reason": "Need 2+ filings of same type"}

    # Group by form type and find first pair (latest + predecessor of same type)
    from collections import defaultdict
    by_form: dict[str, list] = defaultdict(list)
    for f in filings:
        by_form[f["form"]].append(f)

    current = None
    previous = None
    for form_type in ["10-K", "20-F", "10-Q"]:  # prefer annual filings
        group = by_form.get(form_type, [])
        if len(group) >= 2:
            current = group[0]
            previous = group[1]
            break

    if not current or not previous:
        return {"available": False, "reason": "Need 2+ filings of same type"}

    # Read filing text
    try:
        from app.core.filing_text import read_filing_text_any
        current_text = read_filing_text_any(current["path"], max_chars=220000) or ""
        previous_text = read_filing_text_any(previous["path"], max_chars=220000) or ""
    except Exception:
        log.exception("Failed to read filing text for %s", tk)
        return {"available": False, "reason": "Could not read filing text"}

    if not current_text or not previous_text:
        return {"available": False, "reason": "Filing text is empty"}

    # Extract sections using our own TOC-aware extractor
    from app.core.filing_text import extract_risk_mda_sections
    cur_sections = extract_risk_mda_sections(current["form"], current_text)
    prev_sections = extract_risk_mda_sections(previous["form"], previous_text)

    result = {
        "available": True,
        "current": {"form": current["form"], "date": current["date"], "accession": current["accession"]},
        "previous": {"form": previous["form"], "date": previous["date"], "accession": previous["accession"]},
    }

    for section_key in ("risk_factors", "mda"):
        cur_text = (cur_sections.get(section_key) or "").strip()
        prev_text = (prev_sections.get(section_key) or "").strip()
        if cur_text or prev_text:
            result[section_key] = _word_diff(prev_text, cur_text)
        else:
            result[section_key] = {"diff_blocks": [], "words_added": 0, "words_removed": 0, "pct_changed": 0.0}

    return result


# _extract_risk_mda_sections — moved to app/core/filing_text.py as extract_risk_mda_sections()


def _query_filings_for_ticker(ticker: str, forms: list[str]) -> list[dict]:
    """Query filings_core for filings of given forms, ordered by date DESC."""
    try:
        from app.services.postgres_core_service import pg_connect
        con = pg_connect()
        if con is None:
            return []
        cur = con.cursor()
        cur.execute(
            """SELECT id, form, date, accession, path FROM filings_core
               WHERE ticker=%s AND form = ANY(%s)
               ORDER BY date DESC""",
            (ticker, forms),
        )
        rows = cur.fetchall() or []
        con.close()
        return [
            {"id": r[0], "form": r[1], "date": r[2], "accession": r[3], "path": r[4]}
            for r in rows
        ]
    except Exception:
        log.exception("Failed to query filings for %s", ticker)
        return []


def _word_diff(old_text: str, new_text: str) -> dict:
    """Word-level diff returning blocks of equal/insert/delete with stats."""
    old_words = old_text.split()
    new_words = new_text.split()

    sm = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    opcodes = sm.get_opcodes()

    blocks: list[dict] = []
    words_added = 0
    words_removed = 0

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            blocks.append({"type": "equal", "text": " ".join(old_words[i1:i2])})
        elif tag == "insert":
            text = " ".join(new_words[j1:j2])
            blocks.append({"type": "insert", "text": text})
            words_added += j2 - j1
        elif tag == "delete":
            text = " ".join(old_words[i1:i2])
            blocks.append({"type": "delete", "text": text})
            words_removed += i2 - i1
        elif tag == "replace":
            blocks.append({"type": "delete", "text": " ".join(old_words[i1:i2])})
            blocks.append({"type": "insert", "text": " ".join(new_words[j1:j2])})
            words_removed += i2 - i1
            words_added += j2 - j1

    total_words = max(len(old_words), len(new_words), 1)
    pct_changed = round((words_added + words_removed) / total_words * 100, 1)

    return {
        "diff_blocks": blocks,
        "words_added": words_added,
        "words_removed": words_removed,
        "pct_changed": pct_changed,
    }


# ── AI Scenario Stress Test ──────────────────────────────────────────────────

_SHOCK_VECTORS = [
    "Inflation / Cost of Capital",
    "Supply Chain / Logistics",
    "Geopolitical / War",
    "Energy / Commodity Spikes",
    "Trade / Tariffs",
    "Regulatory / Antitrust",
]


def simulate_scenario(ticker: str, scenario: str) -> dict:
    """AI-powered scenario stress test using 10-K risk factors + LLM."""
    tk = str(ticker or "").strip().upper()
    if not tk or not scenario.strip():
        return {"ok": False, "error": "Ticker and scenario are required."}

    # 1. Get sector/industry from yfinance
    sector, industry = "Unknown", "Unknown"
    try:
        import yfinance as yf
        t = yf.Ticker(tk)
        info = t.info or {}
        sector = info.get("sector") or "Unknown"
        industry = info.get("industry") or "Unknown"
    except Exception:
        log.warning("Could not fetch sector/industry for %s", tk)

    # 2. Get 10-K risk factors (keep small for local LLM)
    risk_factors_text = ""
    try:
        filings = _query_filings_for_ticker(tk, ["10-K", "20-F"])
        if filings:
            from app.core.filing_text import read_filing_text_any
            raw = read_filing_text_any(filings[0]["path"], max_chars=60000) or ""
            if raw:
                from app.core.filing_text import extract_risk_mda_sections
                sections = extract_risk_mda_sections(filings[0]["form"], raw)
                risk_factors_text = (sections.get("risk_factors") or "")[:2000]
    except Exception:
        log.warning("Could not read 10-K risk factors for %s", tk)

    if not risk_factors_text:
        risk_factors_text = "(No 10-K risk factors available)"

    # 3. Build LLM prompt — kept compact for local models
    vectors_str = ", ".join(_SHOCK_VECTORS)
    prompt = f"""You are a risk analyst. Scenario: "{scenario}".
Company: {tk} ({sector} / {industry}).
10-K risk factors excerpt: {risk_factors_text}

Output ONLY valid JSON:
{{"vulnerability_score": 1-10, "revenue_impact_pct": number, "margin_compression_bps": number, "wacc_increase_bps": number, "shock_vectors": [from: {vectors_str}], "rationale": "2 sentences"}}"""

    schema = {
        "type": "object",
        "properties": {
            "vulnerability_score": {"type": "integer"},
            "revenue_impact_pct": {"type": "number"},
            "margin_compression_bps": {"type": "number"},
            "wacc_increase_bps": {"type": "number"},
            "shock_vectors": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["vulnerability_score", "revenue_impact_pct", "margin_compression_bps",
                      "wacc_increase_bps", "shock_vectors", "rationale"],
    }

    try:
        from tools.llm_engine import ask_ai_json_schema
        raw = ask_ai_json_schema(prompt, "", schema, mode="smart", temperature=0.3)
        ai = json.loads(raw)
    except Exception:
        log.exception("LLM scenario simulation failed for %s", tk)
        return {"ok": False, "error": "AI analysis failed. Please try again."}

    # Validate / clamp values
    vuln = max(1, min(10, int(ai.get("vulnerability_score") or 5)))
    rev_pct = float(ai.get("revenue_impact_pct") or 0)
    margin_bps = float(ai.get("margin_compression_bps") or 0)
    wacc_bps = float(ai.get("wacc_increase_bps") or 0)
    vectors = [v for v in (ai.get("shock_vectors") or []) if v in _SHOCK_VECTORS]
    rationale = str(ai.get("rationale") or "")

    # 4. Apply to DCF math
    ctx = get_lab_context(tk)
    latest_revenue = ctx.get("latest_revenue")
    latest_fcf = ctx.get("latest_fcf")

    # Fallback: get FCF from yfinance if DB doesn't have it
    if not latest_fcf:
        try:
            import yfinance as yf
            t = yf.Ticker(tk)
            cf = t.cash_flow
            if cf is not None and not cf.empty:
                for label in ["Free Cash Flow"]:
                    if label in cf.index:
                        vals = cf.loc[label].dropna()
                        if len(vals):
                            latest_fcf = float(vals.iloc[0])
                            break
                if not latest_fcf:
                    # Compute from operating CF - capex
                    op_cf = None
                    capex = None
                    for lbl in ["Operating Cash Flow", "Total Cash From Operating Activities"]:
                        if lbl in cf.index:
                            v = cf.loc[lbl].dropna()
                            if len(v): op_cf = float(v.iloc[0]); break
                    for lbl in ["Capital Expenditure", "Capital Expenditures"]:
                        if lbl in cf.index:
                            v = cf.loc[lbl].dropna()
                            if len(v): capex = float(v.iloc[0]); break
                    if op_cf and capex:
                        latest_fcf = op_cf + capex  # capex is negative
        except Exception:
            pass

    stressed_financials = {}
    if latest_revenue and latest_fcf and latest_revenue > 0:
        base_fcf_margin = latest_fcf / latest_revenue
        stressed_revenue = latest_revenue * (1 + rev_pct / 100)
        stressed_fcf_margin = base_fcf_margin + margin_bps / 10000
        stressed_fcf = stressed_revenue * stressed_fcf_margin

        base_discount = 10.0  # default
        roic_wacc = ctx.get("roic_wacc") or {}
        if roic_wacc.get("wacc"):
            base_discount = roic_wacc["wacc"]
        stressed_discount = base_discount + wacc_bps / 10000

        stressed_financials = {
            "base_revenue": latest_revenue,
            "stressed_revenue": round(stressed_revenue),
            "base_fcf_margin": round(base_fcf_margin * 100, 2),
            "stressed_fcf_margin": round(stressed_fcf_margin * 100, 2),
            "base_fcf": latest_fcf,
            "stressed_fcf": round(stressed_fcf),
            "base_discount": round(base_discount, 2),
            "stressed_discount": round(stressed_discount, 2),
        }

    return {
        "ok": True,
        "scenario": scenario.strip(),
        "ticker": tk,
        "sector": sector,
        "industry": industry,
        "ai_assessment": {
            "vulnerability_score": vuln,
            "revenue_impact_pct": rev_pct,
            "margin_compression_bps": margin_bps,
            "wacc_increase_bps": wacc_bps,
            "shock_vectors": vectors,
            "rationale": rationale,
        },
        "stressed_financials": stressed_financials,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _get_financials(tk: str) -> dict:
    try:
        from app.services.mini_statements_service import get_mini_statements
        data = get_mini_statements(tk)
        if data:
            # Parse years_json if it's a string
            yj = data.get("years_json")
            if isinstance(yj, str):
                try:
                    data["years_json"] = json.loads(yj)
                except Exception:
                    data["years_json"] = []
            # Parse list fields that may be stored as JSON strings
            for key in ("revenue", "gross_profit", "operating_income", "net_income",
                        "operating_cf", "free_cf", "capex", "total_assets",
                        "total_liabilities", "stockholders_equity", "debt_to_equity",
                        "total_debt", "total_cash", "total_equity",
                        "revenue_growth", "earnings_growth", "fcf_growth"):
                val = data.get(key)
                if isinstance(val, str):
                    try:
                        data[key] = json.loads(val)
                    except Exception:
                        data[key] = []
            return data
        return {}
    except Exception:
        log.exception("Failed to get financials for %s", tk)
        return {}


def _get_intel(tk: str) -> dict:
    try:
        from app.services.company_intel_service import get_company_intel
        return get_company_intel(tk) or {}
    except Exception:
        return {}


def _get_price(tk: str) -> dict:
    try:
        from app.services.price_metrics_service import get_price_metrics
        return get_price_metrics(tk) or {}
    except Exception:
        return {}


def _get_yfinance_basics(tk: str) -> tuple:
    """Return (market_cap, current_price, shares_outstanding) from yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(tk)
        fi = t.fast_info
        return (
            getattr(fi, "market_cap", None),
            getattr(fi, "last_price", None),
            getattr(fi, "shares", None),
        )
    except Exception:
        return (None, None, None)


def _last_valid(lst: list) -> float | None:
    if not lst:
        return None
    for v in reversed(lst):
        if v is not None:
            return float(v)
    return None


def _first_valid(lst: list) -> float | None:
    if not lst:
        return None
    for v in lst:
        if v is not None:
            return float(v)
    return None


def _cagr(values: list) -> float | None:
    """Compute CAGR from a list of annual values."""
    valid = [(i, v) for i, v in enumerate(values or []) if v is not None and v > 0]
    if len(valid) < 2:
        return None
    first_i, first_v = valid[0]
    last_i, last_v = valid[-1]
    n = last_i - first_i
    if n <= 0 or first_v <= 0:
        return None
    return ((last_v / first_v) ** (1 / n) - 1) * 100


def _check_margin_stability(revenue: list, gross_profit: list) -> tuple[bool, str]:
    """Check if gross margins are stable or expanding."""
    margins = []
    for r, g in zip(revenue or [], gross_profit or []):
        if r and g and r > 0:
            margins.append(g / r * 100)
    if len(margins) < 3:
        return False, "Insufficient data"
    # Check: last margin >= first margin - 2pp (allow small dip)
    expanding = margins[-1] >= margins[0] - 2
    return expanding, f"{margins[0]:.1f}% → {margins[-1]:.1f}%"


def _compute_roe(net_income: list, equity: list) -> float | None:
    ni = _last_valid(net_income)
    eq = _last_valid(equity)
    if ni is not None and eq and eq > 0:
        return ni / eq * 100
    return None


def _fmt_money(val) -> str:
    if val is None:
        return "0"
    v = float(val)
    if abs(v) >= 1e9:
        return f"{v/1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"{v/1e3:.0f}K"
    return f"{v:.0f}"
