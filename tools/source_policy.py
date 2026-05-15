#!/usr/bin/env python3
"""
source_policy.py — Shared source mode config for reliability controls.

Modes:
  - strict: primary/official sources only
  - bloomberg_like: broader, mixed-source intelligence
"""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODE_PATH = REPO_ROOT / "data" / "source_mode.json"
DEFAULT_MODE = "bloomberg_like"

SOURCE_MODES = {
    "strict": {
        "description": (
            "Primary sources only: SEC EDGAR, FRED, and official public institutions."
        ),
        "allowed_labels": [
            "SEC EDGAR",
            "FRED",
            "Federal Reserve",
            "ECB",
            "BIS",
            "EIA",
            "IEA",
            "USDA",
            "SEC Press",
        ],
    },
    "bloomberg_like": {
        "description": (
            "Broader market intelligence: official sources plus exchange feeds, "
            "financial data vendors, and curated media."
        ),
        "allowed_labels": ["*"],
    },
}


def load_source_mode():
    """Return active mode string."""
    if not MODE_PATH.exists():
        return DEFAULT_MODE
    try:
        raw = json.loads(MODE_PATH.read_text(encoding="utf-8"))
        mode = raw.get("mode", DEFAULT_MODE)
        if mode not in SOURCE_MODES:
            return DEFAULT_MODE
        return mode
    except Exception:
        return DEFAULT_MODE


def get_mode_config():
    """Return active mode config dict."""
    mode = load_source_mode()
    return {"mode": mode, **SOURCE_MODES[mode]}


def set_source_mode(mode):
    """Set active mode, writing config file."""
    if mode not in SOURCE_MODES:
        raise ValueError(
            f"Unknown mode '{mode}'. Valid modes: {', '.join(sorted(SOURCE_MODES))}"
        )
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "description": SOURCE_MODES[mode]["description"],
    }
    MODE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def source_allowed(label):
    """Check if a named source label is allowed in active mode."""
    mode = load_source_mode()
    allowed = SOURCE_MODES[mode]["allowed_labels"]
    if "*" in allowed:
        return True
    return label in allowed
