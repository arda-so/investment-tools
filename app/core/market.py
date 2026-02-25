from __future__ import annotations

import os

from app.core.config import app_env


def finnhub_key() -> str:
    return (
        os.getenv("FINNHUB_API_KEY", "")
        or os.getenv("FINNHUB_TOKEN", "")
        or app_env("FINNHUB_API_KEY", "")
        or app_env("FINNHUB_TOKEN", "")
    ).strip()
