from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.core.config import ROOT
from app.core.ticker import normalize_ticker as _normalize_ticker

SEC_SYNC_STATE_PATH = ROOT / "data" / "sec_sync_state.json"


def load_sec_sync_state() -> dict[str, dict[str, str]]:
    try:
        if not SEC_SYNC_STATE_PATH.exists():
            return {}
        obj = json.loads(SEC_SYNC_STATE_PATH.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(obj, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for k, v in obj.items():
            t = _normalize_ticker(k)
            if not t or not isinstance(v, dict):
                continue
            out[t] = {
                "running": str(v.get("running") or "0"),
                "last": str(v.get("last") or ""),
                "result": str(v.get("result") or ""),
                "message": str(v.get("message") or ""),
            }
        return out
    except Exception:
        return {}


def write_sec_sync_state(state: dict[str, dict[str, str]]) -> None:
    SEC_SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(SEC_SYNC_STATE_PATH.parent), encoding="utf-8") as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(SEC_SYNC_STATE_PATH)


def set_ticker_sync_state(ticker: str, values: dict[str, str]) -> dict[str, dict[str, str]]:
    t = _normalize_ticker(ticker)
    if not t:
        return load_sec_sync_state()
    state = load_sec_sync_state()
    cur = state.get(t, {"running": "0", "last": "", "result": "", "message": ""})
    cur.update({k: str(v) for k, v in values.items()})
    state[t] = cur
    write_sec_sync_state(state)
    return state
