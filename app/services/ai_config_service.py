"""
DB-driven AI configuration service.

Tables:
  - ai_config_core        — scalar config (thresholds, weights, limits)
  - sector_peers_config   — sector peer mappings (29 tickers)
  - macro_tickers_config  — macro dashboard tickers (18 symbols)

Priority: env var > DB > code default.  30-second TTL cache.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from app.services.postgres_core_service import pg_connect

log = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────────────
_CONFIG_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 30.0

# ── Env-var override mapping (config_key → env var name) ─────────────────────
_ENV_OVERRIDES: dict[str, str] = {
    "insider_trade_min_usd": "INSIDER_TRADE_MIN_USD",
    "debate_gate_min_confidence": "DEBATE_GATE_MIN_CONFIDENCE",
    "token_budget": "AI_TOKEN_BUDGET",
    "token_overhead": "AI_TOKEN_OVERHEAD",
    "llm_fallback_timeout_sec": "LLM_FALLBACK_TIMEOUT_SEC",
    "poll_forms": "POLL_FORMS",
}


# ── Public API ───────────────────────────────────────────────────────────────

def get_config(key: str, default: Any = None) -> Any:
    """Read a config value.  Priority: env var > DB > code default.  30s TTL cache."""
    # 1. Env var override (fast, no cache needed)
    env_name = _ENV_OVERRIDES.get(key)
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val is not None:
            return _coerce(env_val, default)

    # 2. Cache check
    now = time.time()
    with _CACHE_LOCK:
        entry = _CONFIG_CACHE.get(key)
        if entry and (now - entry[0]) < _CACHE_TTL_SEC:
            return entry[1]

    # 3. DB lookup
    try:
        con = pg_connect()
        if con is not None:
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT config_value FROM ai_config_core WHERE config_key = %s LIMIT 1",
                    (key,),
                )
                row = cur.fetchone()
                if row:
                    val = row[0] if isinstance(row, (list, tuple)) else row.get("config_value", row[0])  # type: ignore[union-attr]
                    # JSONB comes back as Python object already in psycopg2
                    result = val if val is not None else default
                    with _CACHE_LOCK:
                        _CONFIG_CACHE[key] = (now, result)
                    return result
            except Exception:
                pass
            finally:
                con.close()
    except Exception:
        pass

    # 4. Code default
    return default


def set_config(key: str, value: Any, description: str = "") -> None:
    """Upsert a config value + invalidate cache."""
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO ai_config_core (config_key, config_value, description, updated_at)
               VALUES (%s, %s, %s, NOW()::text)
               ON CONFLICT (config_key) DO UPDATE
               SET config_value = EXCLUDED.config_value,
                   description = CASE WHEN EXCLUDED.description = '' THEN ai_config_core.description ELSE EXCLUDED.description END,
                   updated_at = EXCLUDED.updated_at""",
            (key, json.dumps(value), description),
        )
        con.commit()
    except Exception as exc:
        log.warning("set_config(%s) failed: %s", key, exc)
    finally:
        con.close()
    # Invalidate cache
    with _CACHE_LOCK:
        _CONFIG_CACHE.pop(key, None)


def get_sector_peers(ticker: str) -> list[str] | None:
    """Read peers from sector_peers_config.  Returns None if not found (caller should use fallback)."""
    tk = ticker.upper().strip()
    if not tk:
        return None
    try:
        con = pg_connect()
        if con is not None:
            try:
                cur = con.cursor()
                cur.execute("SELECT peers FROM sector_peers_config WHERE ticker = %s LIMIT 1", (tk,))
                row = cur.fetchone()
                if row:
                    val = row[0] if isinstance(row, (list, tuple)) else row.get("peers", row[0])  # type: ignore[union-attr]
                    if isinstance(val, list):
                        return val
                    if isinstance(val, str):
                        return json.loads(val)
                    return val
            except Exception:
                pass
            finally:
                con.close()
    except Exception:
        pass
    return None


def get_macro_tickers() -> dict[str, str] | None:
    """Read enabled macro tickers from macro_tickers_config.  Returns None if not found."""
    try:
        con = pg_connect()
        if con is not None:
            try:
                cur = con.cursor()
                cur.execute("SELECT symbol, label FROM macro_tickers_config WHERE enabled = TRUE")
                rows = cur.fetchall()
                if rows:
                    result: dict[str, str] = {}
                    for r in rows:
                        if isinstance(r, (list, tuple)):
                            result[r[0]] = r[1]
                        else:
                            result[r["symbol"]] = r["label"]
                    if result:
                        return result
            except Exception:
                pass
            finally:
                con.close()
    except Exception:
        pass
    return None


