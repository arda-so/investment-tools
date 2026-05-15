#!/usr/bin/env python3
from __future__ import annotations

import re

from bs4 import BeautifulSoup


class SmartParser:
    def __init__(self) -> None:
        # Regex to find specific SEC sections.
        self.sections = {
            "BUSINESS": {
                "start": r"Item\s+1[\.\:\s]+Business",
                "end": r"Item\s+1A[\.\:\s]+Risk\s+Factors",
            },
            "RISK": {
                "start": r"Item\s+1A[\.\:\s]+Risk\s+Factors",
                "end": r"Item\s+1B[\.\:\s]+Unresolved\s+Staff",
            },
            "MD&A": {
                "start": r"Item\s+7[\.\:\s]+Management",
                "end": r"Item\s+7A[\.\:\s]+Quantitative",
            },
            "NOTES": {
                "start": r"Item\s+8[\.\:\s]+Financial\s+Statements",
                "end": r"Item\s+9[\.\:\s]+Changes\s+in",
            },
        }

    def clean_html(self, html_content: str) -> str:
        """Remove noisy tags, keep dense filing text."""
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")

        for element in soup(["script", "style", "table", "svg", "noscript"]):
            element.extract()

        text = soup.get_text(separator="\n")
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(chunk for chunk in lines if chunk)

    def extract_section(self, text: str, section_name: str, max_chars: int = 25000) -> str:
        """Return text between section start/end markers."""
        if section_name not in self.sections:
            return ""
        pattern = self.sections[section_name]
        match = re.search(
            f"({pattern['start']})(.*?)({pattern['end']})",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return match.group(2)[:max_chars] if match else ""
