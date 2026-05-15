#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
HOTPATHS = [
    ROOT / "app" / "main.py",
    ROOT / "app" / "routers" / "dashboard.py",
    ROOT / "app" / "services" / "workspace_os_service.py",
    ROOT / "app" / "services" / "proactive_ai_service.py",
    ROOT / "app" / "services" / "portfolio_memory_service.py",
]
PATTERN = re.compile(r"except(?:\s+Exception)?\s*:\s*pass\b")


def main() -> int:
    failures: list[str] = []
    for path in HOTPATHS:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            failures.append(f"{path}: read_failed: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PATTERN.search(line):
                failures.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    if failures:
        print("[silent-pass] FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("[silent-pass] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
