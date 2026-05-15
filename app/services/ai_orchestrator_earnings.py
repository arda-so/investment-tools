from __future__ import annotations

from pathlib import Path
import re
import subprocess

from app.core.config import ROOT


def latest_earnings_source() -> tuple[Path | None, str]:
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return None, ""
    candidates: list[Path] = []
    for pattern in ("earnings_radar_*.md", "terminal_appendix_*.md"):
        candidates.extend(reports_dir.glob(pattern))
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        return None, ""
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    path = candidates[0]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return path, ""
    return path, text


def extract_earnings_rows_from_text(text: str, row_limit: int = 24) -> list[dict[str, str]]:
    lines = [str(line or "").rstrip() for line in str(text or "").splitlines()]
    if not lines:
        return []

    rows: list[dict[str, str]] = []
    in_section = False
    current_date = ""

    def _push(date: str, time_label: str, symbol: str, company: str) -> None:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return
        rows.append(
            {
                "date": str(date or "").strip(),
                "time": str(time_label or "").strip() or "-",
                "symbol": ticker,
                "company": str(company or "").strip() or "-",
            }
        )

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^##\s+", line):
            if re.search(r"earnings\s+calendar", line, flags=re.I):
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue

        match_date = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if match_date:
            current_date = match_date.group(1)
            continue

        if line.startswith("|") and line.count("|") >= 4 and not re.search(r"^\|\s*-{2,}", line):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 4:
                date_cell = cells[0]
                time_cell = cells[1]
                symbol_cell = cells[2]
                company_cell = cells[3]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", date_cell):
                    current_date = date_cell
                _push(current_date or date_cell, time_cell, symbol_cell, company_cell)
                if len(rows) >= row_limit:
                    break
                continue

        compact = re.match(r"^(Pre|Post|Day|BMO|AMC)\s+([A-Za-z0-9.\-]+)\s*[•\-]\s*(.+)$", line, flags=re.I)
        if compact:
            _push(current_date, compact.group(1).upper(), compact.group(2), compact.group(3))
            if len(rows) >= row_limit:
                break
            continue

        simple = re.match(r"^([A-Za-z0-9.\-]{1,10})\s*[•\-]\s*(.+)$", line)
        if simple:
            _push(current_date, "-", simple.group(1), simple.group(2))
            if len(rows) >= row_limit:
                break
            continue

    if rows:
        return rows[:row_limit]

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match_date = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if match_date:
            current_date = match_date.group(1)
        compact = re.match(r"^(Pre|Post|Day|BMO|AMC)?\s*([A-Za-z0-9.\-]{1,10})\s*[•\-]\s*(.+)$", line, flags=re.I)
        if compact:
            _push(current_date, (compact.group(1) or "-").upper(), compact.group(2), compact.group(3))
            if len(rows) >= row_limit:
                break
    return rows[:row_limit]


def read_earnings_calendar_rows(limit: int = 24) -> tuple[list[dict[str, str]], str]:
    path, text = latest_earnings_source()
    if not path or not text:
        return [], "-"
    rows = extract_earnings_rows_from_text(text, row_limit=max(1, int(limit)))
    return rows, path.name


def format_earnings_lines(rows: list[dict[str, str]], source: str, limit: int = 16) -> list[str]:
    lines = ["Earnings Calendar (This Week):"]
    shown = 0
    last_date = ""
    for row in rows:
        date = str(row.get("date") or "").strip() or "-"
        time_label = str(row.get("time") or "").strip() or "-"
        ticker = str(row.get("symbol") or "").strip().upper()
        company = str(row.get("company") or "").strip()
        if date != last_date:
            lines.append(f"\n{date}")
            last_date = date
        lines.append(f"- {time_label} {ticker}" + (f" — {company}" if company else ""))
        shown += 1
        if shown >= limit:
            break
    if len(rows) > shown:
        lines.append(f"\n...and {len(rows) - shown} more.")
    lines.append(f"\nSource: {source}")
    lines.append("If you want, I can open Dashboard too.")
    return lines


def run_earnings_refresh(timeout_sec: int = 120) -> tuple[bool, str]:
    script = ROOT / "bin" / "run_earnings"
    if not script.exists():
        return False, "missing_run_earnings_script"
    try:
        proc = subprocess.run(
            [str(script)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(20, int(timeout_sec)),
            check=False,
        )
        ok = int(proc.returncode or 1) == 0
        if ok:
            return True, "ok"
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (err[-1][:180] if err else f"exit_{proc.returncode}")
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)[:160]
