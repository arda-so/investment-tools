from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CORE_DB_PATH = DATA_DIR / "core.db"

_ENV_LOADED = False


def _load_local_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_name = str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev"))).strip().lower()
    non_dev = env_name in {"prod", "production", "staging"}
    allow_local_env = str(os.getenv("ALLOW_LOCAL_ENV_FILE", "0" if non_dev else "1")).strip().lower() in {"1", "true", "yes", "on"}
    if not allow_local_env:
        return
    p = ROOT / ".env"
    if not p.exists():
        return
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = str(ln or "").strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            key = str(k or "").strip()
            if not key:
                continue
            val = str(v or "").strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val
    except Exception:
        return


_load_local_env()


def app_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default)).strip()


def runtime_env_name() -> str:
    return str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev"))).strip().lower()


def is_non_dev_env() -> bool:
    return runtime_env_name() in {"prod", "production", "staging"}


def allow_local_files() -> bool:
    default = "0" if is_non_dev_env() else "1"
    return str(os.getenv("ALLOW_LOCAL_FILES", default)).strip().lower() in {"1", "true", "yes", "on"}
