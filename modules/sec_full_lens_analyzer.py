#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from modules.sec_filing_analyzer import SECFilingAnalyzer
from tools.llm_engine import ask_ai


class SECFullLensAnalyzer:
    """
    Multi-section 10-K analyzer:
    - Business Section (Item 1)
    - MD&A (Item 7)
    - Risk Factors (Item 1A)
    - Financials (Item 8)
    - Notes (within Item 8)
    - Proxy/Ownership/Incentives (latest DEF 14A when available)
    """

    def __init__(self, download_dir: str = "data/sec_filings") -> None:
        self.base = SECFilingAnalyzer(download_dir=download_dir)

    def fetch_latest_10ks(self, ticker: str) -> list[Path]:
        return self.base.fetch_latest_10ks(ticker)

    def load_filing_text(self, path: Path) -> str:
        return self.base.load_filing_text(path)

    def extract_sections(self, html_content: str) -> dict[str, str]:
        txt = html_content or ""
        # Convert HTML/XBRL-heavy documents into normalized plain text so Item regexes work.
        plain = self._to_plain_text(txt)
        business = self._extract_item_block(
            plain,
            r"\bitem\s+1\.?\s+business\b",
            (r"\bitem\s+1a\.?\s+risk\s+factors\b", r"\bitem\s+1b\b", r"\bitem\s+2\b"),
            max_chars=160000,
        )
        mda = self._extract_item_block(
            plain,
            r"\bitem\s+7\.?\b",
            (r"\bitem\s+7a\.?\b", r"\bitem\s+8\.?\b"),
            max_chars=180000,
        )
        risk = ""
        try:
            risk = self.base.extract_risk_factors(txt)
        except Exception:
            risk = self._extract_item_block(
                plain,
                r"\bitem\s+1a\.?\s+risk\s+factors\b",
                (r"\bitem\s+1b\.?\b", r"\bitem\s+2\b"),
                max_chars=180000,
            )
        financials = self._extract_item_block(
            plain,
            r"\bitem\s+8\.?\b",
            (r"\bitem\s+9\b", r"\bitem\s+9a\b"),
            max_chars=220000,
        )
        # Fallbacks if Item extraction misses due unusual formatting.
        if not business:
            business = self._window_around_keywords(
                plain,
                ["our business", "we provide", "products and services", "customers", "market"],
                window=12000,
            )
        if not mda:
            mda = self._window_around_keywords(
                plain,
                ["management s discussion", "results of operations", "liquidity and capital resources"],
                window=15000,
            )
        if not risk:
            risk = self._window_around_keywords(
                plain,
                ["risk factors", "could adversely affect", "material adverse effect"],
                window=14000,
            )
        if not financials:
            financials = self._window_around_keywords(
                plain,
                ["financial statements", "consolidated statements", "cash flows", "balance sheet"],
                window=18000,
            )
        notes = self._extract_notes(financials)
        return {
            "business": business,
            "mda": mda,
            "risk": risk,
            "financials": financials,
            "notes": notes,
        }

    def fetch_latest_proxy_text(self, ticker: str) -> str:
        t = (ticker or "").strip().upper()
        if not t:
            return ""
        files: list[Path] = []
        dl = self.base.downloader
        if dl is not None:
            try:
                try:
                    dl.get("DEF 14A", t, limit=1)
                except TypeError:
                    dl.get("DEF 14A", t, amount=1)
                base = self.base.download_dir / "sec-edgar-filings" / t / "DEF 14A"
                if base.exists():
                    for p in base.rglob("*"):
                        if p.is_file() and p.name.lower().endswith((".txt", ".htm", ".html", ".xhtml")):
                            files.append(p)
            except Exception:
                pass
        if not files:
            return ""
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return self.base.load_filing_text(files[0])[:280000]

    def analyze_sections_with_llm(self, ticker: str, sections: dict[str, str], proxy_text: str = "") -> dict[str, Any]:
        t = (ticker or "").strip().upper()
        business = (sections.get("business") or "")[:22000]
        mda = (sections.get("mda") or "")[:26000]
        risk = (sections.get("risk") or "")[:22000]
        financials = (sections.get("financials") or "")[:24000]
        notes = (sections.get("notes") or "")[:20000]
        proxy = (proxy_text or "")[:22000]

        system_prompt = """
You are a forensic buy-side analyst.
You are NOT a summarizer. You are an EVIDENCE EXTRACTOR.
Analyze six sections separately and return JSON only.
Use Buffett-style quality checks:
- Accountability vs blame
- Earnings quality (GAAP/FCF vs adjusted optics)
- Capital allocation discipline
- Clarity vs jargon
- Competition and moat realism
- Incentive alignment in proxy
Materiality priority:
1) funding/liquidity/covenant stress
2) demand/margin inflection
3) competitive erosion or pricing power gains
4) governance incentive misalignment

Output JSON:
{
  "segments": {
    "business": {"what_it_does":"", "customers_value":"", "product_mix":"", "competition_status":"", "management_signal":"", "red_flag":"", "green_flag":""},
    "mda": {"why_revenue_up_down":"", "margin_drivers":"", "guidance_shift":"", "accountability":"", "red_flag":"", "green_flag":""},
    "risk": {"top_exposure":"", "concentration_risk":"", "dependency_risk":"", "policy_regulatory_risk":"", "red_flag":"", "green_flag":""},
    "financials": {"income_statement_signal":"", "balance_sheet_signal":"", "cashflow_signal":"", "working_capital_signal":"", "red_flag":"", "green_flag":""},
    "notes": {"debt_terms_covenants":"", "revenue_recognition_detail":"", "customer_supplier_concentration":"", "legal_contingency":"", "red_flag":"", "green_flag":""},
    "proxy": {"ownership_alignment":"", "incentive_design":"", "vesting_horizon":"", "capital_behavior_risk":"", "red_flag":"", "green_flag":""}
  }
}
Rules:
- Keep each field <= 24 words.
- Prefer specific evidence (numbers, named entities, exact change language).
- Non-generic rule: each non-empty field must include a concrete trigger phrase.
- If a field is uncertain, say exactly: "Not clearly disclosed in provided text."
- If unknown, write "Not clearly disclosed in provided text."
"""

        prompt = (
            "JSON_MODE=ON. Return ONLY valid JSON.\n"
            f"Ticker: {t}\n\n"
            f"[BUSINESS]\n{business}\n\n"
            f"[MD&A]\n{mda}\n\n"
            f"[RISK FACTORS]\n{risk}\n\n"
            f"[FINANCIALS ITEM 8]\n{financials}\n\n"
            f"[NOTES SUBSET]\n{notes}\n\n"
            f"[PROXY DEF 14A]\n{proxy}"
        )
        raw = ask_ai(prompt, system_prompt)
        parsed = self._parse_json(raw)
        if parsed:
            return parsed
        # Heuristic fallback (non-empty) so UI still provides useful intelligence.
        return self._heuristic_segments(sections, proxy_text)

    def _extract_item_block(self, text: str, start_re: str, end_res: tuple[str, ...], max_chars: int = 180000) -> str:
        if not text:
            return ""
        ms = list(re.finditer(start_re, text, flags=re.IGNORECASE))
        if not ms:
            return ""
        m = ms[1] if len(ms) > 1 else ms[0]
        s = m.start()
        end_idx = len(text)
        for er in end_res:
            em = re.search(er, text[s + 1 :], flags=re.IGNORECASE)
            if em:
                end_idx = min(end_idx, s + 1 + em.start())
        blk = re.sub(r"\s+", " ", text[s:end_idx]).strip()
        return blk[:max_chars]

    def _extract_notes(self, item8_text: str) -> str:
        txt = item8_text or ""
        if not txt:
            return ""
        pats = [
            r"notes to consolidated financial statements",
            r"note\s+1\.",
            r"revenue recognition",
            r"debt",
            r"covenant",
            r"contingenc",
            r"legal proceedings",
            r"segment information",
        ]
        chunks: list[str] = []
        for pat in pats:
            for m in re.finditer(pat, txt, flags=re.IGNORECASE):
                a = max(0, m.start() - 850)
                b = min(len(txt), m.end() + 1400)
                chunks.append(txt[a:b])
                if len(chunks) >= 10:
                    break
            if len(chunks) >= 10:
                break
        if not chunks:
            return txt[:45000]
        return re.sub(r"\s+", " ", " ".join(chunks))[:65000]

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        txt = (raw or "").strip()
        if not txt:
            return None
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", txt, flags=re.IGNORECASE | re.DOTALL)
        if m:
            txt = m.group(1).strip()
        for candidate in (txt, txt[txt.find("{") : txt.rfind("}") + 1] if ("{" in txt and "}" in txt) else ""):
            if not candidate:
                continue
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and isinstance(obj.get("segments"), dict):
                    return obj
            except Exception:
                continue
        return None

    def _to_plain_text(self, raw: str) -> str:
        s = raw or ""
        if not s.strip():
            return ""
        try:
            soup = BeautifulSoup(s, "html.parser")
            for bad in soup(["script", "style", "noscript"]):
                bad.decompose()
            txt = soup.get_text(" ", strip=True)
        except Exception:
            txt = s
        return re.sub(r"\s+", " ", txt).strip()

    def _window_around_keywords(self, text: str, kws: list[str], window: int = 12000) -> str:
        low = (text or "").lower()
        for kw in kws:
            i = low.find(kw.lower())
            if i >= 0:
                a = max(0, i - 1200)
                b = min(len(text), i + window)
                return re.sub(r"\s+", " ", text[a:b]).strip()
        return ""

    def _clip_sentence(self, text: str, max_chars: int = 180) -> str:
        s = re.sub(r"\s+", " ", (text or "")).strip()
        if not s:
            return "Not clearly disclosed in provided text."
        parts = re.split(r"(?<=[\.\;\:])\s+", s)
        out = (parts[0] if parts else s).strip()
        if len(out) > max_chars:
            out = out[: max_chars - 1].rstrip() + "..."
        return out

    def _heuristic_segments(self, sections: dict[str, str], proxy_text: str) -> dict[str, Any]:
        biz = sections.get("business") or ""
        mda = sections.get("mda") or ""
        risk = sections.get("risk") or ""
        fin = sections.get("financials") or ""
        notes = sections.get("notes") or ""
        proxy = proxy_text or ""
        return {
            "segments": {
                "business": {
                    "what_it_does": self._clip_sentence(biz),
                    "customers_value": self._clip_sentence(biz[220:1000]),
                    "product_mix": self._clip_sentence(biz[1000:2000]),
                    "competition_status": "Competition discussed." if "compet" in biz.lower() else "Competition specifics limited in extracted text.",
                    "management_signal": "Business narrative is concrete." if len(biz) > 1500 else "Business narrative is short/limited.",
                    "red_flag": "Low specificity in business narrative." if len(biz) < 900 else "No obvious business red flag in heuristic pass.",
                    "green_flag": "Core business model text extracted.",
                },
                "mda": {
                    "why_revenue_up_down": self._clip_sentence(mda),
                    "margin_drivers": "Margin/cost drivers discussed." if any(x in mda.lower() for x in ["margin", "cost", "pricing"]) else "Margin drivers not explicit.",
                    "guidance_shift": "Guidance/outlook language present." if any(x in mda.lower() for x in ["guidance", "outlook", "expect"]) else "No clear guidance shift detected.",
                    "accountability": "Owns execution details." if "we" in mda.lower() and "result" in mda.lower() else "Accountability tone not explicit.",
                    "red_flag": "Macro-blame language risk." if any(x in mda.lower() for x in ["weather", "currency", "macro headwind"]) else "No obvious accountability red flag.",
                    "green_flag": "MD&A explanation text extracted.",
                },
                "risk": {
                    "top_exposure": self._clip_sentence(risk),
                    "concentration_risk": "Concentration risk disclosed." if any(x in risk.lower() for x in ["concentration", "major customer", "single"]) else "No explicit concentration wording found.",
                    "dependency_risk": "Dependency risk disclosed." if any(x in risk.lower() for x in ["supplier", "partner", "platform"]) else "Dependency wording limited.",
                    "policy_regulatory_risk": "Regulatory risk disclosed." if any(x in risk.lower() for x in ["regulation", "government", "compliance"]) else "Policy risk wording limited.",
                    "red_flag": "Dense risk language." if len(risk) > 30000 else "No unusual risk-density red flag.",
                    "green_flag": "Risk section extracted for review.",
                },
                "financials": {
                    "income_statement_signal": "Income statement discussion present." if "revenue" in fin.lower() else "Income statement signal not explicit.",
                    "balance_sheet_signal": "Balance-sheet discussion present." if any(x in fin.lower() for x in ["assets", "liabilities", "debt", "cash"]) else "Balance-sheet signal not explicit.",
                    "cashflow_signal": "Cash-flow discussion present." if any(x in fin.lower() for x in ["cash flow", "operating cash", "capex"]) else "Cash-flow signal not explicit.",
                    "working_capital_signal": "Working-capital terms present." if any(x in fin.lower() for x in ["working capital", "inventory", "receivable"]) else "Working-capital signal limited.",
                    "red_flag": "Financial statement detail thin." if len(fin) < 2000 else "No immediate financial red flag in heuristic pass.",
                    "green_flag": "Financial statement text extracted.",
                },
                "notes": {
                    "debt_terms_covenants": "Debt/covenant notes present." if any(x in notes.lower() for x in ["debt", "covenant", "maturity"]) else "Debt/covenant note not explicit.",
                    "revenue_recognition_detail": "Revenue recognition note present." if any(x in notes.lower() for x in ["revenue recognition", "performance obligation"]) else "Revenue recognition detail limited.",
                    "customer_supplier_concentration": "Customer/supplier note present." if any(x in notes.lower() for x in ["customer", "supplier", "concentration"]) else "Concentration detail limited.",
                    "legal_contingency": "Legal contingency note present." if any(x in notes.lower() for x in ["legal", "contingenc", "litigation"]) else "Legal contingency detail limited.",
                    "red_flag": "Notes extraction may be partial.",
                    "green_flag": "Key notes windows extracted.",
                },
                "proxy": {
                    "ownership_alignment": "Ownership language present." if any(x in proxy.lower() for x in ["ownership", "beneficial"]) else "Ownership alignment not clearly disclosed.",
                    "incentive_design": "Incentive plan language present." if any(x in proxy.lower() for x in ["compensation", "incentive", "bonus"]) else "Incentive design not clearly extracted.",
                    "vesting_horizon": "Vesting terms present." if any(x in proxy.lower() for x in ["vesting", "rsu", "performance stock"]) else "Vesting horizon not explicit.",
                    "capital_behavior_risk": "EPS-linked incentive risk." if "eps" in proxy.lower() else "Capital behavior risk not explicit.",
                    "red_flag": "Proxy unavailable/partial." if not proxy else "No obvious proxy red flag in heuristic pass.",
                    "green_flag": "Proxy incentives/ownership data extracted." if proxy else "Proxy text unavailable.",
                },
            }
        }
