#!/usr/bin/env python3
"""Build consolidated Terminal reports from existing report artifacts.

This script is additive: it never deletes or rewrites legacy reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
INPUTS = REPORTS / ".terminal_inputs"


def _today_tag() -> str:
    return dt.date.today().strftime("%Y%m%d")


def _latest(pattern: str) -> Optional[Path]:
    files = []
    if INPUTS.exists():
        files.extend(INPUTS.glob(pattern))
    files.extend(REPORTS.glob(pattern))
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _latest_quarterly_full() -> Optional[Path]:
    files = [
        p
        for p in list(REPORTS.glob("quarterly_report_*.md")) + (list(INPUTS.glob("quarterly_report_*.md")) if INPUTS.exists() else [])
        if not p.name.endswith("_brief.md")
    ]
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _read(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _section(title: str, path: Optional[Path], required: bool = False) -> str:
    if not path:
        return f"## {title}\n\n*Missing source report.*\n" if required else ""
    body = _read(path)
    if not body:
        return f"## {title}\n\n*Source report exists but is empty: `{path.name}`*\n"
    return f"## {title}\n\n*Source: `{path.name}`*\n\n{body}\n"


def build_daily() -> Path:
    tag = _today_tag()
    out = REPORTS / f"terminal_daily_brief_{tag}.md"

    morning = _latest("morning_intelligence_*.md")
    daily = _latest("daily_brief_*.md")
    redflag = _latest("red_flag_alert_*.txt")

    txt = [
        f"# Terminal: Daily Brief — {dt.date.today():%A, %B %d, %Y}",
        "",
        "This is the consolidated front page for daily decision-making.",
        "",
        _section("Macro Snapshot", morning, required=True),
        _section("Filing Intelligence", daily, required=True),
        _section("Red Flags", redflag),
    ]
    out.write_text("\n".join(txt).strip() + "\n", encoding="utf-8")
    return out


def build_appendix() -> Path:
    tag = _today_tag()
    out = REPORTS / f"terminal_appendix_{tag}.md"

    earnings = _latest("earnings_radar_*.md")
    signals = _latest("signal_tracker_*.md")

    txt = [
        f"# Terminal: Appendix — Earnings & Signals — {dt.date.today():%A, %B %d, %Y}",
        "",
        "Detailed data sections that support the Daily Brief.",
        "",
        _section("Earnings Radar", earnings, required=True),
        _section("Signal Tracker", signals, required=True),
    ]
    out.write_text("\n".join(txt).strip() + "\n", encoding="utf-8")
    return out


def build_weekly() -> Path:
    tag = _today_tag()
    out = REPORTS / f"terminal_weekly_outlook_{tag}.md"

    weekly = _latest("weekly_report_*.md")
    quarterly_brief = _latest("quarterly_report_*_brief.md")
    thesis = _latest("thesis_check_*.md")
    valuation = _latest("valuation_monitor_*.md")

    txt = [
        f"# Terminal: Weekly Outlook — {dt.date.today():%A, %B %d, %Y}",
        "",
        "Strategic weekly synthesis and positioning context.",
        "",
        _section("Weekly Filing Synthesis", weekly, required=True),
        _section("Quarterly Context (Brief)", quarterly_brief),
        _section("Thesis Health", thesis),
        _section("Valuation Monitor", valuation),
    ]
    out.write_text("\n".join(txt).strip() + "\n", encoding="utf-8")
    return out


def build_monthly() -> Path:
    tag = _today_tag()
    out = REPORTS / f"terminal_monthly_ic_memo_{tag}.md"

    monthly = _latest("monthly_report_*.md")
    quarterly = _latest_quarterly_full()
    valuation = _latest("valuation_monitor_*.md")
    thesis = _latest("thesis_check_*.md")

    txt = [
        f"# Terminal: Monthly IC Memo — {dt.date.today():%A, %B %d, %Y}",
        "",
        "Investment-committee level monthly memo.",
        "",
        _section("Monthly Deep Dive", monthly, required=True),
        _section("Quarterly Deep Dive", quarterly),
        _section("Valuation Monitor", valuation),
        _section("Thesis Health", thesis),
    ]
    out.write_text("\n".join(txt).strip() + "\n", encoding="utf-8")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Build consolidated Terminal reports.")
    p.add_argument("--daily", action="store_true")
    p.add_argument("--appendix", action="store_true")
    p.add_argument("--weekly", action="store_true")
    p.add_argument("--monthly", action="store_true")
    args = p.parse_args()

    if not any([args.daily, args.appendix, args.weekly, args.monthly]):
        args.daily = args.appendix = args.weekly = args.monthly = True

    built = []
    if args.daily:
        built.append(build_daily())
    if args.appendix:
        built.append(build_appendix())
    if args.weekly:
        built.append(build_weekly())
    if args.monthly:
        built.append(build_monthly())

    for path in built:
        print(f"Built: {path}")


if __name__ == "__main__":
    main()
