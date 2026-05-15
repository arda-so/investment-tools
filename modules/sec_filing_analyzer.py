#!/usr/bin/env python3
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from modules.llm_engine import ask_ai
from modules.smart_parser import SmartParser

try:
    from sec_edgar_downloader import Downloader
except Exception:  # pragma: no cover
    Downloader = None  # type: ignore[assignment]

try:
    from nltk.tokenize import sent_tokenize
except Exception:  # pragma: no cover
    sent_tokenize = None  # type: ignore[assignment]


class ExtractionError(RuntimeError):
    """Raised when robust ToC-based extraction cannot locate Item 1A boundaries."""


class SECFilingAnalyzer:
    def __init__(self, download_dir: str = "data/sec_filings") -> None:
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.parser = SmartParser()
        self.user_agent = os.getenv("SEC_USER_AGENT", "OnyxTerminal/3.1 (research)")
        self.downloader = None
        self.init_error = ""
        if Downloader is not None:
            try:
                # company_name/email pair is required by sec-edgar-downloader user-agent format.
                self.downloader = Downloader(
                    company_name="OnyxTerminal",
                    email_address="research@onyx.local",
                    download_folder=str(self.download_dir),
                )
            except Exception as e:
                # Do not hard-fail; fallback to local filing DB.
                self.init_error = str(e)[:260]

    def fetch_latest_10ks(self, ticker: str) -> list[Path]:
        t = (ticker or "").strip().upper()
        if not t:
            raise ValueError("ticker is required")

        candidates = self._download_form_candidates(ticker=t, form="10-K", limit=2)

        if len(candidates) < 2:
            candidates = self._fallback_latest_10ks_from_local_db(t)

        if len(candidates) < 2:
            raise FileNotFoundError(
                f"Expected at least 2 10-K files for {t}, found {len(candidates)} "
                f"(downloader init: {self.init_error or 'ok'})"
            )

        # sec-edgar-downloader stores filings by accession folder; mtime/date ordering is safest.
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[:2]

    def fetch_latest_proxy(self, ticker: str) -> Path:
        t = (ticker or "").strip().upper()
        if not t:
            raise ValueError("ticker is required")

        candidates = self._download_form_candidates(ticker=t, form="DEF 14A", limit=1)
        if not candidates:
            candidates = self._fallback_latest_forms_from_local_db(t, forms=["DEF 14A", "DEF14A"], limit=1)
        if not candidates:
            raise FileNotFoundError(
                f"Expected latest DEF 14A for {t}, found none "
                f"(downloader init: {self.init_error or 'ok'})"
            )
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def load_filing_text(self, path: Path) -> str:
        """
        Read a filing path and normalize SEC full-submission bundles by extracting
        only the primary 10-K document payload.
        """
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return self._extract_primary_10k_payload(raw)

    def extract_risk_factors(self, html_content: str) -> str:
        if not (html_content or "").strip():
            raise ExtractionError("Empty filing content.")
        # Fast surgical path first.
        cleaned = self.parser.clean_html(html_content)
        quick = self.parser.extract_section(cleaned, "RISK", max_chars=40000).strip()
        if len(quick) >= 300:
            return re.sub(r"\s+", " ", quick).strip()

        soup = BeautifulSoup(html_content, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.decompose()

        toc_anchor = self._find_item_1a_anchor_from_toc(soup)
        if not toc_anchor:
            raise ExtractionError("Could not locate Item 1A anchor from Table of Contents.")

        start_tag = self._resolve_anchor_target(soup, toc_anchor)
        if start_tag is None:
            raise ExtractionError(f"Resolved ToC href '{toc_anchor}' but anchor target was not found.")

        text = self._extract_until_next_item(start_tag)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 300:
            raise ExtractionError("Extracted Item 1A text is too short; likely malformed anchor boundaries.")
        return text

    def extract_business_section(self, html_content: str) -> str:
        if not (html_content or "").strip():
            raise ExtractionError("Empty filing content.")
        # Prefer surgical parser for Item 1 before ToC walk.
        cleaned = self.parser.clean_html(html_content)
        quick = self.parser.extract_section(cleaned, "BUSINESS", max_chars=35000).strip()
        if len(quick) >= 600:
            return re.sub(r"\s+", " ", quick).strip()
        out = self._extract_section_via_toc(
            html_content=html_content,
            start_patterns=[r"\bitem\s*1\b", r"\bbusiness\b"],
            end_patterns=[r"\bitem\s*1a\b", r"\brisk factors\b"],
            min_chars=600,
        )
        if not out:
            raise ExtractionError("Could not extract Item 1 (Business) from ToC anchors.")
        return out

    def extract_financial_notes(self, html_content: str) -> str:
        if not (html_content or "").strip():
            raise ExtractionError("Empty filing content.")
        # Item 8 notes block via surgical parser first.
        cleaned = self.parser.clean_html(html_content)
        quick = self.parser.extract_section(cleaned, "NOTES", max_chars=50000).strip()
        if len(quick) >= 1000:
            return self._extract_notes_focus(quick, max_chars=20000)
        block = self._extract_section_via_toc(
            html_content=html_content,
            start_patterns=[
                r"\bitem\s*8\b",
                r"\bfinancial statements\b",
                r"\bsupplementary data\b",
            ],
            end_patterns=[r"\bitem\s*9\b", r"\bchanges in and disagreements\b"],
            min_chars=1000,
        )
        if not block:
            raise ExtractionError("Could not extract Item 8 (Financial Statements and Notes).")
        return self._extract_notes_focus(block, max_chars=20000)

    def extract_proxy_incentives(self, proxy_html_content: str) -> str:
        if not (proxy_html_content or "").strip():
            raise ExtractionError("Empty proxy content.")
        soup = BeautifulSoup(proxy_html_content, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.decompose()
        page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        if not page_text:
            raise ExtractionError("Proxy parse returned empty text.")

        # Try heading anchored slices first.
        headings = [
            r"compensation discussion and analysis",
            r"executive compensation",
            r"compensation committee",
        ]
        for pat in headings:
            m = re.search(pat, page_text, flags=re.IGNORECASE)
            if not m:
                continue
            start = max(0, m.start() - 120)
            end = min(len(page_text), m.start() + 14000)
            chunk = page_text[start:end].strip()
            if len(chunk) >= 500:
                return chunk
        raise ExtractionError("Could not extract CD&A/Executive Compensation from proxy.")

    def analyze_business_model(self, business_text: str) -> dict[str, Any]:
        SYSTEM_PROMPT_BIZ = """
You are a fundamental long-only analyst.
Use only the provided Item 1 text; do not invent facts.
Output must be specific and non-generic:
1) summary: include product/service + revenue mechanism.
2) customers: identify buyer type and concentration hint if present.
3) moat_source: explicitly state the strongest moat driver and cite one concrete phrase.
Output JSON only: { "summary": "...", "customers": "...", "moat_source": "..." }
"""
        prompt = (
            "Return ONLY JSON with keys: summary, customers, moat_source.\n\n"
            f"Business section:\n{(business_text or '')[:24000]}"
        )
        parsed = self._ask_json(prompt=prompt, context=SYSTEM_PROMPT_BIZ)
        return {
            "summary": str(parsed.get("summary") or "").strip(),
            "customers": str(parsed.get("customers") or "").strip(),
            "moat_source": str(parsed.get("moat_source") or "").strip(),
        }

    def analyze_accounting_risks(self, notes_text: str) -> dict[str, Any]:
        SYSTEM_PROMPT_FORENSIC = """
You are a forensic accountant.
Use only provided Item 8 notes text and avoid generic language.
Prioritize: revenue recognition, debt/covenants/maturities, customer or supplier concentration.
If weak evidence, say "unclear in extract".
Output JSON only:
{ "revenue_flag": "Safe/Aggressive/Unclear", "debt_risk": "Low/Medium/High", "concentration": "None/Moderate/High", "evidence": "One concrete phrase/number from text" }
"""
        prompt = (
            "Return ONLY JSON with keys: revenue_flag, debt_risk, concentration, evidence.\n\n"
            f"Financial notes extract:\n{(notes_text or '')[:24000]}"
        )
        parsed = self._ask_json(prompt=prompt, context=SYSTEM_PROMPT_FORENSIC)
        return {
            "revenue_flag": str(parsed.get("revenue_flag") or "").strip(),
            "debt_risk": str(parsed.get("debt_risk") or "").strip(),
            "concentration": str(parsed.get("concentration") or "").strip(),
            "evidence": str(parsed.get("evidence") or "").strip(),
        }

    def analyze_governance(self, proxy_text: str) -> dict[str, Any]:
        SYSTEM_PROMPT_GOV = """
You are an activist governance analyst.
Use only provided proxy text; avoid generic advice.
Focus on incentive metric quality, ownership alignment, and any pay-risk mismatch.
alignment_score rules:
- 8-10 strong long-term alignment
- 5-7 mixed
- 1-4 weak/short-term alignment
Output JSON only: { "incentive_metric": "...", "alignment_score": 1-10, "red_flags": "..." }
"""
        prompt = (
            "Return ONLY JSON with keys: incentive_metric, alignment_score, red_flags.\n\n"
            f"Proxy extract:\n{(proxy_text or '')[:24000]}"
        )
        parsed = self._ask_json(prompt=prompt, context=SYSTEM_PROMPT_GOV)
        score = 0
        try:
            score = int(parsed.get("alignment_score", 0))
        except Exception:
            score = 0
        score = max(1, min(10, score)) if score else 0
        return {
            "incentive_metric": str(parsed.get("incentive_metric") or "").strip(),
            "alignment_score": score,
            "red_flags": str(parsed.get("red_flags") or "").strip(),
        }

    def analyze_moat_from_mda(self, mda_text: str) -> dict[str, Any]:
        system_prompt = (
            "You are a buy-side analyst evaluating management discussion quality. "
            "Use only provided MD&A text and avoid generic phrasing. "
            "For each field, include one concrete driver, number, or named factor if present. "
            "Return JSON keys exactly: revenue_driver, margin_driver, competition_update, guidance_shift."
        )
        prompt = (
            "Return ONLY JSON.\n\n"
            f"MD&A extract:\n{(mda_text or '')[:20000]}"
        )
        parsed = self._ask_json(prompt=prompt, context=system_prompt)
        return {
            "revenue_driver": str(parsed.get("revenue_driver") or "").strip(),
            "margin_driver": str(parsed.get("margin_driver") or "").strip(),
            "competition_update": str(parsed.get("competition_update") or "").strip(),
            "guidance_shift": str(parsed.get("guidance_shift") or "").strip(),
        }

    def analyze_10k_content(self, ticker: str, raw_html: str) -> dict[str, Any]:
        print(f"[*] Parsing 10-K for {ticker}...")

        clean_text = self.parser.clean_html(raw_html)
        business_text = self.parser.extract_section(clean_text, "BUSINESS")
        risk_text = self.parser.extract_section(clean_text, "RISK")
        mda_text = self.parser.extract_section(clean_text, "MD&A")
        notes_text = self.parser.extract_section(clean_text, "NOTES")

        print("[*] Detecting Critical Risks...")
        risk_system = (
            "You are a short-seller analyst. Analyze Item 1A only. "
            "Ignore boilerplate and pick the single highest-severity failure mode. "
            "Prefer covenant/liquidity/refinancing/legal triggers with concrete wording. "
            "Output strictly valid JSON: "
            '{ "panic_score": 0-10, "killer_risk": "Short Title", "evidence": "Direct Quote" }'
        )
        if len(risk_text) > 100:
            risk_json = ask_ai(risk_system, risk_text, mode="smart", json_mode=True)
        else:
            risk_json = '{ "panic_score": 0, "killer_risk": "Data Missing", "evidence": "Could not find Item 1A" }'

        print("[*] Analyzing Moat Strategy...")
        moat_system = (
            "You are a value investor. Analyze MD&A with emphasis on pricing power, volume vs price mix, "
            "margin durability, and management credibility. Avoid generic conclusions. "
            "Output strictly valid JSON: "
            '{ "moat_score": 0-100, "trend": "Strengthening/Weakening", "justification": "Direct Quote" }'
        )
        if len(mda_text) > 100:
            moat_json = ask_ai(moat_system, mda_text, mode="smart", json_mode=True)
        else:
            moat_json = '{ "moat_score": 0, "trend": "Unknown", "justification": "Could not find Item 7" }'

        print("[*] Mapping Business Model...")
        biz_system = (
            "You are a fundamental investor. Analyze Item 1 Business with concrete specificity. "
            "Output strictly valid JSON: "
            '{ "summary": "What the company sells", "customers": "Who pays", "moat_source": "Why they win" }'
        )
        if len(business_text) > 100:
            biz_json = ask_ai(biz_system, business_text, mode="smart", json_mode=True)
        else:
            biz_json = '{ "summary": "Data Missing", "customers": "Data Missing", "moat_source": "Could not find Item 1" }'

        print("[*] Scanning Notes...")
        notes_system = (
            "You are a forensic accountant. Analyze Item 8 notes for debt/covenants, revenue recognition, and concentration. "
            "Avoid generic text; quote a concrete trigger phrase. "
            "Output strictly valid JSON: "
            '{ "debt_risk": "Low/Medium/High", "revenue_flag": "Safe/Aggressive", '
            '"concentration": "None/High", "evidence": "Direct Quote" }'
        )
        if len(notes_text) > 100:
            notes_json = ask_ai(notes_system, notes_text, mode="smart", json_mode=True)
        else:
            notes_json = '{ "debt_risk": "Unknown", "revenue_flag": "Unknown", "concentration": "Unknown", "evidence": "Could not find Item 8" }'

        def _as_json(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
            try:
                obj = json.loads(raw)
                return obj if isinstance(obj, dict) else dict(fallback)
            except Exception:
                return dict(fallback)

        return {
            "ticker": str(ticker or "").upper(),
            "risk_analysis": _as_json(risk_json, {"panic_score": 0, "killer_risk": "Parse Error", "evidence": ""}),
            "moat_analysis": _as_json(moat_json, {"moat_score": 0, "trend": "Unknown", "justification": ""}),
            "business_analysis": _as_json(
                biz_json, {"summary": "Parse Error", "customers": "Parse Error", "moat_source": "Parse Error"}
            ),
            "notes_analysis": _as_json(
                notes_json, {"debt_risk": "Unknown", "revenue_flag": "Unknown", "concentration": "Unknown", "evidence": ""}
            ),
            "sections_found": {
                "business_chars": len(business_text),
                "risk_chars": len(risk_text),
                "mda_chars": len(mda_text),
                "notes_chars": len(notes_text),
            },
        }

    def calculate_sentence_diff(self, old_text: str, new_text: str) -> list[dict[str, str]]:
        old_sents = self._sentence_tokenize(old_text)
        new_sents = self._sentence_tokenize(new_text)

        sm = difflib.SequenceMatcher(a=old_sents, b=new_sents)
        out: list[dict[str, str]] = []

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "insert":
                for s in new_sents[j1:j2]:
                    if s:
                        out.append({"type": "added", "text": s})
                continue
            if tag == "replace":
                old_chunk = old_sents[i1:i2]
                new_chunk = new_sents[j1:j2]
                pair_count = min(len(old_chunk), len(new_chunk))
                for k in range(pair_count):
                    o = old_chunk[k]
                    n = new_chunk[k]
                    ratio = difflib.SequenceMatcher(a=o, b=n).ratio()
                    if ratio >= 0.55:
                        out.append({"type": "modified", "text": n})
                    else:
                        out.append({"type": "added", "text": n})
                for s in new_chunk[pair_count:]:
                    if s:
                        out.append({"type": "added", "text": s})
                continue
            if tag == "delete":
                # Requirement asks for added/modified in new text only.
                continue

        seen: set[str] = set()
        deduped: list[dict[str, str]] = []
        for row in out:
            key = f"{row['type']}::{row['text'][:220]}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def analyze_diff_with_llama(self, diff_list: list[dict[str, str]]) -> str:
        """
        Backward-compatible text output for CLI/logging.
        For UI/data workflows, use analyze_diff_with_llama_structured().
        """
        result = self.analyze_diff_with_llama_structured(diff_list)
        return str(result.get("report_text") or "").strip()

    def analyze_diff_with_llama_structured(self, diff_list: list[dict[str, str]]) -> dict[str, Any]:
        if not diff_list:
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": "No material added/modified risk-factor sentences detected.",
            }

        lines: list[str] = []
        for i, d in enumerate(diff_list, 1):
            t = str(d.get("type") or "added").strip().lower()
            tx = re.sub(r"\s+", " ", str(d.get("text") or "")).strip()
            if not tx:
                continue
            lines.append(f"{i}. [{t}] {tx}")
        if not lines:
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": "No usable diff entries after cleaning.",
            }
        payload = "\n".join(lines)
        SYSTEM_PROMPT = """
You are a forensic financial analyst. Your job is to analyze changes in SEC Risk Factors.
You are NOT a summarizer. You are an EVIDENCE EXTRACTOR.
Follow these strict rules:
1. IGNORE BOILERPLATE: If a change is just rephrasing (e.g., changing "adverse effect" to "material impact"), ignore it.
2. HUNT FOR SPECIFICS: Look for NEW proper nouns (names of laws, agencies, competitors, regions), numbers (%, $ amounts), or timeframes.
3. DETECT TENSE SHIFTS: Flag any shift from hypothetical ("we may") to definite ("we are", "we have received").
4. MATERIALITY FIRST: prioritize covenant breach, going-concern, refinancing wall, customer concentration, regulatory action.
5. NON-GENERIC OUTPUT: "specific_trigger" must be a concrete phrase from the diff, not a broad summary.
Output format (JSON):
{
  "risks": [
    {
      "severity": 1-10, // Integer
      "category": "Legal" | "Ops" | "Macro" | "Cyber",
      "specific_trigger": "Quote the EXACT 3-5 words that triggered this flag",
      "analysis": "Explain WHY this matters in 10 words or less."
    }
  ]
}
"""

        chunks = self._chunk_text(payload, max_chars=12000)
        partials: list[list[dict[str, Any]]] = []
        for idx, ch in enumerate(chunks, 1):
            prompt = (
                "JSON_MODE=ON. Return ONLY valid JSON (no markdown, no commentary).\n"
                "Analyze the Item 1A diffs and return up to 8 risks.\n"
                "Use exactly this schema keys: risks[].severity, risks[].category, risks[].specific_trigger, risks[].analysis.\n\n"
                f"Chunk {idx}/{len(chunks)}\n"
                f"Diff entries:\n{ch}"
            )
            raw = ask_ai(SYSTEM_PROMPT, prompt, mode="smart", json_mode=True)
            parsed = self._parse_llm_risks(raw)
            if parsed:
                partials.append(parsed)

        merged_items: list[dict[str, Any]] = []
        for part in partials:
            merged_items.extend(part)

        if not merged_items:
            # Final fallback to a plain summary if JSON parsing fails.
            fallback_prompt = (
                "Summarize the 3 most material risk increases in this 10-K update. "
                "Include a severity score (1-10) per bullet."
            )
            txt = ask_ai(SYSTEM_PROMPT, fallback_prompt + "\n\n" + payload[:12000], mode="smart").strip()
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": txt,
            }

        # Normalize, sort, and dedupe by risk text.
        normalized: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row in merged_items:
            risk_text = re.sub(r"\s+", " ", str(row.get("specific_trigger") or "")).strip()
            if not risk_text:
                continue
            key = risk_text[:220].lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            try:
                sev = int(row.get("severity", 0))
            except Exception:
                sev = 0
            sev = max(1, min(10, sev)) if sev else 1
            category = str(row.get("category") or "Macro").strip()
            if category not in {"Legal", "Ops", "Macro", "Cyber"}:
                category = "Macro"
            why = re.sub(r"\s+", " ", str(row.get("analysis") or "")).strip()
            normalized.append(
                {
                    "severity_score": sev,
                    "change_type": category,
                    "risk_text": risk_text,
                    "why_it_matters": why,
                }
            )

        normalized.sort(key=lambda x: int(x["severity_score"]), reverse=True)
        top = normalized[:3]

        out_lines = ["Top Material Risk Increases (Ranked):", ""]
        for i, row in enumerate(top, 1):
            out_lines.append(
                f"{i}. [Severity {row['severity_score']}/10] ({row['change_type']}) {row['risk_text']}"
            )
            if row["why_it_matters"]:
                out_lines.append(f"   Why it matters: {row['why_it_matters']}")
        if len(normalized) > 3:
            out_lines.append("")
            out_lines.append(f"Additional scored risks detected: {len(normalized) - 3}")
        return {
            "ranked_changes": normalized,
            "top_3": top,
            "additional_count": max(0, len(normalized) - 3),
            "report_text": "\n".join(out_lines).strip(),
        }

    def run_full_deep_dive(self, ticker: str) -> dict[str, Any]:
        t = (ticker or "").strip().upper()
        if not t:
            raise ValueError("ticker is required")

        tenks = self.fetch_latest_10ks(t)
        latest_10k_path = tenks[0]
        prior_10k_path = tenks[1] if len(tenks) > 1 else tenks[0]
        proxy_path = self.fetch_latest_proxy(t)

        latest_10k_raw = self.load_filing_text(latest_10k_path)
        prior_10k_raw = self.load_filing_text(prior_10k_path)
        proxy_raw = self.load_filing_text(proxy_path)

        business_text = self.extract_business_section(latest_10k_raw)
        risk_new = self.extract_risk_factors(latest_10k_raw)
        risk_old = self.extract_risk_factors(prior_10k_raw)
        notes_text = self.extract_financial_notes(latest_10k_raw)
        proxy_text = self.extract_proxy_incentives(proxy_raw)
        mda_text = self._extract_item7_mda(latest_10k_raw)

        diff = self.calculate_sentence_diff(risk_old, risk_new)
        risk_structured = self.analyze_diff_with_llama_structured(diff)
        business_analysis = self.analyze_business_model(business_text)
        forensic_analysis = self.analyze_accounting_risks(notes_text)
        governance_analysis = self.analyze_governance(proxy_text)
        moat_analysis = self.analyze_moat_from_mda(mda_text) if mda_text else {}

        return {
            "ticker": t,
            "files": {
                "latest_10k": str(latest_10k_path),
                "prior_10k": str(prior_10k_path),
                "latest_proxy": str(proxy_path),
            },
            "business": business_analysis,
            "risk_factors": risk_structured,
            "mda_moat": moat_analysis,
            "financial_notes": forensic_analysis,
            "proxy_governance": governance_analysis,
        }

    # --------------------------- internals ---------------------------

    def _download_form_candidates(self, ticker: str, form: str, limit: int = 2) -> list[Path]:
        t = (ticker or "").strip().upper()
        f = (form or "").strip().upper()
        if not t or not f:
            return []
        candidates: list[Path] = []
        if self.downloader is not None:
            try:
                try:
                    self.downloader.get(f, t, limit=limit)
                except TypeError:
                    self.downloader.get(f, t, amount=limit)
                base = self.download_dir / "sec-edgar-filings" / t / f
                if base.exists():
                    for p in base.rglob("*"):
                        if p.is_file() and p.name.lower().endswith((".htm", ".html", ".xhtml", ".txt")):
                            candidates.append(p)
            except Exception:
                pass
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates

    def _find_toc_anchor(self, soup: BeautifulSoup, patterns: list[str]) -> str | None:
        regs = [re.compile(p, re.IGNORECASE) for p in (patterns or [])]
        links = soup.find_all("a", href=True)
        if not links:
            return None
        best_href: str | None = None
        best_score = -999
        for a in links:
            href = str(a.get("href") or "").strip()
            if not href.startswith("#"):
                continue
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not txt:
                continue
            score = 0
            for i, rgx in enumerate(regs):
                if rgx.search(txt):
                    score += 10 - i
            if score <= 0:
                continue
            blob = ""
            if isinstance(a.parent, Tag):
                blob = (
                    (a.parent.get("id", "") or "")
                    + " "
                    + " ".join(a.parent.get("class", []) or [])
                ).lower()
            if any(k in blob for k in ("toc", "table", "content", "index")):
                score += 2
            if score > best_score:
                best_score = score
                best_href = href
        return best_href

    def _extract_section_via_toc(
        self,
        html_content: str,
        start_patterns: list[str],
        end_patterns: list[str],
        min_chars: int = 300,
    ) -> str:
        soup = BeautifulSoup(html_content, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.decompose()
        start_href = self._find_toc_anchor(soup, start_patterns)
        if not start_href:
            return ""
        start_tag = self._resolve_anchor_target(soup, start_href)
        if start_tag is None:
            return ""
        out_parts: list[str] = []
        end_regs = [re.compile(p, re.IGNORECASE) for p in (end_patterns or [])]
        node: Any = start_tag
        while node is not None:
            if isinstance(node, Tag):
                txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                if txt:
                    if node is not start_tag and any(r.search(txt[:120]) for r in end_regs):
                        break
                    out_parts.append(txt)
            node = node.next_sibling
            if node is None and isinstance(start_tag.parent, Tag):
                start_tag = start_tag.parent
                node = start_tag.next_sibling
        text = re.sub(r"\s+", " ", "\n".join(out_parts)).strip()
        return text if len(text) >= min_chars else ""

    def _extract_item7_mda(self, html_content: str) -> str:
        cleaned = self.parser.clean_html(html_content)
        quick = self.parser.extract_section(cleaned, "MD&A", max_chars=35000).strip()
        if len(quick) >= 800:
            return re.sub(r"\s+", " ", quick).strip()
        out = self._extract_section_via_toc(
            html_content=html_content,
            start_patterns=[
                r"\bitem\s*7\b",
                r"\bmanagement[’']?s discussion",
                r"\bresults of operations\b",
            ],
            end_patterns=[r"\bitem\s*7a\b", r"\bitem\s*8\b"],
            min_chars=800,
        )
        return out

    def _extract_notes_focus(self, text: str, max_chars: int = 20000) -> str:
        t = re.sub(r"\s+", " ", text or "").strip()
        if not t:
            return ""
        # Prefer accounting-policy/notes areas first if present.
        anchors = [
            r"significant accounting policies",
            r"notes to (consolidated )?financial statements",
            r"revenue recognition",
            r"debt",
            r"concentration",
        ]
        best_start = 0
        for pat in anchors:
            m = re.search(pat, t, flags=re.IGNORECASE)
            if m:
                best_start = m.start()
                break
        chunk = t[best_start : best_start + max_chars]
        return chunk

    def _ask_json(self, prompt: str, context: str) -> dict[str, Any]:
        raw = ask_ai(context, prompt, mode="smart", json_mode=True)
        txt = self._strip_markdown_fences(raw or "")
        if not txt:
            return {}
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        m = txt.find("{")
        n = txt.rfind("}")
        if m >= 0 and n > m:
            try:
                obj = json.loads(txt[m : n + 1])
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    def _find_item_1a_anchor_from_toc(self, soup: BeautifulSoup) -> str | None:
        links = soup.find_all("a", href=True)
        if not links:
            return None

        # Prefer links that are likely in table of contents regions.
        def _score(a: Tag) -> int:
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).lower()
            href = (a.get("href") or "").strip()
            if not href.startswith("#"):
                return -100
            if "item" not in txt and "risk factors" not in txt:
                return -50
            s = 0
            if re.search(r"item\s*1a", txt):
                s += 8
            if "risk factors" in txt:
                s += 6
            parent_blob = " ".join([
                a.parent.get("id", "") if isinstance(a.parent, Tag) else "",
                " ".join(a.parent.get("class", [])) if isinstance(a.parent, Tag) else "",
            ]).lower()
            if any(k in parent_blob for k in ("toc", "table", "content", "index")):
                s += 3
            return s

        ranked = sorted(links, key=_score, reverse=True)
        best = ranked[0] if ranked else None
        if best is None or _score(best) < 6:
            return None
        return str(best.get("href") or "").strip()

    def _resolve_anchor_target(self, soup: BeautifulSoup, href: str) -> Tag | None:
        anchor = href.lstrip("#").strip()
        if not anchor:
            return None

        # Exact match first.
        tgt = soup.find(id=anchor)
        if isinstance(tgt, Tag):
            return tgt
        tgt = soup.find(attrs={"name": anchor})
        if isinstance(tgt, Tag):
            return tgt

        # SEC docs often normalize ids differently.
        norm = re.sub(r"[^a-z0-9]", "", anchor.lower())
        if not norm:
            return None

        for tag in soup.find_all(True):
            tid = str(tag.get("id") or "")
            tname = str(tag.get("name") or "")
            cand = re.sub(r"[^a-z0-9]", "", (tid or tname).lower())
            if cand and (cand == norm or norm in cand or cand in norm):
                return tag
        return None

    def _extract_until_next_item(self, start_tag: Tag) -> str:
        end_patterns = [
            re.compile(r"\bitem\s*1b\b", re.IGNORECASE),
            re.compile(r"\bitem\s*2\b", re.IGNORECASE),
        ]
        out_parts: list[str] = []

        node: Any = start_tag
        while node is not None:
            if isinstance(node, Tag):
                txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                if txt:
                    if node is not start_tag and any(p.search(txt[:80]) for p in end_patterns):
                        break
                    out_parts.append(txt)
            node = node.next_sibling
            # If sibling chain ends, walk to parent's sibling for broader coverage.
            if node is None and isinstance(start_tag.parent, Tag):
                start_tag = start_tag.parent
                node = start_tag.next_sibling

        return "\n".join(out_parts)

    def _sentence_tokenize(self, text: str) -> list[str]:
        src = re.sub(r"\s+", " ", text or "").strip()
        if not src:
            return []
        if sent_tokenize is not None:
            try:
                return [s.strip() for s in sent_tokenize(src) if s and s.strip()]
            except LookupError:
                # punkt not downloaded
                pass
            except Exception:
                pass
        # Fallback tokenizer
        return [s.strip() for s in re.split(r"(?<=[\.!?])\s+", src) if s.strip()]

    def _chunk_text(self, text: str, max_chars: int = 12000) -> list[str]:
        s = text or ""
        if len(s) <= max_chars:
            return [s]
        chunks: list[str] = []
        i = 0
        while i < len(s):
            j = min(i + max_chars, len(s))
            if j < len(s):
                cut = s.rfind("\n", i, j)
                if cut > i + (max_chars // 2):
                    j = cut
            chunks.append(s[i:j])
            i = j
        return chunks

    def _strip_markdown_fences(self, txt: str) -> str:
        s = (txt or "").strip()
        if not s:
            return s
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
        return s

    def _parse_llm_risks(self, raw: str) -> list[dict[str, Any]]:
        txt = self._strip_markdown_fences(raw)
        if not txt:
            return []
        # Try exact JSON, then object extraction.
        objs: list[Any] = []
        try:
            objs.append(json.loads(txt))
        except Exception:
            pass
        if not objs:
            m = txt.find("{")
            n = txt.rfind("}")
            if m >= 0 and n > m:
                try:
                    objs.append(json.loads(txt[m : n + 1]))
                except Exception:
                    pass
        # One more attempt after removing any remaining code fences/comments.
        if not objs:
            try:
                candidate = re.sub(r"^\s*```.*?$|^\s*```$", "", txt, flags=re.MULTILINE)
                objs.append(json.loads(candidate))
            except Exception:
                pass
        for obj in objs:
            if isinstance(obj, dict) and isinstance(obj.get("risks"), list):
                risks = [x for x in obj["risks"] if isinstance(x, dict)]
                if risks:
                    return self._validate_risk_rows(risks)
        return []

    def _validate_risk_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                sev = int(row.get("severity", 0))
            except Exception:
                continue
            if sev < 1 or sev > 10:
                continue

            cat = str(row.get("category") or "").strip()
            if cat not in {"Legal", "Ops", "Macro", "Cyber"}:
                continue

            trig = re.sub(r"\s+", " ", str(row.get("specific_trigger") or "")).strip()
            if not trig:
                continue
            wc = len([w for w in trig.split(" ") if w])
            if wc < 3 or wc > 5:
                continue

            analysis = re.sub(r"\s+", " ", str(row.get("analysis") or "")).strip()
            if not analysis:
                continue
            awc = len([w for w in analysis.split(" ") if w])
            if awc > 10:
                continue

            out.append(
                {
                    "severity": sev,
                    "category": cat,
                    "specific_trigger": trig,
                    "analysis": analysis,
                }
            )
        return out

    def _extract_primary_10k_payload(self, raw: str) -> str:
        """
        SEC full-submission files can contain multiple DOCUMENT blocks,
        including uuencoded/binary exhibit blobs. Parsing the full bundle as HTML
        can trigger parser errors. Extract only the main 10-K TEXT payload.
        """
        s = raw or ""
        if "<DOCUMENT>" not in s.upper():
            return s

        best_fallback = ""
        for m in re.finditer(r"<DOCUMENT>(.*?)</DOCUMENT>", s, flags=re.IGNORECASE | re.DOTALL):
            doc = m.group(1) or ""
            t_m = re.search(r"<TYPE>\s*([^\n<]+)", doc, flags=re.IGNORECASE)
            typ = (t_m.group(1).strip().upper() if t_m else "")
            x_m = re.search(r"<TEXT>(.*)", doc, flags=re.IGNORECASE | re.DOTALL)
            payload = (x_m.group(1) if x_m else doc).strip()
            if not payload:
                continue
            if typ in {"10-K", "10K", "10-K405"}:
                return payload
            if not best_fallback:
                low = payload[:20000].lower()
                if "<html" in low or "item 1a" in low or "risk factors" in low:
                    best_fallback = payload

        return best_fallback or s

    def _fallback_latest_10ks_from_local_db(self, ticker: str) -> list[Path]:
        root = Path("/Users/solmaz/Investment_Tools")
        db = root / "data" / "research.db"
        if not db.exists():
            return []
        out: list[Path] = []
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT path FROM filings WHERE ticker = ? AND form = '10-K' ORDER BY date DESC LIMIT 2",
                (ticker,),
            ).fetchall()
            for (path_s,) in rows:
                ptxt = str(path_s or "").strip()
                if not ptxt:
                    continue
                p = Path(ptxt)
                if not p.is_absolute():
                    p = (root / p).resolve()
                if p.exists() and p.is_file():
                    out.append(p)
        except Exception:
            return []
        finally:
            conn.close()
        return out

    def _fallback_latest_forms_from_local_db(self, ticker: str, forms: list[str], limit: int = 1) -> list[Path]:
        root = Path("/Users/solmaz/Investment_Tools")
        db = root / "data" / "research.db"
        if not db.exists() or not forms:
            return []
        forms_u = [f.strip().upper() for f in forms if f and f.strip()]
        if not forms_u:
            return []
        placeholders = ",".join(["?"] * len(forms_u))
        q = (
            "SELECT path FROM filings "
            f"WHERE ticker = ? AND UPPER(form) IN ({placeholders}) "
            "ORDER BY date DESC LIMIT ?"
        )
        params: list[Any] = [ticker] + forms_u + [int(limit)]
        out: list[Path] = []
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(q, tuple(params)).fetchall()
            for (path_s,) in rows:
                ptxt = str(path_s or "").strip()
                if not ptxt:
                    continue
                p = Path(ptxt)
                if not p.is_absolute():
                    p = (root / p).resolve()
                if p.exists() and p.is_file():
                    out.append(p)
        except Exception:
            return []
        finally:
            conn.close()
        return out


if __name__ == "__main__":
    ticker = "MSFT"
    analyzer = SECFilingAnalyzer(download_dir="data/sec_filings")
    report = analyzer.run_full_deep_dive(ticker)
    print(json.dumps(report, indent=2, ensure_ascii=False))
