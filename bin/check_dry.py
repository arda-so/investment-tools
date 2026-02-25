#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"


# Forbidden legacy helper names. These caused duplicate logic in past refactors.
FORBIDDEN_DEFS = {
    "_conn",
    "_norm",
    "_normalize_ticker",
    "_read_watchlist_file",
}


def main() -> int:
    violations: list[str] = []
    pattern = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            m = pattern.match(line)
            if not m:
                continue
            fn = m.group(1)
            if fn in FORBIDDEN_DEFS:
                violations.append(f"{rel}:{i}: forbidden duplicate helper `{fn}`")

    if violations:
        print("DRY check failed. Forbidden helper definitions found:")
        for v in violations:
            print(f"- {v}")
        return 1

    print("DRY check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
