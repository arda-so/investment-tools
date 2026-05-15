#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import app


@dataclass
class Case:
    query: str
    expect_intent: str
    expect_status_in: tuple[str, ...] = ("ok",)
    expect_redirect_prefix: str = ""


CASES = [
    Case("show me my notes", "open_notes", ("ok",), "/organizer/file/notes"),
    Case("open company adbe", "open_company", ("ok",), "/company_file?t="),
    Case("add note buy Apple", "add_note_draft", ("ok",), "/organizer"),
    Case("add task read crm 10-q tomorrow", "add_task", ("ok",), "/organizer"),
    Case("summarize latest report", "summarize_latest_report", ("ok", "needs_confirmation"), "/reports/view"),
]


def main() -> int:
    c = TestClient(app)
    failures: list[str] = []
    for case in CASES:
        r = c.post("/ai/command", json={"query": case.query})
        if r.status_code != 200:
            failures.append(f"{case.query!r}: HTTP {r.status_code}")
            continue
        data = r.json()
        intent = str(data.get("intent") or "")
        status = str(data.get("status") or "")
        redirect = str(data.get("redirect_url") or "")
        if intent != case.expect_intent:
            failures.append(f"{case.query!r}: intent={intent!r} expected={case.expect_intent!r}")
        if status not in case.expect_status_in:
            failures.append(f"{case.query!r}: status={status!r} expected one of {case.expect_status_in!r}")
        if case.expect_redirect_prefix and not redirect.startswith(case.expect_redirect_prefix):
            failures.append(
                f"{case.query!r}: redirect={redirect!r} expected prefix {case.expect_redirect_prefix!r}"
            )
        print(f"OK: {case.query} -> {intent} [{status}] {redirect}")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("-", f)
        return 1
    print("\nAll benchmark checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
