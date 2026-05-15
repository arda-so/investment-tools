#!/usr/bin/env python3
import argparse
import datetime as dt
import glob
import html
import os
import re
import subprocess
from pathlib import Path


ROOT = Path("/Users/solmaz/Investment_Tools")
REPORTS = ROOT / "reports"
INPUTS = REPORTS / ".terminal_inputs"
DATA = ROOT / "data"


def latest(pattern: str) -> str:
    files = sorted(glob.glob(str(ROOT / pattern)), key=os.path.getmtime, reverse=True)
    return files[0] if files else ""


def mtime(path: str) -> str:
    if not path:
        return "MISSING"
    ts = dt.datetime.fromtimestamp(os.path.getmtime(path))
    return ts.strftime("%Y-%m-%d %H:%M")


def run_cmd(args: list[str]) -> list[str]:
    try:
        out = subprocess.check_output(args, text=True, cwd=str(ROOT), stderr=subprocess.DEVNULL)
    except Exception:
        return []
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def read_portfolio(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        while len(parts) < 4:
            parts.append("")
        rows.append(parts[:4])
    return rows


def linkify(path: str) -> str:
    if not path:
        return "<span class='missing'>MISSING</span>"
    p = Path(path)
    rel = str(p.relative_to(ROOT)) if p.exists() else path
    return f"<code>{html.escape(rel)}</code>"


def render_list(items: list[str], empty: str) -> str:
    if not items:
        return f"<li class='muted'>{html.escape(empty)}</li>"
    return "".join(f"<li>{html.escape(i)}</li>" for i in items)


def alert_ticker(line: str) -> str:
    line = line.strip().lstrip("-").strip()
    left = line.split("|", 1)[0].strip()
    m = re.search(r"\)\s*([A-Z][A-Z0-9.\-]+)\s*$", left)
    if m:
        return m.group(1)
    parts = left.split()
    return parts[-1] if parts else ""


def earnings_ticker(line: str) -> str:
    m = re.search(r"^(?:-)?\s*(?:UPCOMING|REPORTED)\s*\|\s*([A-Z][A-Z0-9.\-]+)\s*\|", line.strip())
    return m.group(1) if m else ""


def filter_personal(lines: list[str], tickers: set[str], kind: str) -> list[str]:
    out: list[str] = []
    for ln in lines:
        t = alert_ticker(ln) if kind == "alert" else earnings_ticker(ln)
        if t and t in tickers:
            out.append(ln)
    return out


def sev_counts(alerts: list[str]) -> tuple[int, int, int]:
    hi = sum(1 for a in alerts if "HIGH(" in a)
    med = sum(1 for a in alerts if "MED(" in a)
    low = sum(1 for a in alerts if "LOW(" in a)
    return hi, med, low


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Beta graphical dashboard HTML.")
    parser.add_argument("--out", default=str(REPORTS / "terminal_beta_dashboard.html"))
    args = parser.parse_args()

    daily = latest("reports/terminal_daily_brief_*.md")
    appendix = latest("reports/terminal_appendix_*.md")
    weekly = latest("reports/terminal_weekly_outlook_*.md")
    monthly = latest("reports/terminal_monthly_ic_memo_*.md")
    quarterly = latest("reports/quarterly_report_*.md")
    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    earnings = latest("reports/.terminal_inputs/earnings_radar_*.md")

    alerts = (
        run_cmd(
            [
                "python3",
                "tools/red_flag_rank.py",
                "--file",
                red_flag,
                "--limit",
                "7",
            ]
        )
        if red_flag
        else []
    )
    upcoming = (
        run_cmd(
            [
                "python3",
                "tools/earnings_watch_rank.py",
                "--file",
                earnings,
                "--limit",
                "7",
                "--scope",
                "upcoming",
            ]
        )
        if earnings
        else []
    )
    this_week = (
        run_cmd(
            [
                "python3",
                "tools/earnings_watch_rank.py",
                "--file",
                earnings,
                "--limit",
                "7",
                "--scope",
                "week",
            ]
        )
        if earnings
        else []
    )

    watchlist = read_lines(DATA / "my_watchlist.txt")
    portfolio = read_portfolio(DATA / "portfolio.csv")
    portfolio_tickers = [r[0].strip().upper() for r in portfolio if r and r[0].strip()]
    user_tickers = {t.upper() for t in watchlist + portfolio_tickers if t.strip()}

    personal_alerts = filter_personal(alerts, user_tickers, "alert")
    personal_upcoming = filter_personal(upcoming, user_tickers, "earnings")
    personal_week = filter_personal(this_week, user_tickers, "earnings")
    hi, med, low = sev_counts(alerts)

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html_out = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Investor Terminal Beta Dashboard</title>
  <style>
    :root {{
      --bg: #0b1014;
      --panel: #111920;
      --panel2: #182430;
      --text: #e6eef5;
      --muted: #98acbd;
      --line: #2a3c4b;
      --good: #62d38a;
      --warn: #f3b66a;
      --bad: #f07a86;
      --accent: #65c2ff;
      --gold: #d4b26c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Helvetica Neue", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(1100px 460px at -10% -10%, #25394b 0%, transparent 60%),
        radial-gradient(800px 420px at 95% 0%, #1d3142 0%, transparent 58%),
        var(--bg);
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    .top {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
    h1 {{ margin: 0; letter-spacing: 0.5px; }}
    .sub {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; margin-top: 16px; }}
    .card {{
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      min-height: 120px;
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
    }}
    .kpis {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }}
    .kpi {{
      background: rgba(9, 14, 18, 0.45);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
    }}
    .kpi .v {{ font-size: 22px; font-weight: 700; }}
    .kpi .l {{ color: var(--muted); font-size: 12px; }}
    .c4 {{ grid-column: span 4; }}
    .c6 {{ grid-column: span 6; }}
    .c8 {{ grid-column: span 8; }}
    .c12 {{ grid-column: span 12; }}
    h2 {{ margin: 0 0 8px; font-size: 16px; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 4px 0; }}
    .muted {{ color: var(--muted); }}
    .missing {{ color: var(--bad); font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .badge {{ padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); }}
    .ok {{ color: var(--good); }}
    .warn {{ color: var(--warn); }}
    .gold {{ color: var(--gold); }}
    .stack {{ display: grid; gap: 12px; }}
    .hint {{ font-size: 12px; color: var(--muted); }}
    @media (max-width: 920px) {{
      .c4, .c6, .c8, .c12 {{ grid-column: span 12; }}
      .kpis {{ grid-template-columns: repeat(2, 1fr); }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Investor Terminal <span class="badge gold">BETA v2</span></h1>
        <div class="sub">Generated {html.escape(generated)} | Folder: {html.escape(str(ROOT))}</div>
      </div>
      <div class="sub">Source mode is managed in terminal via <code>./bin/mode</code></div>
    </div>

    <div class="grid">
      <section class="card c12">
        <h2>At A Glance</h2>
        <div class="kpis">
          <div class="kpi"><div class="v">{len(alerts)}</div><div class="l">Ranked Alerts</div></div>
          <div class="kpi"><div class="v">{hi}</div><div class="l">High Severity</div></div>
          <div class="kpi"><div class="v">{med}</div><div class="l">Medium Severity</div></div>
          <div class="kpi"><div class="v">{low}</div><div class="l">Low Severity</div></div>
          <div class="kpi"><div class="v">{len(personal_alerts) + len(personal_upcoming) + len(personal_week)}</div><div class="l">Personal Triggers</div></div>
          <div class="kpi"><div class="v">{len(user_tickers)}</div><div class="l">Tracked Tickers</div></div>
        </div>
      </section>

      <section class="card c6">
        <h2>Status</h2>
        <ul>
          <li>Daily Brief: {mtime(daily)} | {linkify(daily)}</li>
          <li>Appendix: {mtime(appendix)} | {linkify(appendix)}</li>
          <li>Weekly Outlook: {mtime(weekly)} | {linkify(weekly)}</li>
          <li>Monthly IC Memo: {mtime(monthly)} | {linkify(monthly)}</li>
          <li>Quarterly Deep Dive: {mtime(quarterly)} | {linkify(quarterly)}</li>
        </ul>
      </section>

      <section class="card c6 stack">
        <div>
          <h2>Top 7 Alerts</h2>
          <div class="hint">Ranked by severity and red-flag term strength.</div>
          <ul>{render_list(alerts, "No current red-flag alerts.")}</ul>
        </div>
        <div>
          <h2>Your Priority Alerts</h2>
          <div class="hint">Only tickers from your watchlist and portfolio.</div>
          <ul>{render_list(personal_alerts[:7], "No personal alert lines detected.")}</ul>
        </div>
      </section>

      <section class="card c6">
        <h2>Top Earnings Watch (Upcoming)</h2>
        <ul>{render_list(upcoming, "No upcoming key earnings from current radar.")}</ul>
      </section>

      <section class="card c6">
        <h2>Key Earnings This Week</h2>
        <ul>{render_list(this_week, "No weekly key earnings in current radar.")}</ul>
      </section>

      <section class="card c6">
        <h2>Your Earnings Focus</h2>
        <div class="hint">Upcoming + this-week events for your own tickers.</div>
        <ul>{render_list((personal_upcoming + personal_week)[:10], "No personal earnings events found in current radar.")}</ul>
      </section>

      <section class="card c4">
        <h2>Your Watchlist</h2>
        <ul>{render_list(watchlist[:30], "No watchlist yet. Add tickers in data/my_watchlist.txt")}</ul>
      </section>

      <section class="card c8">
        <h2>Your Portfolio</h2>
        {"<table><thead><tr><th>Ticker</th><th>Shares</th><th>Cost Basis</th><th>Notes</th></tr></thead><tbody>" if portfolio else ""}
        {"".join(f"<tr><td>{html.escape(r[0])}</td><td>{html.escape(r[1])}</td><td>{html.escape(r[2])}</td><td>{html.escape(r[3])}</td></tr>" for r in portfolio[:30]) if portfolio else "<div class='muted'>No positions yet. Add rows to data/portfolio.csv as: TICKER,SHARES,COST_BASIS,NOTES</div>"}
        {"</tbody></table>" if portfolio else ""}
      </section>

      <section class="card c12">
        <h2>Workflow</h2>
        <ul>
          <li>Refresh data first: <code>./bin/run_terminal_daily</code></li>
          <li>Build dashboard only: <code>python3 tools/build_beta_dashboard.py</code></li>
          <li>Run and open dashboard: <code>./bin/run_terminal_beta</code></li>
          <li>Manage personal lists in terminal: Advanced menu -> watchlist/portfolio options</li>
        </ul>
      </section>
    </div>
  </div>
</body>
</html>
"""

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")
    print(str(out))


if __name__ == "__main__":
    main()
