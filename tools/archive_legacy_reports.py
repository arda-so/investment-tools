#!/usr/bin/env python3
"""Archive legacy reports so reports/ stays focused on Terminal outputs."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
ARCHIVE_ROOT = REPORTS / "legacy_archive"


DAILY_PATTERNS = [
    "morning_intelligence_{d}.md",
    "daily_brief_{d}.md",
    "red_flag_alert_{d}.txt",
    "earnings_radar_{d}.md",
    "signal_tracker_{d}.md",
    "market_scanner_{d}.md",
    "daily_brief_{d}.md.error.log",
]

WEEKLY_PATTERNS = [
    "weekly_report_{d}.md",
    "thesis_check_{d}.md",
    "valuation_monitor_{d}.md",
    "quarterly_longterm_{d}.md",
]

MONTHLY_PATTERNS = [
    "monthly_report_{d}.md",
]

ALL_PATTERNS = [
    "morning_intelligence_*.md",
    "daily_brief_*.md",
    "red_flag_alert_*.txt",
    "earnings_radar_*.md",
    "signal_tracker_*.md",
    "market_scanner_*.md",
    "daily_brief_*.md.error.log",
    "weekly_report_*.md",
    "monthly_report_*.md",
    "thesis_check_*.md",
    "valuation_monitor_*.md",
    "quarterly_longterm_*.md",
]


def _move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    if target.exists():
        target.unlink()
    shutil.move(str(path), str(target))
    print(f"Archived: {path.name} -> {dest_dir}")


def _archive_by_patterns(patterns: list[str], date_tag: str) -> None:
    dest = ARCHIVE_ROOT / date_tag
    for p in patterns:
        name = p.format(d=date_tag)
        path = REPORTS / name
        if path.exists():
            _move(path, dest)


def _archive_all() -> None:
    dest = ARCHIVE_ROOT / "bulk"
    for pattern in ALL_PATTERNS:
        for path in REPORTS.glob(pattern):
            _move(path, dest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive legacy non-Terminal reports.")
    parser.add_argument("--scope", choices=["daily", "weekly", "monthly", "all"], default="daily")
    parser.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"))
    args = parser.parse_args()

    if args.scope == "all":
        _archive_all()
        return
    if args.scope == "daily":
        _archive_by_patterns(DAILY_PATTERNS, args.date)
        return
    if args.scope == "weekly":
        _archive_by_patterns(WEEKLY_PATTERNS, args.date)
        return
    if args.scope == "monthly":
        _archive_by_patterns(MONTHLY_PATTERNS, args.date)


if __name__ == "__main__":
    main()
