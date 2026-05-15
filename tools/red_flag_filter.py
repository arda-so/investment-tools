#!/usr/bin/env python3
"""Filter filing change output into red-flag alert blocks."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
TERMS_FILE = ROOT / "data" / "red_flag_terms.txt"


def load_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.lower())
    return out


def split_blocks(text: str) -> list[list[str]]:
    lines = text.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if ln.lstrip().startswith("[~]"):
            if cur:
                blocks.append(cur)
            cur = [ln]
        else:
            if cur:
                cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def filter_blocks(blocks: list[list[str]], terms: list[str]) -> list[list[str]]:
    out = []
    for b in blocks:
        hay = "\n".join(b).lower()
        if any(t in hay for t in terms):
            out.append(b)
    return out


def fallback_line_filter(text: str, terms: list[str]) -> str:
    out = []
    for ln in text.splitlines():
        l = ln.lower()
        if any(t in l for t in terms):
            out.append(ln)
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Extract red-flag change blocks from stdin.")
    p.add_argument("--terms-file", default=str(TERMS_FILE))
    args = p.parse_args()

    terms = load_terms(Path(args.terms_file))
    if not terms:
        return

    text = sys.stdin.read()
    if not text.strip():
        return

    blocks = split_blocks(text)
    if blocks:
        matched = filter_blocks(blocks, terms)
        if matched:
            print("\n".join("\n".join(b).rstrip() for b in matched))
        return

    out = fallback_line_filter(text, terms)
    if out.strip():
        print(out)


if __name__ == "__main__":
    main()
