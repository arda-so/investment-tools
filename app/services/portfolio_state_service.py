from __future__ import annotations

import datetime as dt
from typing import Any

from app.core import cloud_files
from app.core.ticker import normalize_ticker
from app.services.postgres_core_service import pg_connect, strict_postgres_mode


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _ensure_state_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_positions_core (
                ticker TEXT PRIMARY KEY,
                shares DOUBLE PRECISION NOT NULL DEFAULT 0,
                cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_balances_core (
                currency TEXT PRIMARY KEY,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_core (
                ticker TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Migration: add category column if missing
        try:
            cur.execute("ALTER TABLE watchlist_core ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        except Exception:
            con.rollback()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_groups_core (
                group_name TEXT PRIMARY KEY,
                color TEXT NOT NULL DEFAULT '',
                emoji TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Portfolio accounts table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_accounts_core (
                account_name TEXT PRIMARY KEY,
                color TEXT NOT NULL DEFAULT '#6366f1',
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Seed default account
        cur.execute(
            """
            INSERT INTO portfolio_accounts_core (account_name, color, sort_order)
            VALUES ('Main', '#6366f1', 0)
            ON CONFLICT (account_name) DO NOTHING
            """
        )
        # Migration: add account column to portfolio_positions_core
        try:
            cur.execute("ALTER TABLE portfolio_positions_core ADD COLUMN account TEXT NOT NULL DEFAULT 'Main'")
        except Exception:
            con.rollback()
        # Migration: change primary key from (ticker) to (ticker, account)
        try:
            cur.execute("ALTER TABLE portfolio_positions_core DROP CONSTRAINT portfolio_positions_core_pkey")
            cur.execute("ALTER TABLE portfolio_positions_core ADD PRIMARY KEY (ticker, account)")
        except Exception:
            con.rollback()
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


def _read_positions_rows_pg(cur: Any) -> list[dict[str, str]]:
    cur.execute("SELECT ticker, shares, cost, note, COALESCE(account, 'Main') FROM portfolio_positions_core ORDER BY account ASC, ticker ASC")
    rows = cur.fetchall() or []
    out = [
        {
            "ticker": normalize_ticker(str(r[0] or "")),
            "shares": f"{_to_float(r[1], 0.0):g}",
            "cost": f"{_to_float(r[2], 0.0):g}",
            "note": str(r[3] or ""),
            "account": str(r[4] or "Main"),
        }
        for r in rows
        if normalize_ticker(str(r[0] or ""))
    ]
    return out


def _bootstrap_portfolio_positions_pg(cur: Any) -> int:
    inserted = 0
    # 1) Preferred source of truth: canonical portfolio.csv (local or GCS-backed cloud_files).
    for r in _read_local_portfolio_rows():
        t = normalize_ticker(str(r.get("ticker") or ""))
        sh = _to_float(r.get("shares"), 0.0)
        if not t or sh <= 0:
            continue
        acct = str(r.get("account") or "Main").strip() or "Main"
        cur.execute(
            """
            INSERT INTO portfolio_positions_core (ticker, shares, cost, note, account, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker, account)
            DO UPDATE SET
                shares = EXCLUDED.shares,
                cost = EXCLUDED.cost,
                note = EXCLUDED.note,
                updated_at = NOW()
            """,
            (t, sh, _to_float(r.get("cost"), 0.0), str(r.get("note") or ""), acct),
        )
        inserted += 1
    if inserted > 0:
        return inserted

    # 2) Last-resort strict-cloud fallback: infer from transactions only when no canonical file exists.
    if not strict_postgres_mode():
        return 0
    try:
        cur.execute(
            """
            SELECT
                UPPER(TRIM(COALESCE(ticker, ''))) AS ticker,
                SUM(
                    CASE
                        WHEN LOWER(COALESCE(action, '')) IN ('sell','trim','reduce','exit')
                            THEN -ABS(COALESCE(shares, 0))
                        ELSE ABS(COALESCE(shares, 0))
                    END
                ) AS net_shares,
                SUM(
                    CASE
                        WHEN LOWER(COALESCE(action, '')) IN ('sell','trim','reduce','exit')
                            THEN 0
                        ELSE ABS(COALESCE(shares, 0)) * COALESCE(price, 0)
                    END
                ) AS buy_notional,
                SUM(
                    CASE
                        WHEN LOWER(COALESCE(action, '')) IN ('sell','trim','reduce','exit')
                            THEN 0
                        ELSE ABS(COALESCE(shares, 0))
                    END
                ) AS buy_shares,
                MAX(COALESCE(note, '')) AS note
            FROM portfolio_transactions_core
            WHERE COALESCE(ticker, '') <> ''
            GROUP BY UPPER(TRIM(COALESCE(ticker, '')))
            """
        )
        for tk, net_shares, buy_notional, buy_shares, note in (cur.fetchall() or []):
            t = normalize_ticker(str(tk or ""))
            sh = _to_float(net_shares, 0.0)
            if not t or sh <= 1e-6:
                continue
            b_not = _to_float(buy_notional, 0.0)
            b_sh = _to_float(buy_shares, 0.0)
            cost = (b_not / b_sh) if b_sh > 0 else 0.0
            cur.execute(
                """
                INSERT INTO portfolio_positions_core (ticker, shares, cost, note, account, updated_at)
                VALUES (%s, %s, %s, %s, 'Main', NOW())
                ON CONFLICT (ticker, account)
                DO UPDATE SET
                    shares = EXCLUDED.shares,
                    cost = EXCLUDED.cost,
                    note = EXCLUDED.note,
                    updated_at = NOW()
                """,
                (t, sh, cost, str(note or "")),
            )
            inserted += 1
    except Exception:
        pass
    return inserted


def _read_local_portfolio_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/portfolio.csv")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for ln in str(txt).splitlines():
        parts = [x.strip() for x in ln.split(",")]
        tk = normalize_ticker(parts[0] if parts else "")
        if not tk:
            continue
        out.append(
            {
                "ticker": tk,
                "shares": parts[1] if len(parts) >= 2 else "",
                "cost": parts[2] if len(parts) >= 3 else "",
                "note": parts[3] if len(parts) >= 4 else "",
            }
        )
    return out


def _read_local_cash_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/cash_balances.csv")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for i, ln in enumerate(str(txt).splitlines()):
        s = str(ln or "").strip()
        if not s:
            continue
        if i == 0 and "currency" in s.lower() and "amount" in s.lower():
            continue
        parts = [x.strip() for x in s.split(",")]
        ccy = str(parts[0] if parts else "USD").strip().upper() or "USD"
        out.append({"currency": ccy, "amount": str(_to_float(parts[1] if len(parts) >= 2 else 0.0, 0.0))})
    return out


def _read_local_watchlist_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/my_watchlist.txt")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for ln in str(txt).splitlines():
        s = str(ln or "").strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split(",")]
        tk = normalize_ticker(parts[0] if parts else "")
        if not tk:
            continue
        out.append(
            {
                "ticker": tk,
                "added_at": parts[1] if len(parts) >= 2 else "",
                "reason": parts[2] if len(parts) >= 3 else "",
                "category": parts[3] if len(parts) >= 4 else "",
            }
        )
    return out


def _mirror_portfolio_to_file(rows: list[dict[str, str]]) -> None:
    lines: list[str] = []
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        sh = _to_float(r.get("shares"), 0.0)
        cost = _to_float(r.get("cost"), 0.0)
        note = str(r.get("note") or "").replace("\n", " ").strip()
        lines.append(",".join([tk, f"{sh:g}", f"{cost:g}", note]))
    cloud_files.write_text("data/portfolio.csv", "\n".join(lines) + ("\n" if lines else ""))


def _mirror_cash_to_file(rows: list[dict[str, str]]) -> None:
    lines = ["currency,amount"]
    for r in rows:
        ccy = str(r.get("currency") or "USD").strip().upper() or "USD"
        amt = _to_float(r.get("amount"), 0.0)
        lines.append(f"{ccy},{amt:g}")
    cloud_files.write_text("data/cash_balances.csv", "\n".join(lines) + "\n")


def _mirror_watchlist_to_file(rows: list[dict[str, str]]) -> None:
    lines = ["# TICKER,ADDED_AT,REASON,CATEGORY"]
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        added = str(r.get("added_at") or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reason = str(r.get("reason") or "").replace("\n", " ").strip()
        category = str(r.get("category") or "").replace("\n", " ").replace(",", " ").strip()
        lines.append(",".join([tk, added, reason, category]))
    cloud_files.write_text("data/my_watchlist.txt", "\n".join(lines) + "\n")


def read_portfolio_rows_state() -> list[dict[str, str]]:
    if not strict_postgres_mode():
        local_rows = _read_local_portfolio_rows()
        if local_rows:
            return local_rows
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    try:
        cur = con.cursor()
        out = _read_positions_rows_pg(cur)
        if not out:
            inserted = _bootstrap_portfolio_positions_pg(cur)
            if inserted > 0:
                con.commit()
                out = _read_positions_rows_pg(cur)
            else:
                try:
                    con.rollback()
                except Exception:
                    pass
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    finally:
        con.close()


def write_portfolio_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        acct = str(r.get("account") or "Main").strip() or "Main"
        key = (tk, acct)
        if not tk or key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "ticker": tk,
                "shares": f"{_to_float(r.get('shares'), 0.0):g}",
                "cost": f"{_to_float(r.get('cost'), 0.0):g}",
                "note": str(r.get("note") or "").replace("\n", " ").strip(),
                "account": acct,
            }
        )
    if not strict_postgres_mode():
        _mirror_portfolio_to_file(cleaned)
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM portfolio_positions_core")
        for r in cleaned:
            cur.execute(
                """
                INSERT INTO portfolio_positions_core (ticker, shares, cost, note, account, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (r["ticker"], _to_float(r["shares"], 0.0), _to_float(r["cost"], 0.0), r["note"], r["account"]),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def read_cash_rows_state() -> list[dict[str, str]]:
    if not strict_postgres_mode():
        local_rows = _read_local_cash_rows()
        if local_rows:
            return local_rows
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    try:
        cur = con.cursor()
        cur.execute("SELECT currency, amount FROM cash_balances_core ORDER BY currency ASC")
        rows = cur.fetchall() or []
        out = [{"currency": str(r[0] or "USD").upper(), "amount": f"{_to_float(r[1], 0.0):g}"} for r in rows]
        if not out:
            for r in _read_local_cash_rows():
                ccy = str(r.get("currency") or "USD").strip().upper() or "USD"
                cur.execute(
                    """
                    INSERT INTO cash_balances_core (currency, amount, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (currency) DO UPDATE SET amount = EXCLUDED.amount, updated_at = NOW()
                    """,
                    (ccy, _to_float(r.get("amount"), 0.0)),
                )
            con.commit()
            cur.execute("SELECT currency, amount FROM cash_balances_core ORDER BY currency ASC")
            rows = cur.fetchall() or []
            out = [{"currency": str(r[0] or "USD").upper(), "amount": f"{_to_float(r[1], 0.0):g}"} for r in rows]
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    finally:
        con.close()


def write_cash_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        ccy = str(r.get("currency") or "USD").strip().upper() or "USD"
        if ccy in seen:
            continue
        seen.add(ccy)
        cleaned.append({"currency": ccy, "amount": f"{_to_float(r.get('amount'), 0.0):g}"})
    if not strict_postgres_mode():
        _mirror_cash_to_file(cleaned)
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM cash_balances_core")
        for r in cleaned:
            cur.execute(
                "INSERT INTO cash_balances_core (currency, amount, updated_at) VALUES (%s, %s, NOW())",
                (r["currency"], _to_float(r["amount"], 0.0)),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def read_watchlist_rows_state() -> list[dict[str, str]]:
    if not strict_postgres_mode():
        local_rows = _read_local_watchlist_rows()
        if local_rows:
            return local_rows
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    try:
        cur = con.cursor()
        cur.execute("SELECT ticker, added_at, reason, category FROM watchlist_core ORDER BY ticker ASC")
        rows = cur.fetchall() or []
        out = []
        for r in rows:
            tk = normalize_ticker(str(r[0] or ""))
            if not tk:
                continue
            out.append({"ticker": tk, "added_at": str(r[1] or ""), "reason": str(r[2] or ""), "category": str(r[3] or "")})
        if not out:
            for r in _read_local_watchlist_rows():
                tk = normalize_ticker(str(r.get("ticker") or ""))
                if not tk:
                    continue
                cur.execute(
                    """
                    INSERT INTO watchlist_core (ticker, added_at, reason, category, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (ticker)
                    DO UPDATE SET added_at = EXCLUDED.added_at, reason = EXCLUDED.reason, category = EXCLUDED.category, updated_at = NOW()
                    """,
                    (
                        tk,
                        str(r.get("added_at") or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        str(r.get("reason") or ""),
                        str(r.get("category") or ""),
                    ),
                )
            con.commit()
            cur.execute("SELECT ticker, added_at, reason, category FROM watchlist_core ORDER BY ticker ASC")
            rows = cur.fetchall() or []
            out = []
            for r in rows:
                tk = normalize_ticker(str(r[0] or ""))
                if not tk:
                    continue
                out.append({"ticker": tk, "added_at": str(r[1] or ""), "reason": str(r[2] or ""), "category": str(r[3] or "")})
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    finally:
        con.close()


def write_watchlist_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        cleaned.append(
            {
                "ticker": tk,
                "added_at": str(r.get("added_at") or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "reason": str(r.get("reason") or "").replace("\n", " ").strip(),
                "category": str(r.get("category") or "").replace("\n", " ").strip(),
            }
        )
    if not strict_postgres_mode():
        _mirror_watchlist_to_file(cleaned)
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM watchlist_core")
        for r in cleaned:
            cur.execute(
                "INSERT INTO watchlist_core (ticker, added_at, reason, category, updated_at) VALUES (%s, %s, %s, %s, NOW())",
                (r["ticker"], r["added_at"], r["reason"], r.get("category", "")),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ── Watchlist Groups (color, emoji, order) ─────────────────────────────


def read_watchlist_groups() -> dict[str, dict]:
    """Return {group_name: {color, emoji, sort_order}} for all groups."""
    if not _ensure_state_schema_pg():
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute("SELECT group_name, color, emoji, sort_order FROM watchlist_groups_core ORDER BY sort_order ASC, group_name ASC")
        rows = cur.fetchall() or []
        out: dict[str, dict] = {}
        for r in rows:
            out[str(r[0] or "")] = {
                "color": str(r[1] or ""),
                "emoji": str(r[2] or ""),
                "sort_order": int(r[3] or 0),
            }
        return out
    except Exception:
        return {}
    finally:
        con.close()


def upsert_watchlist_group(group_name: str, color: str = "", emoji: str = "", sort_order: int = 0) -> bool:
    """Create or update a watchlist group's appearance."""
    if not group_name or not _ensure_state_schema_pg():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO watchlist_groups_core (group_name, color, emoji, sort_order, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (group_name)
            DO UPDATE SET color = EXCLUDED.color, emoji = EXCLUDED.emoji,
                          sort_order = EXCLUDED.sort_order, updated_at = NOW()
            """,
            (group_name.strip(), color.strip(), emoji.strip(), sort_order),
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


def delete_watchlist_group(group_name: str) -> bool:
    """Delete a watchlist group (tickers in it become 'General')."""
    if not group_name or not _ensure_state_schema_pg():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("UPDATE watchlist_core SET category = '' WHERE category = %s", (group_name,))
        cur.execute("DELETE FROM watchlist_groups_core WHERE group_name = %s", (group_name,))
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


# ── Portfolio Accounts (multi-account support) ──────────────────────────


def read_portfolio_accounts() -> list[dict]:
    """Return list of portfolio accounts sorted by sort_order."""
    if not _ensure_state_schema_pg():
        return [{"account_name": "Main", "color": "#6366f1", "sort_order": 0}]
    con = pg_connect()
    if con is None:
        return [{"account_name": "Main", "color": "#6366f1", "sort_order": 0}]
    try:
        cur = con.cursor()
        cur.execute("SELECT account_name, color, sort_order FROM portfolio_accounts_core ORDER BY sort_order ASC, account_name ASC")
        rows = cur.fetchall() or []
        out = [
            {"account_name": str(r[0] or ""), "color": str(r[1] or "#6366f1"), "sort_order": int(r[2] or 0)}
            for r in rows
        ]
        return out if out else [{"account_name": "Main", "color": "#6366f1", "sort_order": 0}]
    except Exception:
        return [{"account_name": "Main", "color": "#6366f1", "sort_order": 0}]
    finally:
        con.close()


def upsert_portfolio_account(account_name: str, color: str = "#6366f1", sort_order: int = 0) -> bool:
    """Create or update a portfolio account."""
    if not account_name or not _ensure_state_schema_pg():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO portfolio_accounts_core (account_name, color, sort_order, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (account_name)
            DO UPDATE SET color = EXCLUDED.color, sort_order = EXCLUDED.sort_order, updated_at = NOW()
            """,
            (account_name.strip(), color.strip(), sort_order),
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


def delete_portfolio_account(account_name: str) -> bool:
    """Delete an account and move its positions to 'Main'."""
    if not account_name or account_name.strip() == "Main" or not _ensure_state_schema_pg():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        # Move positions to Main (merge: if Main already has same ticker, add shares)
        cur.execute(
            """
            WITH moving AS (
                DELETE FROM portfolio_positions_core
                WHERE account = %s
                RETURNING ticker, shares, cost, note
            )
            INSERT INTO portfolio_positions_core (ticker, shares, cost, note, account, updated_at)
            SELECT ticker, shares, cost, note, 'Main', NOW() FROM moving
            ON CONFLICT (ticker, account)
            DO UPDATE SET
                shares = portfolio_positions_core.shares + EXCLUDED.shares,
                cost = EXCLUDED.cost,
                updated_at = NOW()
            """,
            (account_name.strip(),),
        )
        cur.execute("DELETE FROM portfolio_accounts_core WHERE account_name = %s", (account_name.strip(),))
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


def rename_portfolio_account(old_name: str, new_name: str) -> bool:
    """Rename a portfolio account (updates positions and account table)."""
    old = str(old_name or "").strip()
    new = str(new_name or "").strip()
    if not old or not new or old == new or not _ensure_state_schema_pg():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE portfolio_positions_core SET account = %s WHERE account = %s",
            (new, old),
        )
        cur.execute(
            "SELECT color, sort_order FROM portfolio_accounts_core WHERE account_name = %s",
            (old,),
        )
        row = cur.fetchone()
        color = str(row[0] or "#6366f1") if row else "#6366f1"
        sort_order = int(row[1] or 0) if row else 0
        cur.execute(
            """
            INSERT INTO portfolio_accounts_core (account_name, color, sort_order, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (account_name)
            DO UPDATE SET color = EXCLUDED.color, sort_order = EXCLUDED.sort_order, updated_at = NOW()
            """,
            (new, color, sort_order),
        )
        cur.execute("DELETE FROM portfolio_accounts_core WHERE account_name = %s", (old,))
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
