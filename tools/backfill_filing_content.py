from __future__ import annotations

import argparse
from typing import Any

from app.core.filing_text import read_filing_text_any
from app.services.postgres_core_service import core_backend, pg_connect


def _fetch_rows(limit: int, only_empty: bool) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        where = "WHERE COALESCE(content,'')=''" if only_empty else ""
        cur.execute(
            f"""
            SELECT id, ticker, form, date, accession, path, doc_url, content
            FROM filings_core
            {where}
            ORDER BY id ASC
            LIMIT %s
            """,
            (int(limit),),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall() or []:
            out.append(
                {
                    "id": int(r[0] or 0),
                    "ticker": str(r[1] or ""),
                    "form": str(r[2] or ""),
                    "date": str(r[3] or ""),
                    "accession": str(r[4] or ""),
                    "path": str(r[5] or ""),
                    "doc_url": str(r[6] or ""),
                    "content": str(r[7] or ""),
                }
            )
        return out
    finally:
        con.close()


def _update_content(row_id: int, content_excerpt: str) -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE filings_core SET content=%s WHERE id=%s",
            (str(content_excerpt or ""), int(row_id)),
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill filings_core.content excerpts from filing paths (local/GCS)."
    )
    ap.add_argument("--limit", type=int, default=200000, help="Max rows to process.")
    ap.add_argument("--max-chars", type=int, default=15000, help="Excerpt size for content field.")
    ap.add_argument("--only-empty", action="store_true", help="Only rows with empty content.")
    ap.add_argument("--dry-run", action="store_true", help="Do not write DB updates.")
    args = ap.parse_args()

    if core_backend() != "postgres":
        print("ERROR: CORE_DB_BACKEND must be postgres.")
        return 2

    rows = _fetch_rows(limit=max(1, int(args.limit)), only_empty=bool(args.only_empty))
    if not rows:
        print("No rows to process.")
        return 0

    processed = 0
    updated = 0
    missing = 0
    failed = 0

    max_chars = max(1000, int(args.max_chars))
    for r in rows:
        processed += 1
        path = str(r.get("path") or "").strip()
        if not path:
            missing += 1
            continue
        txt = read_filing_text_any(path, max_chars=max_chars)
        if not txt:
            missing += 1
            continue
        if args.dry_run:
            updated += 1
            continue
        ok = _update_content(int(r.get("id") or 0), txt[:max_chars])
        if ok:
            updated += 1
        else:
            failed += 1

        if processed % 500 == 0:
            print(
                f"progress processed={processed} updated={updated} missing={missing} failed={failed}"
            )

    print(
        {
            "processed": processed,
            "updated": updated,
            "missing_text": missing,
            "failed_updates": failed,
            "dry_run": bool(args.dry_run),
            "max_chars": max_chars,
        }
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
