from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from app.services.postgres_core_service import pg_connect
try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]

def ensure_user_preferences_schema() -> None:
    con_pg = pg_connect()
    if con_pg is None:
        return
    try:
        cur = con_pg.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS user_preferences_core (
                id BIGSERIAL PRIMARY KEY,
                pref_key TEXT NOT NULL UNIQUE,
                pref_value TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat',
                preference_key TEXT NOT NULL DEFAULT '',
                preference_value TEXT NOT NULL DEFAULT '',
                context_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        cur.execute(
            "UPDATE user_preferences_core SET preference_key = pref_key, preference_value = pref_value "
            "WHERE COALESCE(preference_key,'') = ''"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_pref_core_updated ON user_preferences_core(updated_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_pref_core_key_v2 ON user_preferences_core(preference_key)")
        con_pg.commit()
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
    finally:
        con_pg.close()


def upsert_user_preference(pref_key: str, pref_value: str, source: str = "chat") -> bool:
    k = str(pref_key or "").strip().lower()[:80]
    v = str(pref_value or "").strip()[:1000]
    s = str(source or "chat").strip()[:40]
    if not k or not v:
        return False
    now = dt.datetime.now().isoformat()
    con_pg = pg_connect()
    if con_pg is None:
        return False
    try:
        cur = con_pg.cursor()
        cur.execute(
            """
            INSERT INTO user_preferences_core
            (pref_key, pref_value, source, preference_key, preference_value, context_reason, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(pref_key) DO UPDATE SET
              pref_value=EXCLUDED.pref_value,source=EXCLUDED.source,preference_key=EXCLUDED.preference_key,
              preference_value=EXCLUDED.preference_value,updated_at=EXCLUDED.updated_at
            """,
            (k, v, s, k, v, "", now, now),
        )
        con_pg.commit()
        return True
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return False
    finally:
        con_pg.close()


def list_user_preferences(limit: int = 40) -> list[dict[str, str]]:
    ensure_user_preferences_schema()
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT pref_key, pref_value, preference_key, preference_value, source, updated_at
               FROM user_preferences_core
               ORDER BY updated_at DESC
               LIMIT %s""",
            (max(1, min(200, int(limit or 40))),),
        )
        rows = cur.fetchall() or []
        return [
            {
                "key": str((r[2] or r[0] or "")).strip(),
                "value": str((r[3] or r[1] or "")).strip(),
                "source": str(r[4] or "").strip(),
                "updated_at": str(r[5] or "").strip(),
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        con_pg.close()


def summarize_user_preferences(limit: int = 24) -> str:
    rows = list_user_preferences(limit=limit)
    if not rows:
        return ""
    lines: list[str] = []
    for r in rows:
        k = str(r.get("key") or "").strip()
        v = str(r.get("value") or "").strip()
        if not k or not v:
            continue
        lines.append(f"- {k}: {v}")
    return "\n".join(lines[:limit])


def _normalize_pref_key(key: str) -> str:
    k = re.sub(r"[^a-z0-9_]+", "_", str(key or "").strip().lower())
    k = re.sub(r"_+", "_", k).strip("_")
    return k[:80]


def _learn_preferences_semantic(text: str, history: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if ask_ai is None:
        return []
    q = str(text or "").strip()
    if not q:
        return []
    hist_lines: list[str] = []
    if isinstance(history, list):
        for m in history[-12:]:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "").strip().lower()
            txt = str(m.get("text") or "").strip()
            if role not in {"user", "assistant"} or not txt:
                continue
            hist_lines.append(f"{role.upper()}: {txt[:240]}")
    hist_block = "\n".join(hist_lines) if hist_lines else "(none)"
    prompt = (
        "Extract stable user preferences from the user message, using recent conversation for context.\n"
        "Return strict JSON only: "
        '{"preferences":[{"key":"snake_case","value":"short preference value","reason":"why","confidence":0.0}]}\n'
        "Rules:\n"
        "- Include only durable preferences (style/tone/output/risk/workflow), not one-off requests.\n"
        "- If the user gives feedback to the assistant response (liked/disliked style), convert that into durable preference.\n"
        "- If none, return {\"preferences\":[]}.\n"
        "- confidence is 0..1.\n"
        f"Recent conversation:\n{hist_block}\n\n"
        f"User message: {q}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Preference extractor. JSON only.",
                mode="fast",
                json_mode=True,
                temperature=0.0,
            )
            or ""
        ).strip()
        if not raw:
            return []
        obj = json.loads(raw)
        prefs = obj.get("preferences") if isinstance(obj, dict) else []
        if not isinstance(prefs, list):
            return []
        saved: list[dict[str, Any]] = []
        for p in prefs[:8]:
            if not isinstance(p, dict):
                continue
            conf = float(p.get("confidence") or 0.0)
            if conf < 0.72:
                continue
            k = _normalize_pref_key(str(p.get("key") or ""))
            v = str(p.get("value") or "").strip()[:1000]
            reason = str(p.get("reason") or "").strip()[:240]
            if not k or not v:
                continue
            if upsert_user_preference(k, v, source="chat_semantic"):
                saved.append({"key": k, "value": v, "reason": reason, "confidence": round(conf, 3)})
        return saved
    except Exception:
        return []


def learn_preferences_from_trajectory(history: list[dict[str, Any]] | None, latest_user_text: str = "") -> list[dict[str, Any]]:
    """Infer durable preferences from conversation behavior (not only explicit wording)."""
    if ask_ai is None or not isinstance(history, list) or len(history) < 4:
        return []
    lines: list[str] = []
    for m in history[-14:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        txt = str(m.get("text") or "").strip()
        if role not in {"user", "assistant"} or not txt:
            continue
        lines.append(f"{role.upper()}: {txt[:280]}")
    if not lines:
        return []
    prompt = (
        "Infer durable user preferences from this conversation trajectory.\n"
        "Return strict JSON only: "
        '{"preferences":[{"key":"snake_case","value":"short value","reason":"behavioral evidence","confidence":0.0}]}\n'
        "Rules:\n"
        "- Use behavior evidence (retries, corrections, ignored paths), not just explicit statements.\n"
        "- Only durable preferences (style/flow/interaction), no one-off requests.\n"
        "- If none, return {\"preferences\":[]}.\n"
        f"Trajectory:\n{chr(10).join(lines)}\n\nLatest user message: {str(latest_user_text or '').strip()}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Trajectory preference extractor. JSON only.",
                mode="fast",
                json_mode=True,
                temperature=0.0,
            )
            or ""
        ).strip()
        if not raw:
            return []
        obj = json.loads(raw)
        prefs = obj.get("preferences") if isinstance(obj, dict) else []
        if not isinstance(prefs, list):
            return []
        out: list[dict[str, Any]] = []
        for p in prefs[:6]:
            if not isinstance(p, dict):
                continue
            conf = float(p.get("confidence") or 0.0)
            if conf < 0.78:
                continue
            k = _normalize_pref_key(str(p.get("key") or ""))
            v = str(p.get("value") or "").strip()[:1000]
            reason = str(p.get("reason") or "").strip()[:240]
            if not k or not v:
                continue
            if upsert_user_preference(k, v, source="chat_trajectory"):
                out.append({"key": k, "value": v, "reason": reason, "confidence": round(conf, 3)})
        return out
    except Exception:
        return []


def learn_preferences_from_text(text: str, history: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    ensure_user_preferences_schema()
    q = str(text or "").strip()
    if not q:
        return []
    # Model-first semantic extraction only (no phrase hardcoding).
    return _learn_preferences_semantic(q, history=history)
