from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.core.normalize import normalize_text as _norm


def pick_report_for_query(query: str, rows: list[dict]) -> dict | None:
    if not rows:
        return None
    query_low = _norm(query)
    if not query_low:
        return rows[0]
    best = None
    best_score = -1.0
    for row in rows:
        name = str(row.get("name") or "").strip()
        title = str(row.get("title") or "").strip()
        kind = str(row.get("kind") or "").strip()
        haystack = _norm(f"{name} {title} {kind}")
        if not haystack:
            continue
        score = SequenceMatcher(None, query_low, haystack).ratio()
        if query_low in haystack:
            score += 0.4
        if score > best_score:
            best_score = score
            best = row
    return best or rows[0]


def extract_section_request(query: str) -> str:
    raw = str(query or "").strip()
    if not raw:
        return ""
    quoted = re.search(r"[\"“](.+?)[\"”]", raw)
    if quoted:
        return str(quoted.group(1) or "").strip()[:120]
    match = re.search(
        r"\b(section|part|excerpt|focus|about|on)\s+(.+?)(?:\s+\b(from|in)\b.*|$)",
        raw,
        flags=re.I,
    )
    if match:
        return str(match.group(2) or "").strip()[:120]
    low = _norm(raw)
    for keyword in (
        "risk factors",
        "md&a",
        "management discussion",
        "segment",
        "earnings",
        "guidance",
        "capital allocation",
        "revenue",
        "margin",
    ):
        if keyword in low:
            return keyword
    return ""


def extract_report_section_excerpt(report_text: str, section_query: str, max_chars: int = 2200) -> str:
    text = str(report_text or "")
    section = _norm(section_query)
    if not text or not section:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""

    heading_indexes: list[tuple[int, str]] = []
    for idx, raw in enumerate(lines):
        line = str(raw or "").strip()
        if not line:
            continue
        if line.startswith("#") or re.match(r"^(ITEM|Item)\s+\d+[A-Z]?\b", line):
            heading_indexes.append((idx, line))

    section_tokens = [token for token in section.split() if len(token) >= 3][:6]
    for idx, heading in heading_indexes:
        heading_norm = _norm(heading)
        if section in heading_norm or (
            section_tokens
            and sum(1 for token in section_tokens if token in heading_norm) >= max(1, min(2, len(section_tokens)))
        ):
            end = min(len(lines), idx + 1 + 180)
            for next_idx, _ in heading_indexes:
                if next_idx > idx:
                    end = next_idx
                    break
            return "\n".join(lines[idx:end]).strip()[:max_chars].strip()

    hit_idx = -1
    for idx, raw in enumerate(lines):
        line = str(raw or "").strip()
        if not line:
            continue
        line_norm = _norm(line)
        if section in line_norm or (
            section_tokens
            and sum(1 for token in section_tokens if token in line_norm) >= max(1, min(2, len(section_tokens)))
        ):
            hit_idx = idx
            break
    if hit_idx >= 0:
        lo = max(0, hit_idx - 8)
        hi = min(len(lines), hit_idx + 26)
        return "\n".join(lines[lo:hi]).strip()[:max_chars].strip()
    return ""
