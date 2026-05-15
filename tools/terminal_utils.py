import re

with open('tools/terminal_app.py', 'r') as f:
    content = f.read()

ui_module = """from __future__ import annotations

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
"""

with open('tools/terminal/ui.py', 'w') as f:
    f.write(ui_module)

content = re.sub(r'def fmt_money\(v: float \| None\) -> str:\n(?:[ \t]+.*?\n|\n)*(?=def [a-zA-Z_0-9]+\()', '', content, count=1)
content = re.sub(r'def fmt_pct\(v: float \| None\) -> str:\n(?:[ \t]+.*?\n|\n)*(?=def [a-zA-Z_0-9]+\()', '', content, count=1)
content = re.sub(r'def fmt_money_ccy\(v: float \| None, ccy: str\) -> str:\n(?:[ \t]+.*?\n|\n)*(?=def [a-zA-Z_0-9]+\()', '', content, count=1)

import_statement = "from tools.terminal.ui import fmt_money, fmt_pct, fmt_money_ccy\n"
if 'import sys' in content and import_statement not in content:
    content = content.replace('import sys\n', 'import sys\n' + import_statement, 1)

with open('tools/terminal_app.py', 'w') as f:
    f.write(content)
