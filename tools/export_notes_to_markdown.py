#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path


def _slug(s: str) -> str:
    t = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s or "").strip()).strip("-")
    return t or "note"


def _fetch_notes(base_url: str, limit: int, include_system: bool, bearer: str) -> list[dict]:
    qs = urllib.parse.urlencode(
        {
            "limit": max(1, min(int(limit or 500), 5000)),
            "include_system": 1 if include_system else 0,
        }
    )
    url = f"{base_url.rstrip('/')}/organizer/notes-export?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    ssl_ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    obj = json.loads(raw or "{}")
    rows = obj.get("items") if isinstance(obj, dict) else []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _write_exports(rows: list[dict], out_dir: Path) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    notes_dir = out_dir / "cloud_notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now().isoformat()
    lines = [
        "# Cloud Notes Export",
        "",
        f"- Exported at: `{now}`",
        f"- Notes count: `{len(rows)}`",
        "",
    ]
    for r in rows:
        rid = str(r.get("id") or "")
        table = str(r.get("source_table") or "")
        date = str(r.get("date") or "")
        ticker = str(r.get("ticker") or "")
        tag = str(r.get("tag") or "")
        kind = str(r.get("kind") or "")
        text = str(r.get("text") or "")
        title = text.splitlines()[0][:100] if text else "(empty)"
        file_name = f"{_slug(date[:10] or 'undated')}_{_slug(table)}_{_slug(rid)}.md"
        p = notes_dir / file_name
        p.write_text(
            "\n".join(
                [
                    f"# Note {rid}",
                    "",
                    f"- Source: `{table}`",
                    f"- Date: `{date}`",
                    f"- Ticker: `{ticker}`",
                    f"- Tag: `{tag}`",
                    f"- Kind: `{kind}`",
                    "",
                    "## Content",
                    "",
                    text,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        lines.append(f"- `{date}` [{table}:{rid}] `{ticker}` `{tag}` - {title} -> `cloud_notes/{file_name}`")

    index_path = out_dir / "CLOUD_NOTES_EXPORT.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path, len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Organizer notes to markdown files in Notes/")
    ap.add_argument("--base-url", default="http://127.0.0.1:8085", help="App base URL")
    ap.add_argument("--out-dir", default="Notes", help="Output directory")
    ap.add_argument("--limit", type=int, default=500, help="Max notes to export")
    ap.add_argument("--include-system", action="store_true", help="Include AI/system logs")
    ap.add_argument("--bearer", default=os.getenv("NOTES_EXPORT_BEARER", ""), help="Bearer token for cloud app")
    args = ap.parse_args()

    rows = _fetch_notes(
        base_url=str(args.base_url or "").strip(),
        limit=int(args.limit or 500),
        include_system=bool(args.include_system),
        bearer=str(args.bearer or "").strip(),
    )
    index_path, count = _write_exports(rows, Path(str(args.out_dir or "Notes")))
    print(f"exported={count}")
    print(f"index={index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
