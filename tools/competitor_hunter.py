#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
from pathlib import Path

try:
    import yfinance as yf
except Exception:
    yf = None

ROOT = Path("/Users/solmaz/Investment_Tools")
DATA = ROOT / "data"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _to_num(v: object) -> float | None:
    try:
        if v is None:
            return None
        s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _research_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATA / "research.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _read_local_10k_text(ticker: str, max_chars: int = 450000) -> tuple[str, str]:
    t = ticker.upper().strip()
    conn = _research_db()
    try:
        rows = conn.execute(
            """
            SELECT form, date, path, doc_url
            FROM filings
            WHERE ticker = ? AND form IN ('10-K', '20-F')
            ORDER BY date DESC
            LIMIT 8
            """,
            (t,),
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        p = str(r["path"] or "").strip()
        if p:
            fp = Path(p) if Path(p).is_absolute() else (ROOT / p)
            if not fp.exists():
                # Path migration fallback: old paths under investment_research -> current ROOT/filings by basename.
                alt = ROOT / "filings" / fp.name
                if alt.exists():
                    fp = alt
            if fp.exists() and fp.is_file():
                try:
                    txt = fp.read_text(encoding="utf-8", errors="ignore")[:max_chars]
                    if txt:
                        return txt, f"10-K local filing {r['form']} {r['date']}"
                except Exception:
                    pass

    # Fallback SEC network pull via sec_client if local files are missing.
    for r in rows:
        url = str(r["doc_url"] or "").strip()
        if not url:
            continue
        try:
            from sec_client import download_filing_text

            txt = (download_filing_text(url) or "")[:max_chars]
            if txt:
                return txt, f"10-K SEC pull {r['form']} {r['date']}"
        except Exception:
            continue

    return "", ""


def _read_sec_10k_text(ticker: str, max_chars: int = 450000) -> tuple[str, str]:
    # First use local filings cache from research sync.
    txt, src = _read_local_10k_text(ticker, max_chars=max_chars)
    if txt:
        return txt, src

    # Then try direct SEC submissions feed.
    try:
        from sec_client import download_filing_text, get_all_filings, load_ticker_cik_map

        cik_map = load_ticker_cik_map()
        cik = cik_map.get(ticker.upper().strip())
        if not cik:
            return "", ""
        _, _, _, _, filings = get_all_filings(cik)
        tenk = [f for f in filings if str(f.get("form", "")).upper() in {"10-K", "20-F"}]
        tenk.sort(key=lambda x: str(x.get("date", "")), reverse=True)
        if not tenk:
            return "", ""
        f0 = tenk[0]
        acc = str(f0.get("accession", "")).replace("-", "")
        doc = str(f0.get("primaryDocument", ""))
        if not acc or not doc:
            return "", ""
        cik_int = str(int(cik))
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"
        txt = (download_filing_text(doc_url) or "")[:max_chars]
        if txt:
            return txt, f"10-K SEC submissions {f0.get('date', '-')}"
    except Exception:
        pass

    return "", ""


def _parse_competitor_list(raw: str, ticker: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    # Try strict python-list parse first.
    try:
        obj = ast.literal_eval(s)
        if isinstance(obj, list):
            out = []
            for x in obj:
                name = str(x).strip()
                if name and name.upper() != ticker.upper():
                    out.append(name)
            return out[:8]
    except Exception:
        pass
    # Fallback: comma/newline split.
    bits = re.split(r"[\n,;]+", s)
    out = []
    for b in bits:
        n = re.sub(r"^[\-\*\d\.\)\s]+", "", b).strip()
        if not n:
            continue
        if n.upper() == ticker.upper():
            continue
        out.append(n)
    return out[:8]


def _ai_competitors_from_text(ticker: str, text: str) -> tuple[list[str], str]:
    if not text:
        return [], "no_10k_text"
    prompt = (
        f"Company ticker: {ticker}\n\n"
        "Read this text. List the top 5 specific companies mentioned as competitors or risks. "
        "Return them as a python list of company names only.\n\n"
        f"Text:\n{text[:120000]}"
    )
    system = "You extract competitor company names from SEC filing text. Return only a python list of strings."
    try:
        from tools.llm_engine import ask_ai
        raw = ask_ai(prompt, system)
        out = _parse_competitor_list(raw, ticker)[:5]
        return out, ("ok" if out else "ai_empty")
    except Exception as e:
        return [], f"ai_error:{str(e)[:80]}"


def _heuristic_competitors_from_text(ticker: str, text: str) -> list[str]:
    # Fallback when AI is unavailable: scan competition-related snippets and extract proper names.
    if not text:
        return []
    low = text.lower()
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"compet\w+", low):
        a = max(0, m.start() - 500)
        b = min(len(text), m.end() + 500)
        spans.append((a, b))
        if len(spans) >= 80:
            break
    if not spans:
        return []
    snippet = " ".join(text[a:b] for a, b in spans)
    # Capture likely company names (e.g., Microsoft, Oracle, ServiceNow).
    cands = re.findall(r"\b[A-Z][A-Za-z&\.\-]{2,}(?:\s+[A-Z][A-Za-z&\.\-]{2,}){0,2}\b", snippet)
    stop = {
        "Company",
        "Companies",
        "United States",
        "Risk Factors",
        "Management",
        "Board",
        "Common Stock",
        "Securities",
        "Business",
        "Products",
        "Services",
        "Customers",
        "Vendors",
        "Our",
        "The",
        "Further",
        "This",
        "These",
        "Those",
        "LLMs",
    }
    freq: dict[str, int] = {}
    for c in cands:
        name = c.strip().strip(",.;:()[]{}")
        if not name or name.upper() == ticker.upper():
            continue
        if name.isupper() and len(name) > 4:
            continue
        if name in stop:
            continue
        if len(name) < 3:
            continue
        freq[name] = freq.get(name, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ranked[:8]]


def _fallback_sector_peers(ticker: str) -> list[str]:
    if yf is None:
        return []
    peers: list[str] = []
    try:
        if hasattr(yf, "Search"):
            s = yf.Search(ticker.upper().strip(), max_results=20)
            quotes = getattr(s, "quotes", []) or []
            for q in quotes:
                sym = str(q.get("symbol", "")).upper().strip()
                if not sym or sym == ticker.upper():
                    continue
                if re.match(r"^[A-Z][A-Z0-9\.\-]{0,8}$", sym):
                    peers.append(sym)
    except Exception:
        pass
    # Dedup preserve order.
    seen = set()
    out = []
    for p in peers:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= 8:
            break
    return out


def _symbol_looks_real(sym: str) -> bool:
    if yf is None:
        return False
    s = (sym or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9\.\-]{0,8}$", s):
        return False
    try:
        tk = yf.Ticker(s)
        fi = getattr(tk, "fast_info", {}) or {}
        if _to_num(fi.get("last_price")) is not None:
            return True
        info = getattr(tk, "info", {}) or {}
        if _to_num(info.get("marketCap")) is not None:
            return True
    except Exception:
        return False
    return False


def _to_symbol(candidate: str) -> str:
    c = (candidate or "").strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9\.\-]{0,8}$", c):
        s = c.upper()
        return s if _symbol_looks_real(s) else ""
    # Try yfinance search to map company name -> ticker.
    if yf is not None and hasattr(yf, "Search"):
        try:
            s = yf.Search(c, max_results=6)
            quotes = getattr(s, "quotes", []) or []
            for q in quotes:
                sym = str(q.get("symbol", "")).upper().strip()
                if _symbol_looks_real(sym):
                    return sym
        except Exception:
            pass
    return ""


