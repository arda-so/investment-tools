from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from app.core.entity_quality import is_valid_company_entity_name
from app.core.ontology_math import effective_confidence as _effective_confidence
from app.services.postgres_core_service import core_backend, pg_connect
try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]

ANTI_REL_TYPES = {"HEDGES_AGAINST", "IMMUNE_TO"}


def _postgres_required() -> bool:
    return core_backend() == "postgres"


def _fetch_company_rows() -> list[dict[str, Any]]:
    if not _postgres_required():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, name, normalized_name
            FROM entities_core
            WHERE type='COMPANY'
            """
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall() or []:
            out.append(
                {
                    "id": int(r[0] or 0),
                    "name": str(r[1] or "").strip(),
                    "normalized_name": str(r[2] or "").strip(),
                }
            )
        return out
    finally:
        con.close()


def list_noisy_company_entities(limit: int = 5000) -> list[dict[str, Any]]:
    rows = _fetch_company_rows()
    noisy = [r for r in rows if not is_valid_company_entity_name(str(r.get("name") or ""))]
    noisy.sort(key=lambda x: int(x.get("id") or 0), reverse=True)
    return noisy[: max(1, min(20000, int(limit or 5000)))]


def audit_ontology_quality(limit_top: int = 25) -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        cur.execute("SELECT type, COUNT(*) FROM entities_core GROUP BY type ORDER BY COUNT(*) DESC")
        entities_by_type = {str(r[0] or ""): int(r[1] or 0) for r in (cur.fetchall() or [])}
        cur.execute("SELECT COUNT(*) FROM relationships_core")
        rel_total = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            """
            SELECT COUNT(*)
            FROM entities_core e
            WHERE e.type='COMPANY'
              AND NOT EXISTS (
                SELECT 1
                FROM relationships_core r
                WHERE r.source_id = e.id OR r.target_id = e.id
              )
            """
        )
        orphan_company = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            """
            SELECT COUNT(*)
            FROM relationships_core
            WHERE COALESCE(NULLIF(effective_confidence, 0), confidence_score, confidence, 0.0) < 0.5
            """
        )
        low_conf = int((cur.fetchone() or [0])[0] or 0)
    finally:
        con.close()

    noisy = list_noisy_company_entities(limit=max(50, int(limit_top or 25) * 20))
    company_total = int(entities_by_type.get("COMPANY", 0))
    noisy_count = len(noisy)
    noisy_ratio = (float(noisy_count) / float(company_total)) if company_total > 0 else 0.0
    top_noisy = noisy[: max(1, min(500, int(limit_top or 25)))]
    return {
        "ok": True,
        "asof": dt.datetime.now().isoformat(timespec="seconds"),
        "entities_by_type": entities_by_type,
        "relationship_total": rel_total,
        "company_total": company_total,
        "noisy_company_count": noisy_count,
        "noisy_company_ratio": round(noisy_ratio, 6),
        "orphan_company_count": orphan_company,
        "low_confidence_relationship_count": low_conf,
        "top_noisy_companies": top_noisy,
    }


def cleanup_noisy_company_entities(*, apply: bool = False, limit: int = 20000) -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    noisy = list_noisy_company_entities(limit=limit)
    ids = [int(r.get("id") or 0) for r in noisy if int(r.get("id") or 0) > 0]
    if not ids:
        return {
            "ok": True,
            "apply": bool(apply),
            "candidate_count": 0,
            "candidate_ids": [],
            "deleted_relationships": 0,
            "deleted_entities": 0,
            "sample": [],
        }

    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        marks = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT COUNT(*) FROM relationships_core WHERE source_id IN ({marks}) OR target_id IN ({marks})",
            tuple(ids + ids),
        )
        rel_would_delete = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            f"SELECT COUNT(*) FROM entities_core WHERE id IN ({marks}) AND type='COMPANY'",
            tuple(ids),
        )
        ent_would_delete = int((cur.fetchone() or [0])[0] or 0)

        if not apply:
            return {
                "ok": True,
                "apply": False,
                "candidate_count": len(ids),
                "candidate_ids": ids[:120],
                "would_delete_relationships": rel_would_delete,
                "would_delete_entities": ent_would_delete,
                "sample": noisy[:25],
            }

        cur.execute(
            f"DELETE FROM relationships_core WHERE source_id IN ({marks}) OR target_id IN ({marks})",
            tuple(ids + ids),
        )
        deleted_rel = int(cur.rowcount or 0)
        cur.execute(
            f"DELETE FROM entities_core WHERE id IN ({marks}) AND type='COMPANY'",
            tuple(ids),
        )
        deleted_ent = int(cur.rowcount or 0)
        con.commit()
        return {
            "ok": True,
            "apply": True,
            "candidate_count": len(ids),
            "candidate_ids": ids[:120],
            "deleted_relationships": deleted_rel,
            "deleted_entities": deleted_ent,
            "sample": noisy[:25],
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def _parse_iso_or_now(raw: str) -> dt.datetime:
    s = str(raw or "").strip()
    if not s:
        return dt.datetime.now()
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return dt.datetime.now()


def _decay_confidence(base_conf: float, last_verified_at: str, half_life_days: int) -> float:
    return _effective_confidence(base_conf, last_verified_at, half_life_days)


def _stale_days_threshold() -> int:
    try:
        return max(30, int(str(os.getenv("ONTOLOGY_ANTI_REVERIFY_STALE_DAYS", "180")).strip() or "180"))
    except Exception:
        return 180


def _min_conf_threshold() -> float:
    try:
        v = float(str(os.getenv("ONTOLOGY_ANTI_REVERIFY_MIN_CONFIDENCE", "0.65")).strip() or "0.65")
    except Exception:
        v = 0.65
    return max(0.1, min(0.99, v))


def _retire_conf_threshold() -> float:
    try:
        v = float(str(os.getenv("ONTOLOGY_ANTI_REVERIFY_RETIRE_CONFIDENCE", "0.35")).strip() or "0.35")
    except Exception:
        v = 0.35
    return max(0.05, min(0.9, v))


def _max_attempts() -> int:
    try:
        return max(1, int(str(os.getenv("ONTOLOGY_ANTI_REVERIFY_MAX_ATTEMPTS", "3")).strip() or "3"))
    except Exception:
        return 3


def _recent_days() -> int:
    try:
        return max(7, int(str(os.getenv("ONTOLOGY_ANTI_REVERIFY_RECENT_DAYS", "120")).strip() or "120"))
    except Exception:
        return 120


def anti_reverify_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_ANTI_REVERIFY_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def ensure_anti_reverify_schema() -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS anti_relationship_reverify_queue_core (
                id BIGINT PRIMARY KEY,
                relationship_id BIGINT NOT NULL UNIQUE,
                relationship_type TEXT NOT NULL DEFAULT '',
                source_id BIGINT NOT NULL DEFAULT 0,
                target_id BIGINT NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                priority_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_arrq_core_status_priority ON anti_relationship_reverify_queue_core(status, priority_score DESC, id ASC)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_arrq_core_rel ON anti_relationship_reverify_queue_core(relationship_id)")
        con.commit()
        return {"ok": True}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def enqueue_stale_anti_relationships(*, apply: bool = False, limit: int = 200000) -> dict[str, Any]:
    if not anti_reverify_enabled():
        return {"ok": True, "enabled": False, "apply": bool(apply), "candidates": 0, "queued": 0, "sample": []}
    st = ensure_anti_reverify_schema()
    if not bool(st.get("ok")):
        return st
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(2000000, int(limit or 200000)))
    stale_days = _stale_days_threshold()
    min_conf = _min_conf_threshold()
    rel_types = tuple(sorted(ANTI_REL_TYPES))
    sample: list[dict[str, Any]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, relationship_type, source_id, target_id,
                   COALESCE(last_verified_at,'') AS last_verified_at,
                   COALESCE(effective_confidence, confidence_score, confidence, 0.0) AS eff_conf
            FROM relationships_core
            WHERE relationship_type = ANY(%s)
              AND COALESCE(status, 'active')='active'
            ORDER BY id ASC
            LIMIT %s
            """,
            (list(rel_types), lim),
        )
        rows = cur.fetchall() or []
        candidates: list[tuple[int, str, int, int, str, float, str, float]] = []
        now = dt.datetime.now()
        for r in rows:
            rid = int(r[0] or 0)
            rtype = str(r[1] or "").strip().upper()
            sid = int(r[2] or 0)
            tid = int(r[3] or 0)
            lv = str(r[4] or "").strip()
            eff = float(r[5] or 0.0)
            age = 99999.0
            if lv:
                try:
                    age = max(0.0, (now - dt.datetime.fromisoformat(lv)).total_seconds() / 86400.0)
                except Exception:
                    age = 99999.0
            stale = age >= float(stale_days)
            weak = eff <= float(min_conf)
            if not (stale or weak):
                continue
            reason_bits: list[str] = []
            if stale:
                reason_bits.append(f"stale_{int(age)}d")
            if weak:
                reason_bits.append(f"low_conf_{round(eff, 4)}")
            reason = ",".join(reason_bits)[:180]
            priority = float(age / max(1.0, float(stale_days))) + max(0.0, float(min_conf) - eff)
            candidates.append((rid, rtype, sid, tid, reason, priority, lv, eff))

        if not apply:
            for c in candidates[:25]:
                sample.append(
                    {
                        "relationship_id": int(c[0]),
                        "relationship_type": c[1],
                        "source_id": int(c[2]),
                        "target_id": int(c[3]),
                        "reason": c[4],
                        "priority_score": round(float(c[5]), 6),
                        "last_verified_at": c[6],
                        "effective_confidence": round(float(c[7]), 6),
                    }
                )
            return {
                "ok": True,
                "enabled": True,
                "apply": False,
                "scanned": len(rows),
                "candidates": len(candidates),
                "queued": 0,
                "stale_days_threshold": stale_days,
                "min_conf_threshold": min_conf,
                "sample": sample,
            }

        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        payload = [
            (int(c[0]), c[1], int(c[2]), int(c[3]), c[4], float(c[5]), now_iso, now_iso)
            for c in candidates
        ]
        queued = 0
        if payload:
            cur.executemany(
                """
                INSERT INTO anti_relationship_reverify_queue_core
                (id, relationship_id, relationship_type, source_id, target_id, reason, priority_score, status, created_at, updated_at)
                VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM anti_relationship_reverify_queue_core), %s,%s,%s,%s,%s,%s,'pending',%s,%s)
                ON CONFLICT(relationship_id) DO UPDATE SET
                  relationship_type=EXCLUDED.relationship_type,
                  source_id=EXCLUDED.source_id,
                  target_id=EXCLUDED.target_id,
                  reason=EXCLUDED.reason,
                  priority_score=GREATEST(anti_relationship_reverify_queue_core.priority_score, EXCLUDED.priority_score),
                  updated_at=EXCLUDED.updated_at
                """,
                payload,
            )
        for c in candidates[:25]:
            if len(sample) < 25:
                sample.append(
                    {
                        "relationship_id": int(c[0]),
                        "relationship_type": c[1],
                        "source_id": int(c[2]),
                        "target_id": int(c[3]),
                        "reason": c[4],
                        "priority_score": round(float(c[5]), 6),
                    }
                )
        con.commit()
        queued = max(0, int(cur.rowcount or 0)) if payload else 0
        return {
            "ok": True,
            "enabled": True,
            "apply": True,
            "scanned": len(rows),
            "candidates": len(candidates),
            "queued": queued,
            "stale_days_threshold": stale_days,
            "min_conf_threshold": min_conf,
            "sample": sample,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def _evidence_patterns(rel_type: str) -> list[str]:
    rt = str(rel_type or "").strip().upper()
    if rt == "HEDGES_AGAINST":
        return ["%hedg%", "%mitigat%", "%offset%", "%protection%", "%insulat%"]
    if rt == "IMMUNE_TO":
        return ["%immune%", "%insulat%", "%resilien%", "%unaffected%", "%limited impact%"]
    return ["%risk%", "%exposure%"]


def _recent_support_hits(cur: Any, tickers: list[str], patterns: list[str], recent_days: int) -> int:
    tks = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    if not tks:
        return 0
    pats = [str(p or "").strip() for p in patterns if str(p or "").strip()]
    if not pats:
        return 0
    since = (dt.datetime.now() - dt.timedelta(days=max(1, int(recent_days or 120)))).date().isoformat()
    where_like = " OR ".join(["fact_text ILIKE %s"] * len(pats))
    sql = (
        "SELECT COUNT(*) FROM report_facts_core "
        "WHERE ticker = ANY(%s) "
        "AND ((COALESCE(fact_date,'') <> '' AND fact_date >= %s) OR (COALESCE(fact_date,'') = '' AND COALESCE(created_at,'') >= %s)) "
        f"AND ({where_like})"
    )
    cur.execute(sql, (tks, since, since, *pats))
    return int((cur.fetchone() or [0])[0] or 0)


def process_anti_reverify_queue(*, apply: bool = False, limit: int = 250) -> dict[str, Any]:
    if not anti_reverify_enabled():
        return {"ok": True, "enabled": False, "apply": bool(apply), "selected": 0, "resolved": 0, "retired": 0, "review": 0}
    st = ensure_anti_reverify_schema()
    if not bool(st.get("ok")):
        return st
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(5000, int(limit or 250)))
    stale_days = _stale_days_threshold()
    min_conf = _min_conf_threshold()
    retire_conf = _retire_conf_threshold()
    max_attempts = _max_attempts()
    recent_days = _recent_days()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, relationship_id, relationship_type, source_id, target_id, reason, priority_score, attempts
            FROM anti_relationship_reverify_queue_core
            WHERE status='pending'
            ORDER BY priority_score DESC, id ASC
            LIMIT %s
            """,
            (lim,),
        )
        queue_rows = cur.fetchall() or []
        resolved = 0
        retired = 0
        review = 0
        errored = 0
        sample: list[dict[str, Any]] = []
        now_iso = dt.datetime.now().isoformat(timespec="seconds")

        for qr in queue_rows:
            qid = int(qr[0] or 0)
            rid = int(qr[1] or 0)
            rtype = str(qr[2] or "").strip().upper()
            attempts = int(qr[7] or 0)
            cur.execute(
                """
                SELECT r.id, r.relationship_type, r.source_id, r.target_id,
                       COALESCE(r.last_verified_at,'') AS last_verified_at,
                       COALESCE(r.decay_half_life_days,365) AS half_life,
                       COALESCE(r.confidence_score, r.confidence, 0.0) AS base_conf,
                       COALESCE(r.effective_confidence, r.confidence_score, r.confidence, 0.0) AS eff_conf,
                       COALESCE(r.status,'active') AS rel_status,
                       COALESCE(es.metadata_json->>'ticker','') AS src_tk,
                       COALESCE(et.metadata_json->>'ticker','') AS tgt_tk,
                       COALESCE(es.normalized_name,'') AS src_norm,
                       COALESCE(et.normalized_name,'') AS tgt_norm
                FROM relationships_core r
                LEFT JOIN entities_core es ON es.id = r.source_id
                LEFT JOIN entities_core et ON et.id = r.target_id
                WHERE r.id=%s
                LIMIT 1
                """,
                (rid,),
            )
            row = cur.fetchone()
            if not row:
                if apply:
                    cur.execute(
                        "UPDATE anti_relationship_reverify_queue_core SET status='error', attempts=%s, error_text=%s, updated_at=%s WHERE id=%s",
                        (attempts + 1, "relationship_missing", now_iso, qid),
                    )
                errored += 1
                continue
            rel_status = str(row[8] or "active").strip().lower()
            if rel_status != "active":
                if apply:
                    cur.execute(
                        "UPDATE anti_relationship_reverify_queue_core SET status='resolved', attempts=%s, error_text='', updated_at=%s WHERE id=%s",
                        (attempts + 1, now_iso, qid),
                    )
                resolved += 1
                continue

            last_verified = str(row[4] or "").strip()
            half_life = int(row[5] or 365)
            base_conf = float(row[6] or 0.0)
            eff_conf = float(row[7] or 0.0)
            src_tk = str(row[9] or "").strip().upper() or str(row[11] or "").strip().upper()
            tgt_tk = str(row[10] or "").strip().upper() or str(row[12] or "").strip().upper()
            tickers = [t for t in [src_tk, tgt_tk] if t]
            pats = _evidence_patterns(rtype)
            hits = _recent_support_hits(cur, tickers, pats, recent_days=recent_days)
            age_days = 99999.0
            if last_verified:
                try:
                    age_days = max(0.0, (dt.datetime.now() - dt.datetime.fromisoformat(last_verified)).total_seconds() / 86400.0)
                except Exception:
                    age_days = 99999.0
            stale = age_days >= float(stale_days)
            weak = eff_conf <= float(min_conf)
            over_attempts = (attempts + 1) >= max_attempts
            should_retire = (hits <= 0) and stale and (eff_conf <= retire_conf) and over_attempts
            should_refresh = hits > 0

            decision = "review"
            if should_refresh:
                decision = "refresh"
            elif should_retire:
                decision = "retire"

            if apply:
                if decision == "refresh":
                    ec_new = _decay_confidence(base_conf, now_iso, max(1, half_life))
                    cur.execute(
                        """
                        UPDATE relationships_core
                        SET last_verified_at=%s,
                            effective_confidence=%s,
                            status='active'
                        WHERE id=%s
                        """,
                        (now_iso, float(ec_new), rid),
                    )
                    cur.execute(
                        """
                        UPDATE anti_relationship_reverify_queue_core
                        SET status='resolved',
                            attempts=%s,
                            error_text='',
                            reason=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (attempts + 1, f"refreshed_hits_{int(hits)}", now_iso, qid),
                    )
                    resolved += 1
                elif decision == "retire":
                    cur.execute(
                        """
                        UPDATE relationships_core
                        SET status='inactive',
                            valid_to=%s
                        WHERE id=%s
                        """,
                        (now_iso, rid),
                    )
                    cur.execute(
                        """
                        UPDATE anti_relationship_reverify_queue_core
                        SET status='retired',
                            attempts=%s,
                            error_text='',
                            reason=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (attempts + 1, "retired_stale_weak_no_recent_evidence", now_iso, qid),
                    )
                    retired += 1
                else:
                    cur.execute(
                        """
                        UPDATE anti_relationship_reverify_queue_core
                        SET status='needs_review',
                            attempts=%s,
                            reason=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (attempts + 1, f"needs_review_hits_{int(hits)}_stale_{int(stale)}_weak_{int(weak)}", now_iso, qid),
                    )
                    review += 1
            else:
                if decision == "refresh":
                    resolved += 1
                elif decision == "retire":
                    retired += 1
                else:
                    review += 1
            if len(sample) < 25:
                sample.append(
                    {
                        "queue_id": qid,
                        "relationship_id": rid,
                        "relationship_type": rtype,
                        "decision": decision,
                        "recent_hits": int(hits),
                        "age_days": round(float(age_days), 2),
                        "effective_confidence": round(float(eff_conf), 6),
                        "attempts_next": attempts + 1,
                    }
                )

        if apply:
            con.commit()
        return {
            "ok": True,
            "enabled": True,
            "apply": bool(apply),
            "selected": len(queue_rows),
            "resolved": resolved,
            "retired": retired,
            "review": review,
            "errored": errored,
            "stale_days_threshold": stale_days,
            "min_conf_threshold": min_conf,
            "retire_conf_threshold": retire_conf,
            "max_attempts": max_attempts,
            "recent_days_window": recent_days,
            "sample": sample,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def backfill_relationship_temporal_decay(*, apply: bool = False, limit: int = 200000) -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(1000000, int(limit or 200000)))
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, created_at, confidence, confidence_score,
                   valid_from, valid_to, last_verified_at, decay_half_life_days,
                   effective_confidence, status, signed_weight
            FROM relationships_core
            ORDER BY id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        if not rows:
            return {
                "ok": True,
                "apply": bool(apply),
                "scanned": 0,
                "would_update": 0,
                "updated": 0,
                "sample": [],
            }

        updates: list[tuple[str, str, str, int, float, str, float, int]] = []
        for r in rows:
            rid = int(r[0] or 0)
            created_at = str(r[1] or "").strip()
            conf = float(r[2] or 0.0)
            conf_score = float(r[3] or 0.0)
            valid_from = str(r[4] or "").strip()
            valid_to = str(r[5] or "").strip()
            last_verified_at = str(r[6] or "").strip()
            half_life = int(r[7] or 0)
            effective_conf = float(r[8] or 0.0)
            status = str(r[9] or "").strip()
            signed_weight = float(r[10] or 0.0)

            vf_new = valid_from or created_at or dt.datetime.now().isoformat()
            lv_new = last_verified_at or created_at or dt.datetime.now().isoformat()
            hl_new = half_life if half_life > 0 else 365
            base = conf_score if conf_score > 0 else conf
            ec_new = _decay_confidence(base, lv_new, hl_new)
            st_new = status or "active"
            sw_new = signed_weight if signed_weight != 0.0 else 1.0

            needs = (
                (vf_new != valid_from)
                or (lv_new != last_verified_at)
                or (hl_new != half_life)
                or (abs(ec_new - effective_conf) > 1e-9)
                or (st_new != status)
                or (abs(sw_new - signed_weight) > 1e-9)
            )
            if needs:
                updates.append((vf_new, valid_to, lv_new, hl_new, ec_new, st_new, sw_new, rid))

        if not apply:
            return {
                "ok": True,
                "apply": False,
                "scanned": len(rows),
                "would_update": len(updates),
                "updated": 0,
                "sample": [
                    {"id": int(u[7]), "valid_from": u[0], "half_life": int(u[3]), "effective_confidence": round(float(u[4]), 6), "status": u[5]}
                    for u in updates[:25]
                ],
            }

        if updates:
            cur.executemany(
                """
                UPDATE relationships_core
                SET valid_from=%s,
                    valid_to=%s,
                    last_verified_at=%s,
                    decay_half_life_days=%s,
                    effective_confidence=%s,
                    status=%s,
                    signed_weight=%s
                WHERE id=%s
                """,
                [(u[0], u[1], u[2], int(u[3]), float(u[4]), u[5], float(u[6]), int(u[7])) for u in updates],
            )
        con.commit()
        updated_count = max(0, int(cur.rowcount or 0)) if updates else 0
        return {
            "ok": True,
            "apply": True,
            "scanned": len(rows),
            "would_update": len(updates),
            "updated": updated_count,
            "sample": [
                {"id": int(u[7]), "valid_from": u[0], "half_life": int(u[3]), "effective_confidence": round(float(u[4]), 6), "status": u[5]}
                for u in updates[:25]
            ],
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def refresh_relationship_decay_scores(*, apply: bool = False, limit: int = 500000) -> dict[str, Any]:
    """
    Recompute effective_confidence for existing edges using:
    - confidence_score (or confidence)
    - last_verified_at
    - decay_half_life_days
    """
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(2000000, int(limit or 500000)))
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, confidence, confidence_score, last_verified_at, decay_half_life_days, effective_confidence
            FROM relationships_core
            ORDER BY id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        updates: list[tuple[float, int]] = []
        for r in rows:
            rid = int(r[0] or 0)
            conf = float(r[1] or 0.0)
            conf_score = float(r[2] or 0.0)
            last_verified = str(r[3] or "").strip()
            half_life = int(r[4] or 365)
            current_eff = float(r[5] or 0.0)
            base = conf_score if conf_score > 0 else conf
            new_eff = _decay_confidence(base, last_verified or dt.datetime.now().isoformat(), half_life if half_life > 0 else 365)
            if abs(new_eff - current_eff) > 1e-9:
                updates.append((float(new_eff), int(rid)))

        if not apply:
            anti = enqueue_stale_anti_relationships(apply=False, limit=lim)
            return {
                "ok": True,
                "apply": False,
                "scanned": len(rows),
                "would_update": len(updates),
                "updated": 0,
                "sample": [{"id": int(u[1]), "effective_confidence": round(float(u[0]), 6)} for u in updates[:25]],
                "anti_reverify": anti,
            }

        if updates:
            cur.executemany(
                "UPDATE relationships_core SET effective_confidence=%s WHERE id=%s",
                [(float(eff), int(rid)) for eff, rid in updates],
            )
        con.commit()
        updated_count = max(0, int(cur.rowcount or 0)) if updates else 0
        anti = enqueue_stale_anti_relationships(apply=True, limit=lim)
        return {
            "ok": True,
            "apply": True,
            "scanned": len(rows),
            "would_update": len(updates),
            "updated": updated_count,
            "sample": [{"id": int(u[1]), "effective_confidence": round(float(u[0]), 6)} for u in updates[:25]],
            "anti_reverify": anti,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def _criticality_review_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_CRITICALITY_REVIEW_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _criticality_llm_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_CRITICALITY_LLM_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _criticality_low_conf_max() -> float:
    try:
        v = float(str(os.getenv("ONTOLOGY_CRITICALITY_LOW_CONF_MAX", "0.72")).strip() or "0.72")
    except Exception:
        v = 0.72
    return max(0.1, min(0.99, v))


def _criticality_min_impact() -> float:
    try:
        v = float(str(os.getenv("ONTOLOGY_CRITICALITY_MIN_IMPACT_SCORE", "0.35")).strip() or "0.35")
    except Exception:
        v = 0.35
    return max(0.05, min(1.0, v))


def _criticality_llm_min_confidence() -> float:
    try:
        v = float(str(os.getenv("ONTOLOGY_CRITICALITY_LLM_MIN_CONFIDENCE", "0.65")).strip() or "0.65")
    except Exception:
        v = 0.65
    return max(0.1, min(0.99, v))


def ensure_criticality_review_schema() -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS criticality_score DOUBLE PRECISION NOT NULL DEFAULT 0.5")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS criticality_review_queue_core (
                id BIGINT PRIMARY KEY,
                relationship_id BIGINT NOT NULL UNIQUE,
                priority_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                old_criticality DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                suggested_criticality DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                llm_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_crq_core_status_priority ON criticality_review_queue_core(status, priority_score DESC, id ASC)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crq_core_rel ON criticality_review_queue_core(relationship_id)")
        con.commit()
        return {"ok": True}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def enqueue_criticality_review_candidates(*, apply: bool = False, limit: int = 200000) -> dict[str, Any]:
    if not _criticality_review_enabled():
        return {"ok": True, "enabled": False, "apply": bool(apply), "candidates": 0, "queued": 0}
    st = ensure_criticality_review_schema()
    if not bool(st.get("ok")):
        return st
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(2000000, int(limit or 200000)))
    low_conf_max = _criticality_low_conf_max()
    min_impact = _criticality_min_impact()
    sample: list[dict[str, Any]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, relationship_type,
                   COALESCE(effective_confidence, confidence_score, confidence, 0.0) AS eff_conf,
                   COALESCE(NULLIF(criticality_score, 0.0), 0.5) AS criticality_score
            FROM relationships_core
            WHERE COALESCE(status,'active')='active'
            ORDER BY id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        payload: list[tuple[int, float, str, float, str, str]] = []
        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            rid = int(r[0] or 0)
            rel_type = str(r[1] or "").strip().upper()
            eff_conf = max(0.0, min(1.0, float(r[2] or 0.0)))
            old_crit = max(0.05, min(1.0, float(r[3] or 0.5)))
            impact_score = eff_conf * old_crit
            if eff_conf > low_conf_max or impact_score < min_impact:
                continue
            if rel_type not in {"SUPPLIER_TO", "CUSTOMER_OF", "COMPETES_WITH", "EXPOSED_TO", "SIGNALS_MACRO"}:
                continue
            priority = (min_impact - impact_score) + (low_conf_max - eff_conf) + (0.15 if old_crit >= 0.7 else 0.0)
            reason = f"low_conf_{round(eff_conf,4)}_impact_{round(impact_score,4)}"
            payload.append((rid, float(priority), reason[:180], old_crit, now_iso, now_iso))
            if len(sample) < 25:
                sample.append(
                    {
                        "relationship_id": rid,
                        "relationship_type": rel_type,
                        "effective_confidence": round(eff_conf, 6),
                        "criticality_score": round(old_crit, 6),
                        "impact_score": round(impact_score, 6),
                        "priority_score": round(float(priority), 6),
                    }
                )
        if not apply:
            return {
                "ok": True,
                "enabled": True,
                "apply": False,
                "scanned": len(rows),
                "candidates": len(payload),
                "queued": 0,
                "low_conf_max": low_conf_max,
                "min_impact_score": min_impact,
                "sample": sample,
            }
        if payload:
            cur.executemany(
                """
                INSERT INTO criticality_review_queue_core
                (id, relationship_id, priority_score, reason, status, old_criticality, created_at, updated_at)
                VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM criticality_review_queue_core), %s,%s,%s,'pending',%s,%s,%s)
                ON CONFLICT(relationship_id) DO UPDATE SET
                  priority_score=GREATEST(criticality_review_queue_core.priority_score, EXCLUDED.priority_score),
                  reason=EXCLUDED.reason,
                  old_criticality=EXCLUDED.old_criticality,
                  updated_at=EXCLUDED.updated_at,
                  status=CASE WHEN criticality_review_queue_core.status='resolved' THEN 'pending' ELSE criticality_review_queue_core.status END
                """,
                payload,
            )
        con.commit()
        queued = max(0, int(cur.rowcount or 0)) if payload else 0
        return {
            "ok": True,
            "enabled": True,
            "apply": True,
            "scanned": len(rows),
            "candidates": len(payload),
            "queued": queued,
            "low_conf_max": low_conf_max,
            "min_impact_score": min_impact,
            "sample": sample,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def _estimate_criticality_llm(
    *,
    relationship_type: str,
    source_name: str,
    target_name: str,
    citation_text: str,
    citation_link: str,
    old_criticality: float,
) -> tuple[float, float, str]:
    if ask_ai is None or not _criticality_llm_enabled():
        return old_criticality, 0.0, "llm_unavailable_or_disabled"
    prompt = (
        "You are scoring relationship criticality in an investment ontology graph.\n"
        "Return STRICT JSON only with keys: "
        '{"criticality": 0.0, "confidence": 0.0, "reason": "..."}.\n\n'
        f"relationship_type: {relationship_type}\n"
        f"source: {source_name}\n"
        f"target: {target_name}\n"
        f"prior_criticality: {old_criticality:.3f}\n"
        f"citation_link: {citation_link[:300]}\n"
        f"citation_text: {citation_text[:1400]}\n\n"
        "Rules:\n"
        "- criticality is 0.0-1.0 impact magnitude\n"
        "- high only for material/structural dependence\n"
        "- low for minor/replaceable/immaterial links\n"
        "- confidence is your confidence in this estimate\n"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Financial ontology reviewer. JSON only.",
                mode="fast",
                json_mode=True,
                temperature=0.1,
            )
            or ""
        ).strip()
        parsed = json.loads(raw) if raw else {}
        crit = max(0.05, min(1.0, float(parsed.get("criticality") or old_criticality)))
        conf = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
        reason = str(parsed.get("reason") or "").strip()[:400] or "llm_scored"
        return crit, conf, reason
    except Exception as exc:
        return old_criticality, 0.0, f"llm_error:{str(exc)[:120]}"


def process_criticality_review_queue(*, apply: bool = False, limit: int = 100) -> dict[str, Any]:
    if not _criticality_review_enabled():
        return {"ok": True, "enabled": False, "apply": bool(apply), "selected": 0, "resolved": 0, "review": 0, "errored": 0}
    st = ensure_criticality_review_schema()
    if not bool(st.get("ok")):
        return st
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(2000, int(limit or 100)))
    min_llm_conf = _criticality_llm_min_confidence()
    sample: list[dict[str, Any]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT q.id, q.relationship_id, q.attempts, q.old_criticality,
                   r.relationship_type, COALESCE(r.citation_text,''), COALESCE(r.citation_link,''),
                   COALESCE(es.name,''), COALESCE(et.name,'')
            FROM criticality_review_queue_core q
            JOIN relationships_core r ON r.id = q.relationship_id
            LEFT JOIN entities_core es ON es.id = r.source_id
            LEFT JOIN entities_core et ON et.id = r.target_id
            WHERE q.status='pending'
            ORDER BY q.priority_score DESC, q.id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        resolved = 0
        review = 0
        errored = 0
        for r in rows:
            qid = int(r[0] or 0)
            rid = int(r[1] or 0)
            attempts = int(r[2] or 0)
            old_crit = max(0.05, min(1.0, float(r[3] or 0.5)))
            rel_type = str(r[4] or "").strip().upper()
            citation_text = str(r[5] or "")
            citation_link = str(r[6] or "")
            src_name = str(r[7] or "").strip()
            tgt_name = str(r[8] or "").strip()
            new_crit, llm_conf, reason = _estimate_criticality_llm(
                relationship_type=rel_type,
                source_name=src_name,
                target_name=tgt_name,
                citation_text=citation_text,
                citation_link=citation_link,
                old_criticality=old_crit,
            )
            decision = "review"
            if llm_conf >= min_llm_conf:
                decision = "resolved"
            if apply:
                if decision == "resolved":
                    cur.execute(
                        "UPDATE relationships_core SET criticality_score=%s, last_verified_at=%s WHERE id=%s",
                        (float(new_crit), now_iso, rid),
                    )
                    cur.execute(
                        """
                        UPDATE criticality_review_queue_core
                        SET status='resolved',
                            attempts=%s,
                            suggested_criticality=%s,
                            llm_confidence=%s,
                            detail_json=%s::jsonb,
                            error_text='',
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            attempts + 1,
                            float(new_crit),
                            float(llm_conf),
                            json.dumps({"reason": reason}, ensure_ascii=True),
                            now_iso,
                            qid,
                        ),
                    )
                    resolved += 1
                else:
                    cur.execute(
                        """
                        UPDATE criticality_review_queue_core
                        SET status='needs_review',
                            attempts=%s,
                            suggested_criticality=%s,
                            llm_confidence=%s,
                            detail_json=%s::jsonb,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            attempts + 1,
                            float(new_crit),
                            float(llm_conf),
                            json.dumps({"reason": reason}, ensure_ascii=True),
                            now_iso,
                            qid,
                        ),
                    )
                    review += 1
            else:
                if decision == "resolved":
                    resolved += 1
                else:
                    review += 1
            if len(sample) < 25:
                sample.append(
                    {
                        "queue_id": qid,
                        "relationship_id": rid,
                        "relationship_type": rel_type,
                        "old_criticality": round(old_crit, 6),
                        "new_criticality": round(float(new_crit), 6),
                        "llm_confidence": round(float(llm_conf), 6),
                        "decision": decision,
                    }
                )
        if apply:
            con.commit()
        return {
            "ok": True,
            "enabled": True,
            "apply": bool(apply),
            "selected": len(rows),
            "resolved": resolved,
            "review": review,
            "errored": errored,
            "llm_min_confidence": min_llm_conf,
            "sample": sample,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()
