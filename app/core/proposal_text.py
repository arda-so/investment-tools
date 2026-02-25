from __future__ import annotations

import re


def is_ai_task_text(task: str, category: str = "") -> bool:
    s = str(task or "").strip().lower()
    cat = str(category or "").strip().lower()
    return (
        "[active_proposal]" in s
        or "[proposal " in s
        or "review and validate proposal" in s
        or "proposal" in s
        or cat == "ai"
    )


def clean_task_text(text: str) -> str:
    s = str(text or "").strip()
    s = re.sub(r"\s*\[active_proposal\]\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*\[proposal\s+\d+\]\s*$", "", s, flags=re.I)
    return s.strip()


def is_system_log_text(tag: str = "", text: str = "", title: str = "", created_by: str = "human") -> bool:
    cb = str(created_by or "human").strip().lower()
    if cb != "human":
        return True
    t1 = str(tag or "").strip().lower()
    t2 = str(text or "").strip().lower()
    t3 = str(title or "").strip().lower()
    return ("proposal" in t1) or ("proposal" in t2) or ("proposal" in t3)