def _fallback_template_peers(ticker: str) -> list[str]:
    manual = {
        "CRM": ["NOW", "ORCL", "SAP", "MSFT", "ADBE"],
        "HUBS": ["CRM", "ADBE", "NOW", "ORCL", "MSFT"],
        "IT": ["ACN", "IBM", "CTSH", "EPAM", "GLOB"],
        "ADBE": ["CRM", "MSFT", "INTU", "ORCL", "SAP"],
        "KSPI": ["PYPL", "SQ", "NU", "MELI", "SOFI"],
    }
    m = manual.get(ticker.upper().strip())
    if m:
        return [x for x in m if x != ticker.upper()]
    if yf is None:
        return []
    sector_txt = ""
    try:
        info = getattr(yf.Ticker(ticker), "info", {}) or {}
        sector_txt = f"{info.get('sector','')} {info.get('industry','')}".lower()
    except Exception:
        sector_txt = ""
    templates = [
        (("software", "application"), ["MSFT", "ORCL", "NOW", "ADBE", "INTU"]),
        (("software", "infrastructure"), ["MSFT", "ORCL", "PANW", "CRWD", "SNPS"]),
        (("semiconductor",), ["NVDA", "AMD", "AVGO", "QCOM", "INTC"]),
        (("internet", "retail"), ["AMZN", "EBAY", "SHOP", "MELI", "SE"]),
        (("financial",), ["JPM", "BAC", "MS", "GS", "SCHW"]),
    ]
    for keys, peers in templates:
        if all(k in sector_txt for k in keys):
            return [p for p in peers if p != ticker.upper()]
    return []


