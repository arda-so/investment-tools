from __future__ import annotations

# --- UI Formatting Utilities ---
def fmt_money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"${v:,.2f}"

def fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:+.2f}%"

def fmt_money_ccy(v: float | None, ccy: str) -> str:
    if v is None:
        return "-"
    return f"{(ccy or 'USD').upper()} {v:,.2f}"
