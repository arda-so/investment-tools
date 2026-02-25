# Investor OS v2 (Modular)

This is the new modular architecture track for the full system, built with free/open components:

- FastAPI
- Jinja2
- SQLModel
- SQLite

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8766 --reload
```

Open: `http://127.0.0.1:8766/organizer`

## Scope (current)

- Organizer module migrated as first vertical slice.
- Uses existing `data/core.db`.
- Current monolith remains untouched on `:8765`.

