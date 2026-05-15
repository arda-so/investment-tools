from __future__ import annotations

import datetime as dt
import glob
import os
import re
from pathlib import Path


def latest(root: Path, pattern: str) -> str:
    files = sorted(glob.glob(str(root / pattern)), key=os.path.getmtime, reverse=True)
    return files[0] if files else ""


def latest_any(root: Path, patterns: list[str]) -> str:
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(str(root / pattern)))
    if not found:
        return ""
    return sorted(set(found), key=os.path.getmtime, reverse=True)[0]


def get_latest_file(root: Path, pattern: str) -> str:
    try:
        files = glob.glob(str(root / pattern))
        if not files:
            return ""
        return sorted(files, key=os.path.getmtime, reverse=True)[0]
    except Exception:
        return ""


def mtime(path: str) -> str:
    if not path:
        return "MISSING"
    ts = dt.datetime.fromtimestamp(os.path.getmtime(path))
    return ts.strftime("%Y-%m-%d %H:%M")


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def to_float(raw: str) -> float | None:
    try:
        value = (raw or "").strip().replace("$", "").replace("%", "").replace(",", "")
        if not value:
            return None
        return float(value)
    except Exception:
        return None


def read_watchlist_entries(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in read_lines(path):
        parts = [part.strip() for part in line.split(",")]
        if not parts or not parts[0]:
            continue
        ticker = parts[0].upper()
        added_at = parts[1] if len(parts) > 1 else ""
        added_price = parts[2] if len(parts) > 2 else ""
        entries.append({"ticker": ticker, "added_at": added_at, "added_price": added_price})
    return entries


def read_cash_balances(path: Path) -> list[dict[str, str | float]]:
    out: list[dict[str, str | float]] = []
    for line in read_lines(path):
        parts = [part.strip() for part in line.split(",", 2)]
        while len(parts) < 3:
            parts.append("")
        ccy = (parts[0] or "").upper()
        amount = to_float(parts[1])
        note = parts[2] or ""
        if not re.fullmatch(r"[A-Z]{3}", ccy):
            continue
        if amount is None:
            continue
        out.append({"currency": ccy, "amount": float(amount), "note": note})
    return out


def write_cash_balances(path: Path, rows: list[dict[str, str | float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for row in rows:
        ccy = str(row.get("currency") or "").upper().strip()
        amount = to_float(str(row.get("amount") or ""))
        note = str(row.get("note") or "").strip().replace("\n", " ")
        if not re.fullmatch(r"[A-Z]{3}", ccy):
            continue
        if amount is None:
            continue
        lines.append(f"{ccy},{amount:.6f},{note}")
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
