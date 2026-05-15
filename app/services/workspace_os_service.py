"""workspace_os_service.py — Investment OS Unified Work OS backend.

Single source of truth: Day view and Ticker timeline union ALL existing tables
(investor_annotations_core, action_proposals_core, investment_records_core)
so there is no split brain.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)


def _iter_cursor_rows(cur: Any, batch_size: int = 256):
    size = max(1, int(batch_size or 256))
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            break
        for row in rows:
            yield row


_DDL = """
CREATE TABLE IF NOT EXISTS investment_records_core (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'action',
    domain        TEXT NOT NULL DEFAULT 'work',
    ticker        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    sentiment     TEXT NOT NULL DEFAULT 'neutral',
    status        TEXT NOT NULL DEFAULT 'open',
    priority      TEXT NOT NULL DEFAULT 'normal',
    due_date      DATE,
    pinned        BOOLEAN NOT NULL DEFAULT FALSE,
    source        TEXT NOT NULL DEFAULT 'manual',
    created_by    TEXT NOT NULL DEFAULT 'user',
    approved_by   TEXT NOT NULL DEFAULT '',
    approved_at   TIMESTAMPTZ,
    source_ref_id TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    change_log    JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_irec_status ON investment_records_core(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_irec_ticker ON investment_records_core(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_irec_kind   ON investment_records_core(kind, status);
CREATE INDEX IF NOT EXISTS idx_irec_due    ON investment_records_core(due_date) WHERE due_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_irec_domain ON investment_records_core(domain, status);
CREATE INDEX IF NOT EXISTS idx_irec_agent  ON investment_records_core(created_by, status, created_at DESC);
CREATE TABLE IF NOT EXISTS workspace_projects_core (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    emoji       TEXT NOT NULL DEFAULT '🎯',
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    color       TEXT NOT NULL DEFAULT '#6366f1',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wsproj_status ON workspace_projects_core(status, created_at DESC);
CREATE TABLE IF NOT EXISTS workspace_links_core (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'General',
    description TEXT NOT NULL DEFAULT '',
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wslinks_cat    ON workspace_links_core(category, sort_order);
CREATE INDEX IF NOT EXISTS idx_wslinks_status ON workspace_links_core(status, sort_order);
"""

# Pre-seeded research hub links — (title, url, category, sort_order)
_HUB_SEED: list[tuple[str, str, str, int]] = [
    # ── Screeners & Markets ────────────────────────────────────────────────────
    ("TIKR Screener",           "https://app.tikr.com/screener?sid=1&ref=cq3co",                                 "📊 Screeners & Markets", 1),
    ("Stock Analysis News",     "https://stockanalysis.com/news/",                                               "📊 Screeners & Markets", 2),
    ("Finviz Screener",         "https://finviz.com/screener.ashx?v=111",                                        "📊 Screeners & Markets", 3),
    ("Companies Market Cap",    "https://companiesmarketcap.com/all-countries/",                                 "📊 Screeners & Markets", 4),
    ("US Market Cap Ranking",   "https://companiesmarketcap.com/usa/largest-companies-in-the-usa-by-market-cap/","📊 Screeners & Markets", 5),
    ("TradingView Movers",      "https://www.tradingview.com/markets/stocks-usa/market-movers-losers/",          "📊 Screeners & Markets", 6),
    ("TradingView Futures",     "https://www.tradingview.com/markets/#futures",                                  "📊 Screeners & Markets", 7),
    ("CNBC S&P 500",            "https://www.cnbc.com/quotes/.SPX",                                              "📊 Screeners & Markets", 8),
    ("CNBC Nasdaq",             "https://www.cnbc.com/quotes/.IXIC",                                             "📊 Screeners & Markets", 9),
    ("MarketWatch",             "https://www.marketwatch.com/",                                                  "📊 Screeners & Markets", 10),
    # ── Central Banks & Liquidity ─────────────────────────────────────────────
    ("FRED Fed Funds Rate",     "https://fred.stlouisfed.org/series/FEDFUNDS",                                   "🏦 Central Banks & Liquidity", 1),
    ("FRED M2 Money Supply",    "https://fred.stlouisfed.org/series/M2SL",                                       "🏦 Central Banks & Liquidity", 2),
    ("FRED Fed Balance Sheet",  "https://fred.stlouisfed.org/series/WALCL",                                      "🏦 Central Banks & Liquidity", 3),
    ("FRED Reverse Repo",       "https://fred.stlouisfed.org/series/RRPONTSYD",                                  "🏦 Central Banks & Liquidity", 4),
    ("FRED Reserve Balances",   "https://fred.stlouisfed.org/series/RESBALNS",                                   "🏦 Central Banks & Liquidity", 5),
    ("FRED 10Y-2Y Spread",      "https://fred.stlouisfed.org/series/T10Y2Y",                                     "🏦 Central Banks & Liquidity", 6),
    ("FRED US Federal Debt",    "https://fred.stlouisfed.org/series/GFDEBTN",                                    "🏦 Central Banks & Liquidity", 7),
    ("Treasury Yields",         "https://www.treasury.gov/resource-center/data-chart-center/interest-rates/pages/TextView.aspx?data=yield", "🏦 Central Banks & Liquidity", 8),
    ("ECB Balance Sheet",       "https://data.ecb.europa.eu/data/datasets/BSI/BSI.M.U2.Y.V.M20.X.1.U2.2300.Z01.E", "🏦 Central Banks & Liquidity", 9),
    ("ECB Liquidity",           "https://data.ecb.europa.eu/data/datasets/ILM/ILM.W.U2.C.T000000.Z5.Z01",      "🏦 Central Banks & Liquidity", 10),
    ("PBC China",               "http://www.pbc.gov.cn/en/3688247/3688975/5242368/5242424/index.html",          "🏦 Central Banks & Liquidity", 11),
    ("ICI Money Market Funds",  "https://www.ici.org/research/stats/mmf",                                       "🏦 Central Banks & Liquidity", 12),
    # ── Macro & Economy ───────────────────────────────────────────────────────
    ("TradingEconomics Calendar","https://tradingeconomics.com/calendar",                                        "📈 Macro & Economy", 1),
    ("TradingEconomics Turkey", "https://tradingeconomics.com/turkey/calendar",                                  "📈 Macro & Economy", 2),
    ("TradingEconomics Commodities","https://tradingeconomics.com/commodities",                                  "📈 Macro & Economy", 3),
    ("BLS",                     "https://www.bls.gov/",                                                          "📈 Macro & Economy", 4),
    ("EIA Petroleum Weekly",    "https://www.eia.gov/petroleum/supply/weekly/",                                  "📈 Macro & Economy", 5),
    ("Oil Price",               "https://oilprice.com/",                                                         "📈 Macro & Economy", 6),
    ("Macrotrends Housing",     "https://www.macrotrends.net/1314/housing-starts-historical-chart",              "📈 Macro & Economy", 7),
    ("Census Construction",     "https://www.census.gov/construction/nrc/index.html",                           "📈 Macro & Economy", 8),
    ("FRED Industrial Production","https://fred.stlouisfed.org/series/INDPRO",                                   "📈 Macro & Economy", 9),
    ("FRED JOLTS",              "https://fred.stlouisfed.org/series/JTSJOL",                                     "📈 Macro & Economy", 10),
    ("FRED Initial Claims",     "https://fred.stlouisfed.org/series/ICSA",                                       "📈 Macro & Economy", 11),
    ("FRED Retail Sales",       "https://fred.stlouisfed.org/series/RSXFS",                                      "📈 Macro & Economy", 12),
    ("FRED Consumer Spending",  "https://fred.stlouisfed.org/series/PCEC96",                                     "📈 Macro & Economy", 13),
    ("FRED Payrolls",           "https://fred.stlouisfed.org/series/PAYEMS",                                     "📈 Macro & Economy", 14),
    ("FRED Corp After-tax Profits","https://fred.stlouisfed.org/series/CPATAX",                                  "📈 Macro & Economy", 15),
    ("FRED Leading Index",      "https://fred.stlouisfed.org/series/USSLIND",                                    "📈 Macro & Economy", 16),
    ("FRED Nonfinancial Debt",  "https://fred.stlouisfed.org/series/BCNSDODNS",                                  "📈 Macro & Economy", 17),
    # ── Inflation & Labor ─────────────────────────────────────────────────────
    ("FRED CPI",                "https://fred.stlouisfed.org/series/CPIAUCSL",                                   "💹 Inflation & Labor", 1),
    ("FRED PCE",                "https://fred.stlouisfed.org/series/PCEPI",                                      "💹 Inflation & Labor", 2),
    ("FRED Wage Growth",        "https://fred.stlouisfed.org/series/AHETPI",                                     "💹 Inflation & Labor", 3),
    ("FRED Unit Labor Costs",   "https://fred.stlouisfed.org/series/ULCNFB",                                     "💹 Inflation & Labor", 4),
    ("FRED Unemployment Rate",  "https://fred.stlouisfed.org/series/UNRATE",                                     "💹 Inflation & Labor", 5),
    # ── Real Estate ───────────────────────────────────────────────────────────
    ("FRED Housing Starts",     "https://fred.stlouisfed.org/series/HOUST",                                      "🏠 Real Estate", 1),
    ("FRED Existing Home Sales","https://fred.stlouisfed.org/series/EXHOSLUSM495S",                              "🏠 Real Estate", 2),
    ("FRED Case-Shiller Index", "https://fred.stlouisfed.org/series/CSUSHPINSA",                                 "🏠 Real Estate", 3),
    ("FRED Mortgage 30Y",       "https://fred.stlouisfed.org/series/MORTGAGE30US",                               "🏠 Real Estate", 4),
    ("TE Mortgage Rate",        "https://tradingeconomics.com/united-states/30-year-mortgage-rate",              "🏠 Real Estate", 5),
    ("Redfin Luxury Market",    "https://www.redfin.com/news/luxury-market/",                                    "🏠 Real Estate", 6),
    # ── Research & Value ──────────────────────────────────────────────────────
    ("EDGAR Search",            "https://www.sec.gov/edgar/search/",                                             "🔍 Research & Value", 1),
    ("Investor.gov EDGAR",      "https://www.investor.gov/introduction-investing/getting-started/researching-investments/using-edgar-research-investments", "🔍 Research & Value", 2),
    ("Annual Reports Archive",  "https://archive.org/details/annual-reports-archive",                           "🔍 Research & Value", 3),
    ("Dataroma",                "https://www.dataroma.com/m/home.php",                                           "🔍 Research & Value", 4),
    ("Value Investors Club",    "https://valueinvestorsclub.com/topics",                                         "🔍 Research & Value", 5),
    ("Yardeni Research",        "https://yardeni.com/",                                                          "🔍 Research & Value", 6),
    ("S&P 500 PE Ratio",        "https://www.multpl.com/s-p-500-pe-ratio",                                       "🔍 Research & Value", 7),
    ("IFI Patent Rankings",     "https://www.ificlaims.com/rankings-top-50-2020.htm",                           "🔍 Research & Value", 8),
    ("USA Facts Economy",       "https://usafacts.org/economy/",                                                 "🔍 Research & Value", 9),
    # ── News & Media ──────────────────────────────────────────────────────────
    ("Bloomberg Europe",        "https://www.bloomberg.com/europe",                                              "📰 News & Media", 1),
    ("WSJ",                     "https://www.wsj.com/",                                                          "📰 News & Media", 2),
    ("Financial Times",         "https://www.ft.com/",                                                           "📰 News & Media", 3),
    ("The Economist",           "https://www.economist.com/",                                                    "📰 News & Media", 4),
    ("Barron's",                "https://www.barrons.com/",                                                      "📰 News & Media", 5),
    ("New York Times",          "https://www.nytimes.com/",                                                      "📰 News & Media", 6),
    ("MktNews",                 "https://mktnews.com/",                                                          "📰 News & Media", 7),
    ("Cosmetics Business",      "https://cosmeticsbusiness.com/",                                                "📰 News & Media", 8),
    # ── Asia / EM ─────────────────────────────────────────────────────────────
    ("South China Morning Post","https://www.scmp.com/",                                                         "🌏 Asia / EM", 1),
    ("Global Times",            "https://www.globaltimes.cn/",                                                   "🌏 Asia / EM", 2),
    ("Caixin Global",           "https://www.caixinglobal.com/",                                                 "🌏 Asia / EM", 3),
    ("China Daily",             "https://www.chinadaily.com.cn/",                                                "🌏 Asia / EM", 4),
    ("Japan Times",             "https://www.japantimes.co.jp/",                                                 "🌏 Asia / EM", 5),
    ("Yicai Global",            "https://www.yicaiglobal.com/",                                                  "🌏 Asia / EM", 6),
    ("Times of Israel",         "https://www.timesofisrael.com/",                                                "🌏 Asia / EM", 7),
    # ── Company IR ────────────────────────────────────────────────────────────
    ("TSMC IR",                 "https://investor.tsmc.com/",                                                    "🏢 Company IR", 1),
    ("ASML IR",                 "https://www.asml.com/en/investors",                                             "🏢 Company IR", 2),
    ("Samsung IR",              "https://www.samsung.com/global/ir/",                                            "🏢 Company IR", 3),
    ("Intel IR",                "https://www.intc.com/",                                                         "🏢 Company IR", 4),
    ("NVIDIA IR",               "https://investor.nvidia.com/",                                                  "🏢 Company IR", 5),
    ("Apple IR",                "https://www.apple.com/investor/",                                               "🏢 Company IR", 6),
    ("Amazon IR",               "https://ir.aboutamazon.com/",                                                   "🏢 Company IR", 7),
    ("SLB IR",                  "https://investorcenter.slb.com/",                                               "🏢 Company IR", 8),
    ("Chevron IR",              "https://investor.chevron.com/",                                                  "🏢 Company IR", 9),
    ("ExxonMobil IR",           "https://investor.exxonmobil.com/",                                              "🏢 Company IR", 10),
    ("Rio Tinto IR",            "https://www.riotinto.com/invest",                                               "🏢 Company IR", 11),
    ("BHP IR",                  "https://www.bhp.com/investors",                                                 "🏢 Company IR", 12),
    ("ADM IR",                  "https://investors.adm.com/",                                                    "🏢 Company IR", 13),
    ("Vulcan Materials IR",     "https://ir.vulcanmaterials.com/",                                               "🏢 Company IR", 14),
    ("Martin Marietta IR",      "https://ir.martinmarietta.com/",                                                "🏢 Company IR", 15),
    ("Deere IR",                "https://investor.deere.com/",                                                   "🏢 Company IR", 16),
    ("Boeing IR",               "https://www.boeing.com/investors/",                                             "🏢 Company IR", 17),
    ("Union Pacific IR",        "https://www.up.com/investor/",                                                  "🏢 Company IR", 18),
    ("CSX IR",                  "https://investors.csx.com/",                                                    "🏢 Company IR", 19),
    ("FedEx IR",                "https://www.fedex.com/",                                                        "🏢 Company IR", 20),
    ("Maersk IR",               "https://www.maersk.com/",                                                       "🏢 Company IR", 21),
    ("Home Depot IR",           "https://ir.homedepot.com/",                                                     "🏢 Company IR", 22),
    ("Lowe's IR",               "https://investor.lowes.com/",                                                   "🏢 Company IR", 23),
    ("Walmart IR",              "https://www.walmart.com/",                                                      "🏢 Company IR", 24),
    ("Target IR",               "https://www.target.com/",                                                       "🏢 Company IR", 25),
    ("Nike IR",                 "https://www.nike.com/investors",                                                 "🏢 Company IR", 26),
    ("P&G IR",                  "https://www.pg.com/investors/",                                                  "🏢 Company IR", 27),
    ("Lennar IR",               "https://investors.lennar.com/",                                                  "🏢 Company IR", 28),
    ("D.R. Horton IR",          "https://investor.drhorton.com/",                                                 "🏢 Company IR", 29),
    ("JPMorgan IR",             "https://www.jpmorganchase.com/",                                                 "🏢 Company IR", 30),
    ("Bank of America IR",      "https://www.bankofamerica.com/",                                                 "🏢 Company IR", 31),
    ("Citigroup IR",            "https://www.citigroup.com/",                                                     "🏢 Company IR", 32),
    ("Morgan Stanley IR",       "https://www.morganstanley.com/",                                                 "🏢 Company IR", 33),
    ("BlackRock IR",            "https://www.blackrock.com/",                                                     "🏢 Company IR", 34),
    ("Visa IR",                 "https://investor.visa.com/",                                                     "🏢 Company IR", 35),
    ("Mastercard IR",           "https://investor.mastercard.com/",                                               "🏢 Company IR", 36),
    ("American Express IR",     "https://ir.americanexpress.com/",                                                "🏢 Company IR", 37),
    ("Marriott IR",             "https://www.marriott.com/investor-relations.mi",                                 "🏢 Company IR", 38),
]

# Module-level AI insight cache: {ticker: (text, expires_ts)}
_AI_INSIGHT_CACHE: dict[str, tuple[str, float]] = {}
_AI_CACHE_TTL = 900  # 15 minutes


def ensure_workspace_os_schema() -> None:
    if not pg_enabled():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        for stmt in _DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)
        # Additive column migrations — safe to run repeatedly
        _migrations = [
            "ALTER TABLE investment_records_core ADD COLUMN IF NOT EXISTS url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE investment_records_core ADD COLUMN IF NOT EXISTS emoji TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE investment_records_core ADD COLUMN IF NOT EXISTS project_space TEXT NOT NULL DEFAULT 'general'",
            "ALTER TABLE investment_records_core ADD COLUMN IF NOT EXISTS project_id BIGINT",
        ]
        for migration in _migrations:
            try:
                cur.execute(migration)
            except Exception:
                pass
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_irec_space ON investment_records_core(project_space, status, created_at DESC)")
        except Exception:
            pass
        # One-time cleanup: remove auto-generated [active_proposal] task records
        try:
            cur.execute(
                "DELETE FROM investment_records_core WHERE title LIKE '%[active_proposal]%'"
            )
        except Exception:
            pass
        con.commit()
        # Seed hub links once if table is empty
        try:
            cur.execute("SELECT COUNT(*) FROM workspace_links_core WHERE status='active'")
            row = cur.fetchone()
            if row and int(row[0]) == 0:
                for (title, url, category, sort_order) in _HUB_SEED:
                    cur.execute(
                        "INSERT INTO workspace_links_core(title,url,category,sort_order) VALUES(%s,%s,%s,%s)",
                        (title, url, category, sort_order),
                    )
                con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception as rollback_exc:
                LOGGER.warning("workspace_os schema seed rollback failed: %s", str(rollback_exc), exc_info=True)
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _row_to_dict(cur_description, row) -> dict:
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


# ── Priority normalisation ────────────────────────────────────────────────────
def _norm_priority(p: str) -> str:
    p = str(p or "").strip().upper()
    if p in ("P1", "HIGH"):
        return "high"
    if p in ("P3", "LOW"):
        return "low"
    return "normal"


# ── Unified record shape ──────────────────────────────────────────────────────
def _todo_to_unified(r: dict) -> dict:
    dd = str(r.get("due_date") or "").strip()
    return {
        "_source": "todo",
        "id": int(r.get("id") or 0),
        "kind": "action",
        "domain": "work",
        "ticker": str(r.get("ticker") or "").upper(),
        "title": str(r.get("task") or r.get("title") or ""),
        "body": "",
        "priority": _norm_priority(str(r.get("priority") or "P2")),
        "status": "open",
        "pinned": False,
        "created_by": "user",
        "created_at": str(r.get("created_at") or ""),
        "due_date": dd or None,
    }


def _proposal_to_unified(r: dict) -> dict:
    return {
        "_source": "proposal",
        "id": int(r.get("id") or 0),
        "kind": "decision",
        "domain": "work",
        "ticker": str(r.get("ticker") or "").upper(),
        "title": str(r.get("title") or ""),
        "body": str(r.get("thesis_summary") or "")[:300],
        "priority": "high",
        "status": "open",
        "pinned": False,
        "created_by": "agent",
        "created_at": str(r.get("created_at") or ""),
        "due_date": None,
    }


def _record_to_unified(r: dict) -> dict:
    return {
        "_source": "record",
        "id": int(r.get("id") or 0),
        "kind": str(r.get("kind") or "action"),
        "domain": str(r.get("domain") or "work"),
        "ticker": str(r.get("ticker") or "").upper(),
        "title": str(r.get("title") or ""),
        "body": str(r.get("body") or ""),
        "priority": str(r.get("priority") or "normal"),
        "status": str(r.get("status") or "open"),
        "pinned": bool(r.get("pinned")),
        "created_by": str(r.get("created_by") or "user"),
        "created_at": str(r.get("created_at") or ""),
        "due_date": r.get("due_date"),
    }


# ── Unified action helpers ────────────────────────────────────────────────────
def action_done(source: str, rec_id: int) -> bool:
    """Mark an item done regardless of which table it lives in."""
    if not pg_enabled():
        return False
    if source == "record":
        return update_record_status(rec_id, "done")
    if source == "todo":
        try:
            from app.services.postgres_core_service import toggle_todo_pg
            return toggle_todo_pg(rec_id)
        except Exception:
            return False
    if source == "proposal":
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE action_proposals_core SET status='executed', updated_at=%s WHERE id=%s",
                (dt.datetime.now().isoformat(), rec_id),
            )
            con.commit()
            return True
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return False
        finally:
            con.close()
    return False


def action_dismiss(source: str, rec_id: int) -> bool:
    """Dismiss/reject an item."""
    if not pg_enabled():
        return False
    if source == "record":
        return update_record_status(rec_id, "rejected")
    if source == "todo":
        # Dismissing a todo = archive it
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE investor_annotations_core SET status='archived', updated_at=%s WHERE id=%s AND annotation_type='task'",
                (dt.datetime.now().isoformat(), rec_id),
            )
            con.commit()
            return True
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return False
        finally:
            con.close()
    if source == "proposal":
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE action_proposals_core SET status='rejected', updated_at=%s WHERE id=%s",
                (dt.datetime.now().isoformat(), rec_id),
            )
            con.commit()
            return True
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return False
        finally:
            con.close()
    return False


def action_delete(source: str, rec_id: int) -> bool:
    if source == "record":
        return delete_record(rec_id)
    if source == "todo":
        try:
            from app.services.postgres_core_service import delete_todo_pg
            return delete_todo_pg(rec_id)
        except Exception:
            return False
    if source == "proposal":
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cur.execute("DELETE FROM action_proposals_core WHERE id=%s", (rec_id,))
            con.commit()
            return True
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return False
        finally:
            con.close()
    return False


# ── Day view ─────────────────────────────────────────────────────────────────
def get_day_view() -> dict:
    """Return {focus, agent_flagged, backlog} unified from ALL tables."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return {"focus": [], "agent_flagged": [], "backlog": []}
    con = pg_connect()
    if con is None:
        return {"focus": [], "agent_flagged": [], "backlog": []}

    today = dt.date.today().isoformat()
    yesterday_ts = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat()

    try:
        cur = con.cursor()

        # ── Todos: open tasks from investor_annotations_core ──────────────────
        todos: list[dict] = []
        try:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='investor_annotations_core'
                """
            )
            ann_cols = {str(r[0] or "").strip().lower() for r in _iter_cursor_rows(cur)}
            text_expr = "content" if "content" in ann_cols else ("content_text" if "content_text" in ann_cols else "''")
            cur.execute(
                f"""SELECT id,
                          COALESCE({text_expr}, '') AS task,
                          status,
                          COALESCE(priority,'P2') AS priority,
                          COALESCE(due_date,'') AS due_date,
                          COALESCE(entity_id,'') AS ticker,
                          created_at
                   FROM investor_annotations_core
                   WHERE annotation_type='task'
                     AND status IN ('open','snoozed')
                   ORDER BY CASE COALESCE(priority,'P2')
                              WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END,
                            created_at DESC
                   LIMIT 200"""
            )
            for r in _iter_cursor_rows(cur):
                todos.append({
                    "_source": "todo",
                    "id": int(r[0] or 0),
                    "kind": "action",
                    "domain": "work",
                    "ticker": str(r[5] or "").upper(),
                    "title": str(r[1] or ""),
                    "body": "",
                    "priority": _norm_priority(str(r[3] or "P2")),
                    "status": "open",
                    "pinned": False,
                    "created_by": "user",
                    "created_at": str(r[6] or ""),
                    "due_date": str(r[4] or "") or None,
                })
        except Exception:
            pass

        # ── Agent proposals: open from action_proposals_core (last 7 days) ───
        proposals: list[dict] = []
        try:
            cur.execute(
                """SELECT DISTINCT ON (ticker) id, ticker, title, thesis_summary, created_at
                   FROM action_proposals_core
                   WHERE status='open'
                     AND created_at::timestamptz >= NOW() - INTERVAL '7 days'
                   ORDER BY ticker, created_at DESC
                   LIMIT 50"""
            )
            for r in _iter_cursor_rows(cur):
                proposals.append({
                    "_source": "proposal",
                    "id": int(r[0] or 0),
                    "kind": "decision",
                    "domain": "work",
                    "ticker": str(r[1] or "").upper(),
                    "title": str(r[2] or ""),
                    "body": str(r[3] or "")[:300],
                    "priority": "high",
                    "status": "open",
                    "pinned": False,
                    "created_by": "agent",
                    "created_at": str(r[4] or ""),
                    "due_date": None,
                })
        except Exception:
            pass

        # ── investment_records_core: new unified records ───────────────────────
        records: list[dict] = []
        try:
            cur.execute(
                """SELECT id, kind, domain, ticker, title, body, priority,
                          status, pinned, created_by, created_at, due_date
                   FROM investment_records_core
                   WHERE status='open'
                   ORDER BY created_at DESC
                   LIMIT 200"""
            )
            for r in _iter_cursor_rows(cur):
                dd = r[11]
                created = r[10]
                records.append({
                    "_source": "record",
                    "id": int(r[0] or 0),
                    "kind": str(r[1] or "action"),
                    "domain": str(r[2] or "work"),
                    "ticker": str(r[3] or "").upper(),
                    "title": str(r[4] or ""),
                    "body": str(r[5] or ""),
                    "priority": str(r[6] or "normal"),
                    "status": str(r[7] or "open"),
                    "pinned": bool(r[8]),
                    "created_by": str(r[9] or "user"),
                    "created_at": created.isoformat() if isinstance(created, (dt.date, dt.datetime)) else str(created or ""),
                    "due_date": None,  # filtered in sections below
                })
            # Re-read due_date properly
            cur.execute(
                """SELECT id, due_date FROM investment_records_core
                   WHERE status='open' AND due_date IS NOT NULL"""
            )
            due_map = {int(r[0]): (r[1].isoformat() if r[1] else None) for r in _iter_cursor_rows(cur)}
            for rec in records:
                rec["due_date"] = due_map.get(rec["id"])
        except Exception:
            pass

        # ── Partition into sections ───────────────────────────────────────────
        focus: list[dict] = []
        agent_flagged: list[dict] = []
        backlog: list[dict] = []

        # Focus: todos due today + P1 todos + pinned records
        for t in todos:
            dd = str(t.get("due_date") or "").strip()
            if dd == today or t["priority"] == "high":
                focus.append(t)

        # Focus: pinned records, agent records due today
        for rec in records:
            if rec["pinned"] or str(rec.get("due_date") or "") == today:
                focus.append(rec)

        # Agent flagged: proposals (last 7 days) + agent-created records (last 24h)
        # Dedup: only keep the most recent item per ticker+title
        _seen_agent = set()
        for p in proposals:
            key = (p.get("ticker",""), p.get("title",""))
            if key not in _seen_agent:
                _seen_agent.add(key)
                agent_flagged.append(p)
        for rec in records:
            if rec["created_by"] == "agent" and str(rec.get("created_at") or "") >= yesterday_ts:
                key = (rec.get("ticker",""), rec.get("title",""))
                if key not in _seen_agent:
                    _seen_agent.add(key)
                    agent_flagged.append(rec)

        # Backlog: all other open todos + records (not in focus or agent_flagged)
        focus_ids = {(r["_source"], r["id"]) for r in focus}
        flagged_ids = {(r["_source"], r["id"]) for r in agent_flagged}

        for t in todos:
            key = (t["_source"], t["id"])
            if key not in focus_ids and key not in flagged_ids:
                backlog.append(t)

        for rec in records:
            key = (rec["_source"], rec["id"])
            if key not in focus_ids and key not in flagged_ids and rec["created_by"] != "agent":
                backlog.append(rec)

        # Sort focus by priority
        _pri_order = {"high": 0, "normal": 1, "low": 2}
        focus.sort(key=lambda r: _pri_order.get(r.get("priority", "normal"), 1))
        backlog.sort(key=lambda r: _pri_order.get(r.get("priority", "normal"), 1))

        return {"focus": focus[:60], "agent_flagged": agent_flagged[:30], "backlog": backlog[:40]}

    except Exception as exc:
        LOGGER.warning("workspace_os get_day_view failed: %s", str(exc))
        return {"focus": [], "agent_flagged": [], "backlog": []}
    finally:
        con.close()


# ── Ticker timeline ───────────────────────────────────────────────────────────
def get_ticker_timeline(ticker: str, limit: int = 60) -> list[dict]:
    """Unified timeline from investment_records_core + investor_annotations_core."""
    ensure_workspace_os_schema()
    tk = str(ticker or "").strip().upper()
    if not tk or not pg_enabled():
        return []
    lim = max(1, min(500, int(limit or 60)))
    con = pg_connect()
    if con is None:
        return []
    items: list[dict] = []
    try:
        cur = con.cursor()

        # investor_annotations_core (tasks + notes for this ticker)
        try:
            cur.execute(
                """SELECT id, annotation_type,
                          COALESCE(content_text, content, '') AS body,
                          COALESCE(priority,'P2') AS priority,
                          status, created_at
                   FROM investor_annotations_core
                   WHERE entity_id=%s
                     AND annotation_type IN ('task','note','company_note')
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (tk, lim),
            )
            for r in _iter_cursor_rows(cur):
                atype = str(r[1] or "task")
                kind = "action" if atype == "task" else "thought"
                status = str(r[4] or "open")
                if status not in ("open", "done", "archived"):
                    status = "open"
                items.append({
                    "_source": "todo" if atype == "task" else "note",
                    "id": int(r[0] or 0),
                    "kind": kind,
                    "ticker": tk,
                    "title": str(r[2] or "")[:300],
                    "body": "",
                    "status": status,
                    "priority": _norm_priority(str(r[3] or "P2")),
                    "created_by": "user",
                    "created_at": str(r[5] or ""),
                })
        except Exception:
            pass

        # investment_records_core
        try:
            cur.execute(
                """SELECT id, kind, title, body, status, priority, created_by, created_at
                   FROM investment_records_core
                   WHERE ticker=%s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (tk, lim),
            )
            for r in _iter_cursor_rows(cur):
                items.append({
                    "_source": "record",
                    "id": int(r[0] or 0),
                    "kind": str(r[1] or "action"),
                    "ticker": tk,
                    "title": str(r[2] or ""),
                    "body": str(r[3] or ""),
                    "status": str(r[4] or "open"),
                    "priority": str(r[5] or "normal"),
                    "created_by": str(r[6] or "user"),
                    "created_at": str(r[7] or ""),
                })
        except Exception:
            pass

        # action_proposals_core (agent signals for this ticker)
        try:
            cur.execute(
                """SELECT id, title, thesis_summary, status, created_at
                   FROM action_proposals_core
                   WHERE ticker=%s
                   ORDER BY created_at DESC
                   LIMIT 20""",
                (tk,),
            )
            for r in _iter_cursor_rows(cur):
                st = str(r[3] or "open")
                items.append({
                    "_source": "proposal",
                    "id": int(r[0] or 0),
                    "kind": "decision",
                    "ticker": tk,
                    "title": str(r[1] or ""),
                    "body": str(r[2] or "")[:300],
                    "status": "done" if st in ("executed", "rejected", "debate_rejected") else "open",
                    "priority": "high",
                    "created_by": "agent",
                    "created_at": str(r[4] or ""),
                })
        except Exception:
            pass

        # Sort by created_at descending
        items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        return items[:lim]

    except Exception as exc:
        LOGGER.warning("workspace_os get_ticker_timeline failed ticker=%s err=%s", tk, str(exc))
        return []
    finally:
        con.close()


# ── investment_records_core CRUD ──────────────────────────────────────────────
def create_record(
    *,
    kind: str = "action",
    domain: str = "work",
    title: str = "",
    body: str = "",
    ticker: str = "",
    priority: str = "normal",
    due_date: str | None = None,
    source: str = "manual",
    created_by: str = "user",
    sentiment: str = "neutral",
    pinned: bool = False,
    source_ref_id: str = "",
    url: str = "",
    emoji: str = "",
    project_space: str = "general",
    project_id: int | None = None,
) -> int:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    dd: object = None
    if due_date:
        try:
            dd = dt.date.fromisoformat(str(due_date).strip())
        except Exception:
            dd = None
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO investment_records_core
                (kind, domain, ticker, title, body, sentiment, status, priority,
                 due_date, pinned, source, created_by, source_ref_id,
                 url, emoji, project_space, project_id)
               VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (
                str(kind or "action")[:40],
                str(domain or "work")[:20],
                str(ticker or "").upper()[:20],
                str(title or "")[:500],
                str(body or "")[:4000],
                str(sentiment or "neutral")[:20],
                str(priority or "normal")[:10],
                dd,
                bool(pinned),
                str(source or "manual")[:40],
                str(created_by or "user")[:40],
                str(source_ref_id or "")[:120],
                str(url or "")[:1000],
                str(emoji or "")[:10],
                str(project_space or "general")[:40],
                int(project_id) if project_id else None,
            ),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning(
            "workspace_os create_record failed ticker=%s kind=%s title=%s err=%s",
            str(ticker or "").upper()[:20],
            str(kind or "action")[:40],
            str(title or "")[:80],
            str(exc),
        )
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_record_status(rec_id: int, status: str, approved_by: str = "") -> bool:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        now = dt.datetime.now(dt.timezone.utc)
        if status == "approved" and approved_by:
            cur.execute(
                """UPDATE investment_records_core
                   SET status=%s, approved_by=%s, approved_at=%s, updated_at=NOW()
                   WHERE id=%s""",
                (str(status)[:20], str(approved_by)[:80], now, rec_id),
            )
        else:
            cur.execute(
                "UPDATE investment_records_core SET status=%s, updated_at=NOW() WHERE id=%s",
                (str(status)[:20], rec_id),
            )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("workspace_os update_record_status failed id=%s status=%s err=%s", rec_id, status, str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_record(rec_id: int, **fields) -> bool:
    allowed = {"kind", "domain", "ticker", "title", "body", "sentiment",
               "status", "priority", "due_date", "pinned", "source_ref_id",
               "url", "emoji", "project_space"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    ensure_workspace_os_schema()
    if not updates or not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        log_entry = json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "changes": {k: str(v) for k, v in updates.items()}})
        set_parts = ", ".join(f"{k}=%s" for k in updates)
        vals = list(updates.values()) + [log_entry, rec_id]
        cur.execute(
            f"""UPDATE investment_records_core
               SET {set_parts}, updated_at=NOW(),
                   change_log = change_log || %s::jsonb
               WHERE id=%s""",
            vals,
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("workspace_os update_record failed id=%s err=%s", rec_id, str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_record(rec_id: int) -> bool:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM investment_records_core WHERE id=%s", (rec_id,))
        deleted = int(getattr(cur, "rowcount", 0) or 0)
        con.commit()
        return deleted > 0
    except Exception as exc:
        LOGGER.warning("workspace_os delete_record failed id=%s err=%s", rec_id, str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def pin_record(rec_id: int, pinned: bool) -> bool:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE investment_records_core SET pinned=%s, updated_at=NOW() WHERE id=%s",
            (bool(pinned), rec_id),
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("workspace_os pin_record failed id=%s err=%s", rec_id, str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_record(rec_id: int) -> dict | None:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM investment_records_core WHERE id=%s LIMIT 1", (rec_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(cur.description, row)
    except Exception as exc:
        LOGGER.warning("workspace_os get_record failed id=%s err=%s", rec_id, str(exc))
        return None
    finally:
        con.close()


def list_records(
    kind: str | None = None,
    domain: str | None = None,
    status: str | None = None,
    ticker: str | None = None,
    project_space: str | None = None,
    project_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        where: list[str] = []
        vals: list[Any] = []
        if kind:
            where.append("kind=%s"); vals.append(str(kind))
        if domain:
            where.append("domain=%s"); vals.append(str(domain))
        if status:
            where.append("status=%s"); vals.append(str(status))
        if ticker:
            where.append("ticker=%s"); vals.append(str(ticker).upper())
        if project_space:
            where.append("project_space=%s"); vals.append(str(project_space))
        if project_id:
            where.append("project_id=%s"); vals.append(int(project_id))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        lim = max(1, min(500, int(limit or 50)))
        cur.execute(
            f"SELECT * FROM investment_records_core {clause} ORDER BY created_at DESC LIMIT %s",
            vals + [lim],
        )
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.warning("workspace_os list_records failed err=%s", str(exc))
        return []
    finally:
        con.close()


# ── Sidebar data ──────────────────────────────────────────────────────────────
def get_sidebar_data() -> dict:
    """Return portfolio + watchlist tickers with names, plus open task count."""
    fallback: dict = {"portfolio": [], "watchlist": [], "open_tasks": 0}
    if not pg_enabled():
        return fallback
    con = pg_connect()
    if con is None:
        return fallback
    try:
        cur = con.cursor()

        # --- Portfolio ---
        portfolio: list[dict] = []
        try:
            cur.execute(
                "SELECT ticker FROM portfolio_positions_core ORDER BY ticker ASC"
            )
            portfolio = [{"ticker": str(r[0] or "").upper(), "name": ""} for r in _iter_cursor_rows(cur)]
        except Exception:
            pass

        # --- Watchlist ---
        watchlist: list[dict] = []
        try:
            cur.execute(
                "SELECT ticker FROM watchlist_core ORDER BY ticker ASC"
            )
            watchlist = [{"ticker": str(r[0] or "").upper(), "name": ""} for r in _iter_cursor_rows(cur)]
        except Exception:
            pass

        # --- Enrich with names from universe_registry_core ---
        all_tickers = list({c["ticker"] for c in portfolio + watchlist})
        name_map: dict[str, str] = {}
        if all_tickers:
            try:
                placeholders = ",".join(["%s"] * len(all_tickers))
                cur.execute(
                    f"SELECT ticker, name FROM universe_registry_core WHERE ticker IN ({placeholders})",
                    all_tickers,
                )
                for r in _iter_cursor_rows(cur):
                    name_map[str(r[0] or "").upper()] = str(r[1] or "")
            except Exception:
                pass
        for c in portfolio:
            c["name"] = name_map.get(c["ticker"], "")
        for c in watchlist:
            c["name"] = name_map.get(c["ticker"], "")

        # --- Open task count ---
        open_tasks = 0
        try:
            cur.execute(
                "SELECT COUNT(*) FROM investment_records_core WHERE status='open'"
            )
            row = cur.fetchone()
            open_tasks = int(row[0] or 0) if row else 0
        except Exception:
            pass

        return {"portfolio": portfolio, "watchlist": watchlist, "open_tasks": open_tasks}
    except Exception:
        return fallback
    finally:
        con.close()


# ── AI context (cached) ───────────────────────────────────────────────────────
def get_ticker_ai_context(ticker: str) -> str:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return ""
    now = time.time()
    cached = _AI_INSIGHT_CACHE.get(tk)
    if cached and now < cached[1]:
        return cached[0]

    timeline = get_ticker_timeline(tk, limit=8)
    thesis_text = ""
    latest_filing = ""

    try:
        from app.services.postgres_core_service import list_watchlist_thesis_pg
        for t in list_watchlist_thesis_pg(limit=200):
            if str(t.get("ticker") or "").upper() == tk:
                thesis_text = str(t.get("thesis") or "")
                break
    except Exception:
        pass

    try:
        con = pg_connect()
        if con:
            try:
                cur = con.cursor()
                cur.execute(
                    """SELECT form_type, filed_at, content FROM filings_core
                       WHERE ticker=%s AND content IS NOT NULL
                       ORDER BY filed_at DESC LIMIT 1""",
                    (tk,),
                )
                row = cur.fetchone()
                if row:
                    latest_filing = f"{row[0]} filed {row[1]}: {str(row[2] or '')[:500]}"
            except Exception:
                pass
            finally:
                con.close()
    except Exception:
        pass

    rec_summary = "\n".join(
        f"- [{r.get('kind','?')}] {r.get('title','')[:100]} ({str(r.get('created_at',''))[:10]})"
        for r in timeline[:6]
    )
    prompt = (
        f"Ticker: {tk}\n"
        f"Thesis: {thesis_text[:400] or 'No thesis on file.'}\n"
        f"Recent activity:\n{rec_summary or 'None'}\n"
        f"Latest filing: {latest_filing[:500] or 'None'}\n\n"
        f"Give a 2-3 sentence investment insight for {tk}. Be specific. Do not repeat the thesis verbatim."
    )
    insight = ""
    try:
        from tools.llm_engine import ask_ai
        if ask_ai:
            insight = str(ask_ai(prompt, max_tokens=200) or "").strip()
    except Exception:
        pass
    if insight:
        _AI_INSIGHT_CACHE[tk] = (insight, now + _AI_CACHE_TTL)
    return insight


# ── Activity feed (unified from all investment signal tables) ─────────────────
def get_activity_feed(limit: int = 60) -> list[dict]:
    """Combined chronological feed from AI proposals, alerts, filings, agent runs, and user records."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    items: list[dict] = []
    try:
        cur = con.cursor()

        # AI proposals
        try:
            cur.execute(
                """SELECT id, ticker, title, status, confidence_score, created_at
                   FROM action_proposals_core
                   WHERE status IN ('open','pending','review')
                   ORDER BY created_at DESC LIMIT 25"""
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[1] or "").upper()
                items.append({
                    "kind": "proposal", "source": "ai", "emoji": "🤖",
                    "id": int(r[0] or 0), "ticker": tk,
                    "title": str(r[2] or ""),
                    "meta": f"Confidence {float(r[4] or 0) * 100:.0f}%",
                    "ts": str(r[5] or ""),
                    "actions": [
                        {"label": "Investigate", "url": f"/company_file?t={tk}"},
                        {"label": "Dismiss", "api": "dismiss", "source": "proposal", "id": int(r[0] or 0)},
                    ],
                })
        except Exception:
            pass

        # Thesis breach alerts
        try:
            cur.execute(
                """SELECT id, ticker, breach_type, breach_detail, severity, detected_at
                   FROM thesis_breach_alerts_core
                   WHERE COALESCE(status,'open')='open'
                   ORDER BY detected_at DESC LIMIT 15"""
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[1] or "").upper()
                items.append({
                    "kind": "breach_alert", "source": "ai", "emoji": "⚠️",
                    "id": int(r[0] or 0), "ticker": tk,
                    "title": f"Thesis Breach · {tk} · {str(r[2] or '')}",
                    "meta": str(r[3] or "")[:200],
                    "ts": str(r[5] or ""),
                    "actions": [
                        {"label": "Open", "url": f"/workspace/ticker/{tk}"},
                    ],
                })
        except Exception:
            pass

        # Cascade alerts
        try:
            cur.execute(
                """SELECT id, trigger_ticker, affected_ticker, effect_summary, severity, detected_at
                   FROM portfolio_cascade_alerts_core
                   WHERE COALESCE(status,'open')='open'
                   ORDER BY detected_at DESC LIMIT 10"""
            )
            for r in _iter_cursor_rows(cur):
                items.append({
                    "kind": "cascade_alert", "source": "ai", "emoji": "🔗",
                    "id": int(r[0] or 0),
                    "ticker": str(r[2] or "").upper(),
                    "title": f"Cascade · {str(r[1] or '').upper()} → {str(r[2] or '').upper()}",
                    "meta": str(r[3] or "")[:200],
                    "ts": str(r[5] or ""),
                    "actions": [],
                })
        except Exception:
            pass

        # Recent filings
        try:
            cur.execute(
                """SELECT ticker, form, date, downloaded_at
                   FROM filings_core
                   ORDER BY downloaded_at DESC LIMIT 20"""
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[0] or "").upper()
                ts = str(r[3] or r[2] or "")
                items.append({
                    "kind": "filing", "source": "sec", "emoji": "📄",
                    "ticker": tk,
                    "title": f"{str(r[1] or '')} · {tk}",
                    "meta": f"Filed {str(r[2] or '')}",
                    "ts": ts,
                    "actions": [
                        {"label": "SEC Filing", "url": f"/company_file/sec?t={tk}"},
                        {"label": "Analyze", "url": f"/company_file?t={tk}"},
                    ],
                })
        except Exception:
            pass

        # Agent runs
        try:
            cur.execute(
                """SELECT agent_name, trigger_type, status, started_at
                   FROM agent_runs_core
                   ORDER BY started_at DESC LIMIT 10"""
            )
            for r in _iter_cursor_rows(cur):
                st = str(r[2] or "").upper()
                items.append({
                    "kind": "agent_run", "source": "system", "emoji": "⚙️",
                    "title": f"{str(r[0] or 'Agent')} · {st}",
                    "meta": f"Trigger: {str(r[1] or '')}",
                    "ts": str(r[3] or ""),
                    "actions": [],
                })
        except Exception:
            pass

        # Recent user records (last 50)
        try:
            cur.execute(
                """SELECT id, kind, ticker, title, emoji, project_space, status, created_at
                   FROM investment_records_core
                   WHERE created_by='user'
                   ORDER BY created_at DESC LIMIT 20"""
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[2] or "").upper()
                items.append({
                    "kind": str(r[1] or "action"), "source": "user",
                    "emoji": str(r[4] or "📝"),
                    "id": int(r[0] or 0), "ticker": tk,
                    "title": str(r[3] or ""),
                    "meta": f"{str(r[5] or 'general')} · {str(r[6] or 'open')}",
                    "ts": str(r[7] or ""),
                    "actions": [],
                })
        except Exception:
            pass

        items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
        return items[:limit]

    except Exception as exc:
        LOGGER.warning("workspace_os get_activity_feed failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── Home activity (workspace-only: user records + todos + notes) ──────────────
def get_home_activity(limit: int = 20) -> list[dict]:
    """Activity feed showing only user-created workspace items — no filings, agent runs, or AI proposals."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    items: list[dict] = []
    try:
        cur = con.cursor()

        # User records (actions, notes, ideas, research)
        try:
            cur.execute(
                """SELECT id, kind, ticker, title, emoji, project_space, status, created_at
                   FROM investment_records_core
                   ORDER BY created_at DESC LIMIT %s""",
                [limit * 2],
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[2] or "").upper()
                kind = str(r[1] or "action")
                items.append({
                    "kind": kind, "source": "user",
                    "emoji": str(r[4] or "📝"),
                    "id": int(r[0] or 0), "ticker": tk,
                    "title": str(r[3] or "(untitled)"),
                    "meta": f"{str(r[5] or 'general')} · {str(r[6] or 'open')}",
                    "ts": str(r[7] or ""),
                })
        except Exception:
            pass

        # Recent todos (created/completed)
        try:
            cur.execute(
                """SELECT id,
                          COALESCE(content_text, content, '') AS task,
                          status,
                          COALESCE(priority,'P2') AS priority,
                          COALESCE(entity_id,'') AS ticker,
                          created_at
                   FROM investor_annotations_core
                   WHERE annotation_type='task'
                   ORDER BY created_at DESC LIMIT %s""",
                [limit * 2],
            )
            for r in _iter_cursor_rows(cur):
                st = str(r[2] or "open")
                em = "✅" if st == "done" else "☑️"
                items.append({
                    "kind": "task", "source": "user", "emoji": em,
                    "id": int(r[0] or 0),
                    "ticker": str(r[4] or "").upper(),
                    "title": str(r[1] or "(untitled)"),
                    "meta": f"{str(r[3] or 'P2')} · {st}",
                    "ts": str(r[5] or ""),
                })
        except Exception:
            pass

        # Recent notes
        try:
            cur.execute(
                """SELECT id,
                          COALESCE(content_text, content, '') AS note,
                          COALESCE(entity_id,'') AS ticker,
                          created_at
                   FROM investor_annotations_core
                   WHERE annotation_type='note'
                   ORDER BY created_at DESC LIMIT %s""",
                [limit],
            )
            for r in _iter_cursor_rows(cur):
                txt = str(r[1] or "")
                items.append({
                    "kind": "note", "source": "user", "emoji": "🗒️",
                    "id": int(r[0] or 0),
                    "ticker": str(r[2] or "").upper(),
                    "title": txt[:100] + ("…" if len(txt) > 100 else ""),
                    "ts": str(r[3] or ""),
                })
        except Exception:
            pass

        items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
        return items[:limit]

    except Exception as exc:
        LOGGER.warning("workspace_os get_home_activity failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── Inbox / Triage / References / Tasks ──────────────────────────────────────

def get_inbox_items(limit: int = 50) -> list[dict]:
    """Return records with kind='inbox' (untriaged captures), newest first."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, kind, domain, ticker, title, body, priority,
                      status, pinned, created_by, created_at, due_date, url, emoji, project_space
               FROM investment_records_core
               WHERE kind='inbox' AND status='open'
               ORDER BY created_at DESC
               LIMIT %s""",
            (max(1, min(200, int(limit or 50))),),
        )
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.warning("get_inbox_items failed: %s", str(exc))
        return []
    finally:
        con.close()


def triage_record(rec_id: int, new_kind: str) -> bool:
    """Triage an inbox item — set its kind to task/note/reference/question."""
    valid_kinds = ('task', 'note', 'reference', 'question', 'action')
    kind = str(new_kind or 'task').strip().lower()
    if kind not in valid_kinds:
        kind = 'task'
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE investment_records_core SET kind=%s, updated_at=NOW() WHERE id=%s",
            (kind, rec_id),
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("triage_record failed id=%s kind=%s err=%s", rec_id, kind, str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_references(ticker: str | None = None, limit: int = 50) -> list[dict]:
    """Return records with kind='reference', optionally filtered by ticker."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if ticker:
            cur.execute(
                """SELECT * FROM investment_records_core
                   WHERE kind='reference' AND ticker=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (str(ticker).upper(), max(1, min(200, int(limit or 50)))),
            )
        else:
            cur.execute(
                """SELECT * FROM investment_records_core
                   WHERE kind='reference'
                   ORDER BY created_at DESC LIMIT %s""",
                (max(1, min(200, int(limit or 50))),),
            )
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.warning("get_references failed: %s", str(exc))
        return []
    finally:
        con.close()


def get_tasks(status: str = 'open', limit: int = 100) -> list[dict]:
    """Return records with kind IN ('task','action') for the Tasks view."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT * FROM investment_records_core
               WHERE kind IN ('task','action') AND status=%s
               ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                        created_at DESC
               LIMIT %s""",
            (str(status or 'open'), max(1, min(500, int(limit or 100)))),
        )
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.warning("get_tasks failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── Smart automation queries ─────────────────────────────────────────────────

def get_overdue_tasks(limit: int = 10) -> list[dict]:
    """Tasks with due_date in the past and still open."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT * FROM investment_records_core
               WHERE kind IN ('task','action') AND status='open'
                 AND due_date IS NOT NULL AND due_date < CURRENT_DATE
               ORDER BY due_date ASC LIMIT %s""",
            (max(1, min(50, limit)),),
        )
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception:
        return []
    finally:
        con.close()


def get_stale_records(days: int = 14, limit: int = 10) -> list[dict]:
    """Open records with no update in N days (ignored items that need attention)."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT * FROM investment_records_core
               WHERE status='open' AND kind NOT IN ('inbox')
                 AND updated_at < NOW() - INTERVAL '%s days'
               ORDER BY updated_at ASC LIMIT %s""",
            (max(1, days), max(1, min(50, limit))),
        )
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception:
        return []
    finally:
        con.close()


def get_blocked_items(limit: int = 10) -> list[dict]:
    """Items that are linked with 'blocks' relation and the blocker is still open."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        # Find records blocked by another item (via work_links_core with relation='blocks')
        cur.execute(
            """SELECT DISTINCT r.*
               FROM investment_records_core r
               JOIN work_links_core wl ON
                 (wl.b_type='record' AND wl.b_id=CAST(r.id AS TEXT) AND wl.relation='blocks')
                 OR (wl.a_type='record' AND wl.a_id=CAST(r.id AS TEXT) AND wl.relation='blocks')
               WHERE r.status='open'
               ORDER BY r.updated_at DESC LIMIT %s""",
            (max(1, min(50, limit)),),
        )
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception:
        return []
    finally:
        con.close()


# ── Calendar data (records with due dates + earnings events) ─────────────────
def get_calendar_data(
    project_space: str | None = None,
    year: int | None = None,
    month: int | None = None,
) -> list[dict]:
    """Records with due_date for calendar view, plus earnings events from the app."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    items: list[dict] = []
    try:
        cur = con.cursor()
        where_parts = ["due_date IS NOT NULL", "status NOT IN ('done','rejected','archived')"]
        vals: list = []
        if project_space and project_space not in ("today", "inbox", "all", ""):
            where_parts.append("project_space=%s")
            vals.append(str(project_space))
        if year and month:
            where_parts.append("EXTRACT(YEAR FROM due_date)=%s AND EXTRACT(MONTH FROM due_date)=%s")
            vals.extend([year, month])
        clause = " AND ".join(where_parts)
        cur.execute(
            f"""SELECT id, kind, ticker, title, emoji, project_space, priority, status, due_date
                FROM investment_records_core WHERE {clause} ORDER BY due_date ASC LIMIT 300""",
            vals,
        )
        for r in _iter_cursor_rows(cur):
            dd = r[8]
            items.append({
                "id": int(r[0] or 0), "kind": str(r[1] or "action"),
                "ticker": str(r[2] or "").upper(), "title": str(r[3] or ""),
                "emoji": str(r[4] or ""), "project_space": str(r[5] or "general"),
                "priority": str(r[6] or "normal"), "status": str(r[7] or "open"),
                "due_date": dd.isoformat() if hasattr(dd, "isoformat") else str(dd or ""),
                "is_event": False,
            })

        # Earnings events from backend
        try:
            ec_where = []
            ec_vals: list = []
            if year and month:
                ec_where.append(
                    "report_date IS NOT NULL AND "
                    "EXTRACT(YEAR FROM report_date::date)=%s AND "
                    "EXTRACT(MONTH FROM report_date::date)=%s"
                )
                ec_vals.extend([year, month])
            else:
                ec_where.append("report_date IS NOT NULL")
            cur.execute(
                f"""SELECT ticker, report_date FROM earnings_calendar_snapshot_core
                    WHERE {' AND '.join(ec_where)} ORDER BY report_date ASC LIMIT 100""",
                ec_vals,
            )
            for r in _iter_cursor_rows(cur):
                tk = str(r[0] or "").upper()
                items.append({
                    "id": 0, "kind": "earnings", "ticker": tk,
                    "title": f"📊 Earnings · {tk}",
                    "emoji": "📊", "project_space": "portfolio",
                    "priority": "high", "status": "event",
                    "due_date": str(r[1] or ""),
                    "is_event": True,
                })
        except Exception:
            pass

        # Google Calendar events
        try:
            from app.services.google_workspace_service import get_month_calendar_events
            if year and month:
                gcal_events = get_month_calendar_events(year, month)
                for ev in gcal_events:
                    items.append({
                        "id": 0, "kind": "gcal", "ticker": "",
                        "title": f"📆 {ev.get('time', '')} {ev.get('title', '')}".strip(),
                        "emoji": "📆", "project_space": "general",
                        "priority": "normal", "status": "event",
                        "due_date": ev.get("date", ""),
                        "is_event": True,
                    })
        except Exception:
            pass

        return items

    except Exception as exc:
        LOGGER.warning("workspace_os get_calendar_data failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── workspace_projects_core CRUD ──────────────────────────────────────────────

def create_project(
    *,
    name: str,
    emoji: str = "🎯",
    description: str = "",
    color: str = "#6366f1",
) -> int:
    """Create a new project. Returns the new project id or 0 on failure."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO workspace_projects_core (name, emoji, description, color)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (str(name or "")[:200], str(emoji or "🎯")[:10],
             str(description or "")[:2000], str(color or "#6366f1")[:20]),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("workspace_os create_project failed: %s", str(exc))
        try:
            con.rollback()
        except Exception as rollback_exc:
            LOGGER.warning("workspace_os create_project rollback failed: %s", str(rollback_exc), exc_info=True)
        return 0
    finally:
        con.close()


def list_projects() -> list[dict]:
    """List all active projects, with record count for each."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT p.id, p.name, p.emoji, p.description, p.color, p.status,
                      p.created_at,
                      COUNT(r.id) FILTER (WHERE r.status NOT IN ('done','rejected','archived')) AS open_count,
                      COUNT(r.id) AS total_count
               FROM workspace_projects_core p
               LEFT JOIN investment_records_core r ON r.project_id = p.id
               WHERE p.status = 'active'
               GROUP BY p.id
               ORDER BY p.created_at DESC"""
        )
        rows = cur.fetchall() or []
        result = []
        for r in rows:
            created = r[6]
            result.append({
                "id": int(r[0]),
                "name": str(r[1] or ""),
                "emoji": str(r[2] or "🎯"),
                "description": str(r[3] or ""),
                "color": str(r[4] or "#6366f1"),
                "status": str(r[5] or "active"),
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                "open_count": int(r[7] or 0),
                "total_count": int(r[8] or 0),
            })
        return result
    except Exception as exc:
        LOGGER.warning("workspace_os list_projects failed: %s", str(exc))
        return []
    finally:
        con.close()


def update_project(project_id: int, **fields) -> bool:
    """Update project fields (name, emoji, description, color, status)."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    allowed = {"name", "emoji", "description", "color", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        set_clause += ", updated_at=NOW()"
        cur.execute(
            f"UPDATE workspace_projects_core SET {set_clause} WHERE id=%s",
            list(updates.values()) + [int(project_id)],
        )
        con.commit()
        return cur.rowcount > 0
    except Exception as exc:
        LOGGER.warning("workspace_os update_project failed: %s", str(exc))
        try:
            con.rollback()
        except Exception as rollback_exc:
            LOGGER.warning("workspace_os update_project rollback failed: %s", str(rollback_exc), exc_info=True)
        return False
    finally:
        con.close()


def delete_project(project_id: int) -> bool:
    """Soft-delete a project (set status=archived). Records are kept."""
    return update_project(project_id, status="archived")


# ── Hub links CRUD ────────────────────────────────────────────────────────────

def list_links() -> list[dict]:
    """Return all active links ordered by category + sort_order."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, title, url, category, description, pinned, sort_order
               FROM workspace_links_core
               WHERE status='active'
               ORDER BY category, sort_order, id"""
        )
        rows = cur.fetchall()
        return [
            {
                "id": r[0], "title": r[1], "url": r[2],
                "category": r[3], "description": r[4],
                "pinned": bool(r[5]), "sort_order": int(r[6]),
            }
            for r in rows
        ]
    except Exception as exc:
        LOGGER.warning("workspace_os list_links failed: %s", str(exc))
        return []
    finally:
        con.close()


def create_link(*, title: str, url: str, category: str,
                description: str = "", pinned: bool = False) -> int:
    """Insert a new hub link and return its id (0 on failure)."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        # Place at end of category
        cur.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM workspace_links_core WHERE category=%s AND status='active'",
            (category,),
        )
        next_order = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO workspace_links_core(title, url, category, description, pinned, sort_order)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
            (title, url, category, description, pinned, next_order),
        )
        rec_id = cur.fetchone()[0]
        con.commit()
        return int(rec_id)
    except Exception as exc:
        LOGGER.warning("workspace_os create_link failed: %s", str(exc))
        try:
            con.rollback()
        except Exception as rollback_exc:
            LOGGER.warning("workspace_os create_link rollback failed: %s", str(rollback_exc), exc_info=True)
        return 0
    finally:
        con.close()


def update_link(link_id: int, **fields) -> bool:
    """Update link fields (title, url, category, description, pinned, sort_order)."""
    ensure_workspace_os_schema()
    if not pg_enabled():
        return False
    allowed = {"title", "url", "category", "description", "pinned", "sort_order"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        set_clause += ", updated_at=NOW()"
        cur.execute(
            f"UPDATE workspace_links_core SET {set_clause} WHERE id=%s AND status='active'",
            list(updates.values()) + [int(link_id)],
        )
        con.commit()
        return cur.rowcount > 0
    except Exception as exc:
        LOGGER.warning("workspace_os update_link failed: %s", str(exc))
        try:
            con.rollback()
        except Exception as rollback_exc:
            LOGGER.warning("workspace_os update_link rollback failed: %s", str(rollback_exc), exc_info=True)
        return False
    finally:
        con.close()


def delete_link(link_id: int) -> bool:
    """Soft-delete a link (set status=archived)."""
    return update_link(link_id, status="archived")
