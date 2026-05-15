#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from modules.sec_filing_analyzer import ExtractionError, SECFilingAnalyzer
from tools.llm_engine import ask_ai


class SECMDAAnalyzer:
    """
    MD&A diff engine focused on management quality signals:
    accountability, quality of earnings, capital allocation, competition, guidance.
    """

    def __init__(self, download_dir: str = "data/sec_filings") -> None:
        self.base = SECFilingAnalyzer(download_dir=download_dir)

    def fetch_latest_10ks(self, ticker: str) -> list[Path]:
        return self.base.fetch_latest_10ks(ticker)

    def load_filing_text(self, path: Path) -> str:
        return self.base.load_filing_text(path)

    def calculate_sentence_diff(self, old_text: str, new_text: str) -> list[dict[str, str]]:
        return self.base.calculate_sentence_diff(old_text, new_text)

    def extract_mda(self, html_content: str) -> str:
        if not (html_content or "").strip():
            raise ExtractionError("Empty filing content.")
        soup = BeautifulSoup(html_content, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.decompose()
        href = self._find_item7_anchor_from_toc(soup)
        if not href:
            raise ExtractionError("Could not locate Item 7 (MD&A) anchor from Table of Contents.")
        start = self._resolve_anchor_target(soup, href)
        if start is None:
            raise ExtractionError(f"Resolved ToC href '{href}' but anchor target was not found.")
        text = self._extract_until_next_item(start)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 600:
            raise ExtractionError("Extracted MD&A text is too short; likely malformed boundaries.")
        return text

    def analyze_diff_with_llama_structured(self, diff_list: list[dict[str, str]]) -> dict[str, Any]:
        if not diff_list:
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": "No material MD&A changes detected.",
            }
        lines: list[str] = []
        for i, d in enumerate(diff_list[:220], 1):
            t = str(d.get("type") or "added").strip().lower()
            tx = re.sub(r"\s+", " ", str(d.get("text") or "")).strip()
            if tx:
                lines.append(f"{i}. [{t}] {tx}")
        if not lines:
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": "No usable MD&A diff entries after cleaning.",
            }
        payload = "\n".join(lines)
        system_prompt = """
You are a forensic buy-side analyst reading MD&A changes.
You are NOT a summarizer. You are an EVIDENCE EXTRACTOR.
Apply these rules:
1) Accountability test: detect if management owns mistakes vs blames macro/weather/FX.
2) Quality of earnings: flag emphasis on Adjusted EBITDA/Non-GAAP vs GAAP/FCF/owner earnings.
3) Capital allocation: identify buybacks, dilution, vague strategic spend without return targets.
4) Clarity vs jargon: flag excessive buzzwords with low operational specificity.
5) Competition/moat: extract explicit pricing power, volume, competitor references, guidance shifts.
6) Prefer high-materiality deltas: margin inflection, demand slowdown, pricing erosion, or lowered outlook.
Return JSON only:
{
  "signals": [
    {
      "severity": 1-10,
      "theme": "Accountability|QualityOfEarnings|CapitalAllocation|Competition|Guidance|Clarity",
      "direction": "Positive|Negative|Mixed",
      "specific_trigger": "EXACT 4-12 words from the diff",
      "analysis": "WHY this matters in <=18 words, concrete not generic"
    }
  ]
}
"""
        prompt = (
            "JSON_MODE=ON. Return ONLY valid JSON.\n"
            "Extract up to 12 material MD&A signals from the diffs below.\n"
            "Use exactly keys: signals[].severity, theme, direction, specific_trigger, analysis.\n\n"
            f"MD&A diffs:\n{payload[:120000]}"
        )
        raw = ask_ai(prompt, system_prompt)
        rows = self._parse_llm_signals(raw)
        if not rows:
            fallback = ask_ai(
                "Give top 3 MD&A changes for revenue/margin/competition and why they matter. "
                "Use concrete quoted trigger phrases from the provided diff lines.\n\n"
                f"MD&A diffs:\n{payload[:120000]}",
                system_prompt,
            ).strip()
            return {
                "ranked_changes": [],
                "top_3": [],
                "additional_count": 0,
                "report_text": fallback or "No MD&A signals returned.",
            }

        norm: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            trig = re.sub(r"\s+", " ", str(row.get("specific_trigger") or "")).strip()
            if not trig:
                continue
            k = trig[:220].lower()
            if k in seen:
                continue
            seen.add(k)
            sev = max(1, min(10, int(row.get("severity", 1))))
            theme = str(row.get("theme") or "Guidance").strip()
            direction = str(row.get("direction") or "Mixed").strip()
            why = re.sub(r"\s+", " ", str(row.get("analysis") or "")).strip()
            norm.append(
                {
                    "severity_score": sev,
                    "change_type": f"{theme}/{direction}",
                    "risk_text": trig,
                    "why_it_matters": why,
                }
            )
        norm.sort(key=lambda x: int(x["severity_score"]), reverse=True)
        top = norm[:3]
        out_lines = ["MD&A Intelligence (Buffett Lens):", ""]
        for i, r in enumerate(top, 1):
            out_lines.append(
                f"{i}. [Severity {r['severity_score']}/10] ({r['change_type']}) {r['risk_text']}"
            )
            if r["why_it_matters"]:
                out_lines.append(f"   Why it matters: {r['why_it_matters']}")
        if len(norm) > 3:
            out_lines.append("")
            out_lines.append(f"Additional scored signals detected: {len(norm) - 3}")
        return {
            "ranked_changes": norm,
            "top_3": top,
            "additional_count": max(0, len(norm) - 3),
            "report_text": "\n".join(out_lines).strip(),
        }

    def _find_item7_anchor_from_toc(self, soup: BeautifulSoup) -> str | None:
        links = soup.find_all("a", href=True)
        if not links:
            return None

        def _score(a: Tag) -> int:
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).lower()
            href = (a.get("href") or "").strip()
            if not href.startswith("#"):
                return -100
            s = 0
            if re.search(r"\bitem\s*7\b", txt):
                s += 8
            if "management" in txt and "analysis" in txt:
                s += 6
            blob = " ".join(
                [
                    a.parent.get("id", "") if isinstance(a.parent, Tag) else "",
                    " ".join(a.parent.get("class", [])) if isinstance(a.parent, Tag) else "",
                ]
            ).lower()
            if any(k in blob for k in ("toc", "table", "content", "index")):
                s += 3
            if re.search(r"\bitem\s*7a\b", txt):
                s -= 8
            return s

        ranked = sorted(links, key=_score, reverse=True)
        if not ranked or _score(ranked[0]) < 6:
            return None
        return str(ranked[0].get("href") or "").strip()

    def _resolve_anchor_target(self, soup: BeautifulSoup, href: str) -> Tag | None:
        anchor = href.lstrip("#").strip()
        if not anchor:
            return None
        tgt = soup.find(id=anchor)
        if isinstance(tgt, Tag):
            return tgt
        tgt = soup.find(attrs={"name": anchor})
        if isinstance(tgt, Tag):
            return tgt
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
            re.compile(r"\bitem\s*7a\b", re.IGNORECASE),
            re.compile(r"\bitem\s*8\b", re.IGNORECASE),
        ]
        out_parts: list[str] = []
        node: Any = start_tag
        while node is not None:
            if isinstance(node, Tag):
                txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                if txt:
                    if node is not start_tag and any(p.search(txt[:120]) for p in end_patterns):
                        break
                    out_parts.append(txt)
            node = node.next_sibling
            if node is None and isinstance(start_tag.parent, Tag):
                start_tag = start_tag.parent
                node = start_tag.next_sibling
        return "\n".join(out_parts)

    def _strip_markdown_fences(self, txt: str) -> str:
        s = (txt or "").strip()
        if not s:
            return s
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
        return s

    def _parse_llm_signals(self, raw: str) -> list[dict[str, Any]]:
        txt = self._strip_markdown_fences(raw)
        if not txt:
            return []
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
        for obj in objs:
            if isinstance(obj, dict) and isinstance(obj.get("signals"), list):
                return self._validate_signals([x for x in obj["signals"] if isinstance(x, dict)])
        return []

    def _validate_signals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        allowed_themes = {
            "Accountability",
            "QualityOfEarnings",
            "CapitalAllocation",
            "Competition",
            "Guidance",
            "Clarity",
        }
        allowed_dir = {"Positive", "Negative", "Mixed"}
        for r in rows:
            try:
                sev = int(r.get("severity", 0))
            except Exception:
                continue
            if sev < 1 or sev > 10:
                continue
            theme = str(r.get("theme") or "").strip()
            if theme not in allowed_themes:
                continue
            direction = str(r.get("direction") or "").strip()
            if direction not in allowed_dir:
                continue
            trig = re.sub(r"\s+", " ", str(r.get("specific_trigger") or "")).strip()
            tw = len([w for w in trig.split(" ") if w])
            if tw < 3 or tw > 8:
                continue
            ana = re.sub(r"\s+", " ", str(r.get("analysis") or "")).strip()
            aw = len([w for w in ana.split(" ") if w])
            if not ana or aw > 12:
                continue
            out.append(
                {
                    "severity": sev,
                    "theme": theme,
                    "direction": direction,
                    "specific_trigger": trig,
                    "analysis": ana,
                }
            )
        return out