# ── Schema + seed ────────────────────────────────────────────────────────────

def ensure_ai_config_schema() -> None:
    """Create config tables and seed defaults if empty."""
    con = pg_connect()
    if con is None:
        log.warning("ai_config_service: no Postgres connection, skipping schema")
        return
    try:
        cur = con.cursor()

        # ── Create tables ────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_config_core (
                config_key   TEXT PRIMARY KEY,
                config_value JSONB NOT NULL DEFAULT '{}',
                description  TEXT NOT NULL DEFAULT '',
                updated_at   TEXT NOT NULL DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_peers_config (
                ticker     TEXT PRIMARY KEY,
                peers      JSONB NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS macro_tickers_config (
                symbol     TEXT PRIMARY KEY,
                label      TEXT NOT NULL DEFAULT '',
                enabled    BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        con.commit()

        # ── Seed ai_config_core if empty ─────────────────────────────────
        cur.execute("SELECT COUNT(*) FROM ai_config_core")
        cnt = cur.fetchone()
        count_val = cnt[0] if isinstance(cnt, (list, tuple)) else cnt.get("count", 0)  # type: ignore[union-attr]
        if int(count_val) == 0:
            _seed_config(cur)
            con.commit()

        # ── Seed sector_peers_config if empty ────────────────────────────
        cur.execute("SELECT COUNT(*) FROM sector_peers_config")
        cnt = cur.fetchone()
        count_val = cnt[0] if isinstance(cnt, (list, tuple)) else cnt.get("count", 0)  # type: ignore[union-attr]
        if int(count_val) == 0:
            _seed_sector_peers(cur)
            con.commit()

        # ── Seed macro_tickers_config if empty ───────────────────────────
        cur.execute("SELECT COUNT(*) FROM macro_tickers_config")
        cnt = cur.fetchone()
        count_val = cnt[0] if isinstance(cnt, (list, tuple)) else cnt.get("count", 0)  # type: ignore[union-attr]
        if int(count_val) == 0:
            _seed_macro_tickers(cur)
            con.commit()

        log.info("ai_config_service: schema + seeds OK")
    except Exception as exc:
        log.warning("ai_config_service: schema/seed error: %s", exc)
    finally:
        con.close()


# ── Seed data ────────────────────────────────────────────────────────────────

def _seed_config(cur) -> None:
    """Seed 16 default config keys."""
    seeds: list[tuple[str, Any, str]] = [
        ("insider_trade_min_usd", 500000, "Minimum insider trade USD value for Form 4 significance filter"),
        ("debate_gate_min_confidence", 0.5, "Minimum proposal confidence to trigger multi-agent debate"),
        ("cascade_weights", {"base": 35, "coverage": 35, "citation": 18, "seed": 12, "missing_penalty": 10}, "Cascade analysis confidence formula weights"),
        ("risk_veto", {
            "enabled": True,
            "min_confidence_for_mutation": 0.62,
            "max_single_add_pct": 5.0,
            "max_position_weight_pct": 20.0,
            "max_var95_pct": 6.0,
            "max_cvar95_pct": 8.0,
            "min_quote_coverage_pct": 75.0,
            "high_impact_notional_pct": 3.0,
            "require_known_ticker_scope": True,
            "block_on_unknown_ticker": True,
            "review_for_high_impact": True,
        }, "Risk veto gate thresholds"),
        ("token_budget", 28000, "Max token budget for orchestrator prompt assembly"),
        ("token_overhead", 1200, "Estimated token overhead for query + system contract + labels"),
        ("fuzzy_threshold", 0.74, "Fuzzy matching threshold for orchestrator intent detection"),
        ("low_confidence_threshold", 0.45, "Low confidence threshold for intent classification"),
        ("mid_confidence_threshold", 0.70, "Mid confidence threshold for intent classification"),
        ("llm_fallback_timeout_sec", 6.0, "LLM fallback timeout in seconds"),
        ("manager_llm_timeout_sec", 1.2, "Manager LLM classification timeout in seconds"),
        ("manager_interrupt_threshold", 0.82, "Manager interrupt confidence threshold"),
        ("var95_multiplier", 1.65, "VaR 95% z-score multiplier"),
        ("cvar95_multiplier", 2.06, "CVaR 95% z-score multiplier"),
        ("poll_forms", ["8-K", "10-Q", "10-K", "6-K", "20-F", "DEF 14A", "DEFA14A", "PRE 14A", "DEF 14C"], "SEC form types to poll"),
        ("peer_comparison_pairs", [
            ["HUBS", "CRM", "CRM software"],
            ["GOOGL", "META", "digital advertising"],
            ["MSFT", "ORCL", "enterprise software"],
        ], "Peer comparison pairs for portfolio health prompts"),
    ]
    for key, value, desc in seeds:
        cur.execute(
            """INSERT INTO ai_config_core (config_key, config_value, description, updated_at)
               VALUES (%s, %s, %s, NOW()::text) ON CONFLICT DO NOTHING""",
            (key, json.dumps(value), desc),
        )
    log.info("ai_config_service: seeded %d config keys", len(seeds))


def _seed_sector_peers(cur) -> None:
    """Seed 29 sector peer mappings."""
    peers: dict[str, list[str]] = {
        "AAPL": ["MSFT", "GOOGL", "META"],
        "MSFT": ["AAPL", "GOOGL", "CRM"],
        "GOOGL": ["META", "MSFT", "AMZN"],
        "GOOG": ["META", "MSFT", "AMZN"],
        "META": ["GOOGL", "SNAP", "PINS"],
        "AMZN": ["MSFT", "GOOGL", "WMT"],
        "NVDA": ["AMD", "INTC", "QCOM"],
        "AMD": ["NVDA", "INTC", "QCOM"],
        "INTC": ["NVDA", "AMD", "TSM"],
        "QCOM": ["NVDA", "AMD", "MRVL"],
        "TSM": ["INTC", "SMSN", "UMC"],
        "CRM": ["MSFT", "SAP", "ORCL"],
        "ORCL": ["MSFT", "CRM", "SAP"],
        "NFLX": ["DIS", "WBD", "PARA"],
        "DIS": ["NFLX", "WBD", "CMCSA"],
        "JPM": ["BAC", "WFC", "GS"],
        "BAC": ["JPM", "WFC", "C"],
        "GS": ["MS", "JPM", "BLK"],
        "XOM": ["CVX", "COP", "BP"],
        "CVX": ["XOM", "COP", "SLB"],
        "TSLA": ["GM", "F", "RIVN"],
        "COST": ["WMT", "TGT", "BJ"],
        "WMT": ["COST", "TGT", "AMZN"],
        "UNH": ["CVS", "CI", "HUM"],
        "LLY": ["NVO", "PFE", "MRK"],
        "ASML": ["AMAT", "LRCX", "KLAC"],
        "WYNN": ["LVS", "MGM", "CZR"],
        "ADBE": ["CRM", "MSFT", "FIGMA"],
        "PYPL": ["V", "MA", "SQ"],
    }
    for tk, peer_list in peers.items():
        cur.execute(
            """INSERT INTO sector_peers_config (ticker, peers, updated_at)
               VALUES (%s, %s, NOW()::text) ON CONFLICT DO NOTHING""",
            (tk, json.dumps(peer_list)),
        )
    log.info("ai_config_service: seeded %d sector peer mappings", len(peers))


def _seed_macro_tickers(cur) -> None:
    """Seed 18 macro dashboard tickers."""
    tickers: dict[str, str] = {
        "CL=F":     "WTI Crude Oil",
        "BZ=F":     "Brent Crude",
        "GC=F":     "Gold",
        "SI=F":     "Silver",
        "NG=F":     "Natural Gas",
        "HG=F":     "Copper",
        "^GSPC":    "S&P 500",
        "^IXIC":    "Nasdaq",
        "^DJI":     "Dow Jones",
        "^RUT":     "Russell 2000",
        "^VIX":     "VIX (Fear Index)",
        "^TNX":     "10Y Treasury Yield",
        "^TYX":     "30Y Treasury Yield",
        "DX-Y.NYB": "US Dollar Index",
        "EURUSD=X": "EUR/USD",
        "JPY=X":    "USD/JPY",
        "BTC-USD":  "Bitcoin",
        "ETH-USD":  "Ethereum",
    }
    for sym, label in tickers.items():
        cur.execute(
            """INSERT INTO macro_tickers_config (symbol, label, enabled, updated_at)
               VALUES (%s, %s, TRUE, NOW()::text) ON CONFLICT DO NOTHING""",
            (sym, label),
        )
    log.info("ai_config_service: seeded %d macro tickers", len(tickers))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _coerce(env_val: str, default: Any) -> Any:
    """Coerce an env var string to match the type of the default value."""
    if default is None:
        return env_val
    if isinstance(default, bool):
        return env_val.lower() in ("1", "true", "yes")
    if isinstance(default, int):
        try:
            return int(env_val)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(env_val)
        except ValueError:
            return default
    if isinstance(default, (list, dict)):
        try:
            return json.loads(env_val)
        except (json.JSONDecodeError, ValueError):
            return default
    return env_val
