#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.terminal_app import generate_l2_digest_report


def main() -> None:
    out = generate_l2_digest_report(force_recompute=True)
    print(Path(out).name)
    print(out)


if __name__ == "__main__":
    main()
