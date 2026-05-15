from __future__ import annotations

from typing import Any, Iterator


def iter_cursor_rows(cur: Any, batch_size: int = 256) -> Iterator[Any]:
    size = max(1, int(batch_size or 256))
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            break
        for row in rows:
            yield row