def _peer_metrics(symbol: str) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": symbol,
        "name": symbol,
        "pe": None,
        "profit_margin": None,
        "ytd_return": None,
        "winner": False,
    }
    if yf is None:
        return row
    try:
        tk = yf.Ticker(symbol)
        info = getattr(tk, "info", {}) or {}
        fi = getattr(tk, "fast_info", {}) or {}
        row["name"] = str(info.get("shortName") or info.get("longName") or symbol)
        pe = _to_num(info.get("forwardPE")) or _to_num(fi.get("forwardPE")) or _to_num(info.get("trailingPE"))
        pm = _to_num(info.get("profitMargins"))
        if pm is not None:
            pm *= 100.0
        ytd = None
        try:
            h = tk.history(period="ytd", interval="1d")
            if hasattr(h, "__len__") and len(h) >= 2:
                c = h["Close"].dropna()
                if len(c) >= 2:
                    first = float(c.iloc[0])
                    last = float(c.iloc[-1])
                    if first != 0:
                        ytd = (last - first) / abs(first) * 100.0
        except Exception:
            pass
        row["pe"] = pe
        row["profit_margin"] = pm
        row["ytd_return"] = ytd
    except Exception:
        pass
    return row


def _pick_winner(rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    best_idx = None
    best_key = None
    for i, r in enumerate(rows):
        pm = _to_num(r.get("profit_margin"))
        ytd = _to_num(r.get("ytd_return"))
        key = (
            pm if pm is not None else -10**9,
            ytd if ytd is not None else -10**9,
        )
        if best_key is None or key > best_key:
            best_key = key
            best_idx = i
    if best_idx is not None:
        rows[best_idx]["winner"] = True


def run(ticker: str) -> dict[str, object]:
    t = ticker.upper().strip()
    out: dict[str, object] = {"ticker": t, "source": "", "source_detail": "", "competitors": []}
    if not t:
        out["source"] = "error"
        out["source_detail"] = "Ticker is required."
        return out

    tenk_text, src = _read_sec_10k_text(t)
    names: list[str] = []
    ai_reason = ""
    if tenk_text:
        names, ai_reason = _ai_competitors_from_text(t, tenk_text)
        if not names:
            names = _heuristic_competitors_from_text(t, tenk_text)[:5]
    symbols: list[str] = []
    for n in names:
        sym = _to_symbol(n)
        if sym and sym != t and sym not in symbols:
            symbols.append(sym)
        if len(symbols) >= 5:
            break

    if symbols:
        out["source"] = "10-K"
        reason_suffix = f" | {ai_reason}" if ai_reason else ""
        out["source_detail"] = (src or "10-K text parsed") + reason_suffix
    else:
        symbols = _fallback_sector_peers(t)[:5]
        if not symbols:
            symbols = _fallback_template_peers(t)[:5]
        out["source"] = "Yahoo Sector Peers"
        reason_suffix = f" | {ai_reason}" if ai_reason else ""
        out["source_detail"] = "Fallback: SEC 10-K parse unavailable or produced no competitors." + reason_suffix

    rows = [_peer_metrics(s) for s in symbols[:5]]
    _pick_winner(rows)
    out["competitors"] = rows
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="10-K competitor hunter")
    ap.add_argument("--ticker", required=True)
    args = ap.parse_args()
    result = run(args.ticker)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
