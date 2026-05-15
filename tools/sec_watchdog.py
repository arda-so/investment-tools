#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from typing import Any

import feedparser
import requests

SEC_FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MATERIAL_FORMS = {"8-K", "10-Q", "10-K", "4"}


def _form_from_entry(title: str) -> str:
    t = (title or "").upper()
    m = re.search(r"\b(8-K|10-Q|10-K|4)\b", t)
    return m.group(1) if m else ""


def _entry_summary(entry: Any) -> str:
    summary = str(getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
    if summary:
        return summary
    return str(getattr(entry, "title", "") or "").strip()


def fetch_material_filings_for_ticker(ticker: str, limit: int = 8) -> list[dict[str, str]]:
    t = (ticker or "").strip().upper()
    if not t:
        return []
    url = f"{SEC_FEED_URL}?action=getcompany&CIK={t}&type=&owner=exclude&count=40&output=atom"
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "OnyxTerminal/3.1 (research app)"})
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for e in (getattr(parsed, "entries", None) or []):
        title = str(getattr(e, "title", "") or "").strip()
        form = _form_from_entry(title)
        if form not in MATERIAL_FORMS:
            continue
        out.append(
            {
                "ticker": t,
                "form": form,
                "title": title,
                "updated": str(getattr(e, "updated", "") or "").strip(),
                "link": str(getattr(e, "link", "") or "").strip(),
                "summary": _entry_summary(e),
            }
        )
        if len(out) >= limit:
            break
    return out


def _ollama_sec_summary(ticker: str, form: str, filing_summary: str, model: str = "llama3", timeout: int = 25) -> str:
    system = (
        f"A new SEC Filing ({form}) was released for {ticker}. "
        "Summarize the material event in 1 sentence. Is this negative for shareholders?"
    )
    prompt = (
        "Return JSON only with keys: sentence, shareholder_impact.\n"
        f"Filing summary:\n{filing_summary[:3000]}"
    )
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json=body, timeout=timeout, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        txt = str(((resp.json() or {}).get("message") or {}).get("content") or "").strip()
        if not txt:
            return ""
        m = txt.find("{")
        n = txt.rfind("}")
        if m < 0 or n <= m:
            return txt[:220]
        obj: Any = json.loads(txt[m : n + 1])
        sentence = str((obj or {}).get("sentence") or "").strip()
        impact = str((obj or {}).get("shareholder_impact") or "").strip()
        if sentence and impact:
            return f"{sentence} | Shareholder impact: {impact}"
        return sentence or impact or txt[:220]
    except Exception:
        return ""


def get_material_filings(tickers: list[str], per_ticker: int = 4, model: str = "llama3") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for t in tickers:
        for r in fetch_material_filings_for_ticker(t, limit=per_ticker):
            ai = _ollama_sec_summary(
                ticker=str(r.get("ticker") or ""),
                form=str(r.get("form") or ""),
                filing_summary=str(r.get("summary") or ""),
                model=model,
                timeout=25,
            )
            if ai:
                r["ai_summary"] = ai
            out.append(r)
    # SEC Atom updated values are already sortable lexicographically for our view.
    out.sort(key=lambda x: str(x.get("updated") or ""), reverse=True)
    return out

