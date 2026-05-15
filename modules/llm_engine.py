#!/usr/bin/env python3
from __future__ import annotations

"""
Compatibility shim for module analyzers.

Single source of truth for AI routing/model config is tools/llm_engine.py.
This wrapper preserves the historical call signature used in modules:
    ask_ai(system_prompt, user_text, mode="smart", json_mode=False)
"""

from tools.llm_engine import ask_ai as _unified_ask_ai


def ask_ai(system_prompt: str, user_text: str, mode: str = "smart", json_mode: bool = False) -> str:
    """
    Backward-compatible wrapper.

    tools.llm_engine.ask_ai expects (prompt, context, ...), while legacy module
    calls pass (system_prompt, user_text, ...). We translate here.
    """
    return _unified_ask_ai(prompt=user_text, context=system_prompt, mode=mode, json_mode=json_mode)

