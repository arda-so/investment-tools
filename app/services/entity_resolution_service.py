from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any

from app.services.postgres_core_service import core_backend, pg_connect

try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore


_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "llc",
    "plc",
    "ag",
    "sa",
    "nv",
    "holdings",
    "holding",
    "group",
}


def canonical_llm_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_CANONICAL_LLM_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _postgres_required() -> bool:
    return core_backend() == "postgres"


def _clean_company_key(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    toks = [t for t in re.split(r"\s+", s.strip()) if t]
    out: list[str] = []
    for t in toks:
        if t in _LEGAL_SUFFIXES:
            continue
        out.append(t)
    return " ".join(out).strip()


def _normalize_er_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def ensure_entity_resolution_schema() -> dict[str, Any]:
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_resolution_queue_core (
                id BIGINT PRIMARY KEY,
                pair_key TEXT NOT NULL UNIQUE,
                left_entity_id BIGINT NOT NULL,
                right_entity_id BIGINT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                deterministic_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending',
                llm_decision TEXT NOT NULL DEFAULT '',
                llm_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                llm_rationale TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_erq_core_status ON entity_resolution_queue_core(status, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_erq_core_pair ON entity_resolution_queue_core(left_entity_id, right_entity_id)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_canonical_map_core (
                child_entity_id BIGINT PRIMARY KEY,
                canonical_entity_id BIGINT NOT NULL,
                method TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecm_core_canon ON entity_canonical_map_core(canonical_entity_id)")
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


def _pair_key(a: int, b: int) -> str:
    x = int(a or 0)
    y = int(b or 0)
    if x <= 0 or y <= 0 or x == y:
        return ""
    lo = min(x, y)
    hi = max(x, y)
    return f"{lo}:{hi}"


def _fetch_company_rows(limit: int) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(100, min(50000, int(limit or 5000)))
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, name, normalized_name,
                   COALESCE(metadata_json->>'ticker','') AS ticker,
                   COALESCE(metadata_json->>'cik','') AS cik
            FROM entities_core
            WHERE type='COMPANY'
            ORDER BY id DESC
            LIMIT %s
            """,
            (lim,),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall() or []:
            out.append(
                {
                    "id": int(r[0] or 0),
                    "name": str(r[1] or "").strip(),
                    "normalized_name": _normalize_er_name(str(r[2] or "")),
                    "ticker": str(r[3] or "").strip().upper(),
                    "cik": str(r[4] or "").strip(),
                }
            )
        return out
    finally:
        con.close()


def _score_pair(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    na = _normalize_er_name(str(a.get("normalized_name") or ""))
    nb = _normalize_er_name(str(b.get("normalized_name") or ""))
    ka = _clean_company_key(str(a.get("name") or ""))
    kb = _clean_company_key(str(b.get("name") or ""))
    ta = str(a.get("ticker") or "").strip().upper()
    tb = str(b.get("ticker") or "").strip().upper()
    ca = str(a.get("cik") or "").strip()
    cb = str(b.get("cik") or "").strip()

    if ta and tb and ta == tb:
        return 0.995, "same_ticker"
    if ca and cb and ca == cb:
        return 0.995, "same_cik"
    if na and nb and na == nb:
        return 0.98, "same_normalized_name"
    if ka and kb and ka == kb and len(ka) >= 5:
        return 0.96, "same_company_key"
    if ka and kb and len(ka) >= 8 and (ka.startswith(kb) or kb.startswith(ka)):
        return 0.9, "prefix_company_key"
    return 0.0, ""


def _choose_canonical(a: dict[str, Any], b: dict[str, Any]) -> int:
    ta = str(a.get("ticker") or "").strip().upper()
    tb = str(b.get("ticker") or "").strip().upper()
    if ta and not tb:
        return int(a.get("id") or 0)
    if tb and not ta:
        return int(b.get("id") or 0)
    na = str(a.get("name") or "")
    nb = str(b.get("name") or "")
    if len(na) < len(nb):
        return int(a.get("id") or 0)
    if len(nb) < len(na):
        return int(b.get("id") or 0)
    return min(int(a.get("id") or 0), int(b.get("id") or 0))


def _enqueue_candidates_bulk(cur: Any, pairs: list[tuple[int, int, str, float]]) -> int:
    now = _now_iso()
    payload: list[tuple[str, int, int, str, float, str, str]] = []
    for left_id, right_id, reason, score in pairs:
        pk = _pair_key(left_id, right_id)
        if not pk:
            continue
        payload.append(
            (
                pk,
                int(min(left_id, right_id)),
                int(max(left_id, right_id)),
                str(reason or "")[:120],
                float(score),
                now,
                now,
            )
        )
    if not payload:
        return 0
    cur.executemany(
        """
        INSERT INTO entity_resolution_queue_core
        (id, pair_key, left_entity_id, right_entity_id, reason, deterministic_score, status, created_at, updated_at)
        VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM entity_resolution_queue_core), %s,%s,%s,%s,%s,'pending',%s,%s)
        ON CONFLICT(pair_key) DO UPDATE SET
          reason=EXCLUDED.reason,
          deterministic_score=GREATEST(entity_resolution_queue_core.deterministic_score, EXCLUDED.deterministic_score),
          updated_at=EXCLUDED.updated_at
        """,
        payload,
    )
    return max(0, int(cur.rowcount or 0))


def merge_company_entities(
    canonical_id: int,
    duplicate_id: int,
    *,
    method: str,
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    cid = int(canonical_id or 0)
    did = int(duplicate_id or 0)
    if cid <= 0 or did <= 0 or cid == did:
        return {"ok": False, "error": "invalid_ids"}
    if not _postgres_required():
        return {"ok": False, "error": "postgres_required"}
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        cur.execute("SELECT id, type FROM entities_core WHERE id IN (%s,%s)", (cid, did))
        rows = cur.fetchall() or []
        ids = {int(r[0] or 0): str(r[1] or "") for r in rows}
        if ids.get(cid) != "COMPANY" or ids.get(did) != "COMPANY":
            return {"ok": False, "error": "company_entities_required"}

        # Deduplicate collisions before remapping source side.
        cur.execute(
            """
            DELETE FROM relationships_core r
            USING relationships_core k
            WHERE r.source_id=%s
              AND k.source_id=%s
              AND r.target_id=k.target_id
              AND r.relationship_type=k.relationship_type
              AND COALESCE(r.citation_link,'')=COALESCE(k.citation_link,'')
            """,
            (did, cid),
        )
        pre_del_source = int(cur.rowcount or 0)

        # Deduplicate collisions before remapping target side.
        cur.execute(
            """
            DELETE FROM relationships_core r
            USING relationships_core k
            WHERE r.target_id=%s
              AND k.target_id=%s
              AND r.source_id=k.source_id
              AND r.relationship_type=k.relationship_type
              AND COALESCE(r.citation_link,'')=COALESCE(k.citation_link,'')
            """,
            (did, cid),
        )
        pre_del_target = int(cur.rowcount or 0)

        cur.execute("UPDATE relationships_core SET source_id=%s WHERE source_id=%s", (cid, did))
        upd_source = int(cur.rowcount or 0)
        cur.execute("UPDATE relationships_core SET target_id=%s WHERE target_id=%s", (cid, did))
        upd_target = int(cur.rowcount or 0)

        # Final duplicate cleanup if both sides converged to same edge.
        cur.execute(
            """
            DELETE FROM relationships_core a
            USING relationships_core b
            WHERE a.id > b.id
              AND a.source_id=b.source_id
              AND a.target_id=b.target_id
              AND a.relationship_type=b.relationship_type
              AND COALESCE(a.citation_link,'')=COALESCE(b.citation_link,'')
            """
        )
        post_dedup = int(cur.rowcount or 0)

        now = _now_iso()
        cur.execute(
            """
            INSERT INTO entity_canonical_map_core
            (child_entity_id, canonical_entity_id, method, confidence, reason, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(child_entity_id) DO UPDATE SET
              canonical_entity_id=EXCLUDED.canonical_entity_id,
              method=EXCLUDED.method,
              confidence=EXCLUDED.confidence,
              reason=EXCLUDED.reason,
              updated_at=EXCLUDED.updated_at
            """,
            (did, cid, str(method or "")[:48], float(confidence), str(reason or "")[:500], now, now),
        )

        cur.execute("DELETE FROM entities_core WHERE id=%s", (did,))
        deleted_entities = int(cur.rowcount or 0)

        con.commit()
        return {
            "ok": True,
            "canonical_id": cid,
            "duplicate_id": did,
            "updated_source_edges": upd_source,
            "updated_target_edges": upd_target,
            "deleted_duplicate_edges": pre_del_source + pre_del_target + post_dedup,
            "deleted_entities": deleted_entities,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "canonical_id": cid, "duplicate_id": did}
    finally:
        con.close()


def run_deterministic_canonicalization(
    *,
    apply: bool = False,
    scan_limit: int = 5000,
    queue_limit: int = 2000,
) -> dict[str, Any]:
    st = ensure_entity_resolution_schema()
    if not bool(st.get("ok")):
        return st
    rows = _fetch_company_rows(limit=scan_limit)
    if not rows:
        return {
            "ok": True,
            "apply": bool(apply),
            "scanned": 0,
            "candidate_pairs": 0,
            "auto_merged": 0,
            "queued": 0,
        }

    by_key: dict[str, list[dict[str, Any]]] = {}
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = _clean_company_key(str(r.get("name") or ""))
        tk = str(r.get("ticker") or "").strip().upper()
        if key:
            by_key.setdefault(key, []).append(r)
        if tk:
            by_ticker.setdefault(tk, []).append(r)

    candidate_pairs: dict[str, dict[str, Any]] = {}

    def add_pair(a: dict[str, Any], b: dict[str, Any]) -> None:
        aid = int(a.get("id") or 0)
        bid = int(b.get("id") or 0)
        if aid <= 0 or bid <= 0 or aid == bid:
            return
        score, why = _score_pair(a, b)
        if score <= 0:
            return
        pk = _pair_key(aid, bid)
        if not pk:
            return
        cur = candidate_pairs.get(pk)
        if cur is None or float(cur.get("score") or 0.0) < score:
            candidate_pairs[pk] = {"a": a, "b": b, "score": score, "reason": why}

    for _, bucket in by_ticker.items():
        if len(bucket) < 2:
            continue
        b = sorted(bucket, key=lambda x: int(x.get("id") or 0), reverse=True)
        for i in range(len(b)):
            for j in range(i + 1, len(b)):
                add_pair(b[i], b[j])

    for _, bucket in by_key.items():
        if len(bucket) < 2:
            continue
        b = sorted(bucket, key=lambda x: int(x.get("id") or 0), reverse=True)
        for i in range(len(b)):
            for j in range(i + 1, len(b)):
                add_pair(b[i], b[j])

    auto_merge_threshold = max(0.7, min(0.999, _safe_float(os.getenv("ONTOLOGY_CANONICAL_DETERMINISTIC_THRESHOLD", "0.97"), 0.97)))
    ordered = sorted(candidate_pairs.values(), key=lambda x: float(x.get("score") or 0.0), reverse=True)

    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    try:
        cur = con.cursor()
        queued = 0
        merged = 0
        merge_fail = 0
        examined = 0
        sample: list[dict[str, Any]] = []
        to_queue: list[tuple[int, int, str, float]] = []

        for rec in ordered:
            if examined >= max(1, int(queue_limit or 2000)):
                break
            examined += 1
            a = rec["a"]
            b = rec["b"]
            score = float(rec.get("score") or 0.0)
            reason = str(rec.get("reason") or "").strip()
            canonical = _choose_canonical(a, b)
            duplicate = int(b.get("id") or 0) if canonical == int(a.get("id") or 0) else int(a.get("id") or 0)
            if canonical <= 0 or duplicate <= 0 or canonical == duplicate:
                continue

            if score >= auto_merge_threshold:
                if apply:
                    out = merge_company_entities(
                        canonical,
                        duplicate,
                        method="deterministic",
                        confidence=score,
                        reason=reason,
                    )
                    if bool(out.get("ok")):
                        merged += 1
                    else:
                        merge_fail += 1
                        to_queue.append((canonical, duplicate, reason, score))
                else:
                    sample.append(
                        {
                            "canonical_id": canonical,
                            "duplicate_id": duplicate,
                            "reason": reason,
                            "score": round(score, 6),
                            "would_merge": True,
                        }
                    )
            else:
                to_queue.append((canonical, duplicate, reason, score))
                if len(sample) < 25:
                    sample.append(
                        {
                            "canonical_id": canonical,
                            "duplicate_id": duplicate,
                            "reason": reason,
                            "score": round(score, 6),
                            "would_merge": False,
                        }
                    )

        queued = _enqueue_candidates_bulk(cur, to_queue)
        con.commit()
        return {
            "ok": True,
            "apply": bool(apply),
            "scanned": len(rows),
            "candidate_pairs": len(ordered),
            "examined": examined,
            "auto_merge_threshold": auto_merge_threshold,
            "auto_merged": merged,
            "merge_failures": merge_fail,
            "queued": queued,
            "sample": sample[:25],
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()


def _fetch_entity_pair(cur: Any, left_id: int, right_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    cur.execute(
        """
        SELECT id, name, normalized_name,
               COALESCE(metadata_json->>'ticker','') AS ticker,
               COALESCE(metadata_json->>'cik','') AS cik
        FROM entities_core
        WHERE id IN (%s,%s) AND type='COMPANY'
        """,
        (int(left_id), int(right_id)),
    )
    rows = cur.fetchall() or []
    a = None
    b = None
    for r in rows:
        rec = {
            "id": int(r[0] or 0),
            "name": str(r[1] or "").strip(),
            "normalized_name": str(r[2] or "").strip(),
            "ticker": str(r[3] or "").strip().upper(),
            "cik": str(r[4] or "").strip(),
        }
        if int(rec["id"]) == int(left_id):
            a = rec
        elif int(rec["id"]) == int(right_id):
            b = rec
    return a, b


def _llm_judge_same_company(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    score, reason = _score_pair(left, right)
    if score >= 0.995:
        canonical = _choose_canonical(left, right)
        return {
            "same_company": True,
            "canonical_id": canonical,
            "confidence": score,
            "rationale": f"deterministic_{reason}",
            "source": "deterministic",
        }

    if ask_ai is None or not canonical_llm_enabled():
        return {
            "same_company": False,
            "canonical_id": 0,
            "confidence": 0.0,
            "rationale": "llm_disabled_or_unavailable",
            "source": "guard",
        }

    prompt = (
        "Decide if these are the same public company entity. Return strict JSON only.\n"
        "Schema: {\"same_company\":bool,\"canonical\":\"left|right|unknown\",\"confidence\":number,\"rationale\":string}.\n"
        f"Left: name={left.get('name')} normalized={left.get('normalized_name')} ticker={left.get('ticker')} cik={left.get('cik')}\n"
        f"Right: name={right.get('name')} normalized={right.get('normalized_name')} ticker={right.get('ticker')} cik={right.get('cik')}\n"
        "Use conservative judgment. If uncertain, same_company=false and canonical='unknown'."
    )
    raw = str(
        ask_ai(
            prompt,
            "Entity resolution judge for ontology canonicalization. JSON only.",
            mode="fast",
            json_mode=True,
            temperature=0.0,
        )
        or ""
    ).strip()
    parsed = json.loads(raw)
    same = bool(parsed.get("same_company"))
    canon = str(parsed.get("canonical") or "unknown").strip().lower()
    conf = max(0.0, min(1.0, _safe_float(parsed.get("confidence"), 0.0)))
    rationale = str(parsed.get("rationale") or "").strip()[:500]
    canonical_id = 0
    if same:
        if canon == "left":
            canonical_id = int(left.get("id") or 0)
        elif canon == "right":
            canonical_id = int(right.get("id") or 0)
        else:
            canonical_id = _choose_canonical(left, right)
    return {
        "same_company": same,
        "canonical_id": canonical_id,
        "confidence": conf,
        "rationale": rationale or "llm_judge",
        "source": "llm",
    }


def process_entity_resolution_judge_queue(*, apply: bool = False, limit: int = 100) -> dict[str, Any]:
    st = ensure_entity_resolution_schema()
    if not bool(st.get("ok")):
        return st
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "postgres_unavailable"}
    lim = max(1, min(5000, int(limit or 100)))
    min_conf = max(0.5, min(0.999, _safe_float(os.getenv("ONTOLOGY_CANONICAL_LLM_MIN_CONFIDENCE", "0.86"), 0.86)))
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, left_entity_id, right_entity_id, reason, deterministic_score, attempts
            FROM entity_resolution_queue_core
            WHERE status='pending'
            ORDER BY deterministic_score DESC, id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        decided = 0
        merged = 0
        rejected = 0
        errors = 0
        sample: list[dict[str, Any]] = []

        for r in rows:
            qid = int(r[0] or 0)
            left_id = int(r[1] or 0)
            right_id = int(r[2] or 0)
            attempts = int(r[5] or 0)
            left, right = _fetch_entity_pair(cur, left_id, right_id)
            now = _now_iso()
            if not left or not right:
                cur.execute(
                    """
                    UPDATE entity_resolution_queue_core
                    SET status='error', error_text=%s, attempts=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    ("entity_missing", attempts + 1, now, qid),
                )
                errors += 1
                continue

            try:
                verdict = _llm_judge_same_company(left, right)
                same = bool(verdict.get("same_company"))
                conf = _safe_float(verdict.get("confidence"), 0.0)
                canonical_id = int(verdict.get("canonical_id") or 0)
                rationale = str(verdict.get("rationale") or "")[:500]
                source = str(verdict.get("source") or "judge")[:32]
                decided += 1

                if same and conf >= min_conf and canonical_id > 0:
                    duplicate_id = right_id if canonical_id == left_id else left_id
                    if apply:
                        m = merge_company_entities(
                            canonical_id,
                            duplicate_id,
                            method=f"{source}_judge",
                            confidence=conf,
                            reason=rationale,
                        )
                        if bool(m.get("ok")):
                            cur.execute(
                                """
                                UPDATE entity_resolution_queue_core
                                SET status='merged_llm', llm_decision='merge', llm_confidence=%s,
                                    llm_rationale=%s, attempts=%s, updated_at=%s, error_text=''
                                WHERE id=%s
                                """,
                                (conf, rationale, attempts + 1, now, qid),
                            )
                            merged += 1
                        else:
                            cur.execute(
                                """
                                UPDATE entity_resolution_queue_core
                                SET status='error', llm_decision='merge_failed', llm_confidence=%s,
                                    llm_rationale=%s, attempts=%s, updated_at=%s, error_text=%s
                                WHERE id=%s
                                """,
                                (conf, rationale, attempts + 1, now, str(m.get("error") or "merge_failed")[:500], qid),
                            )
                            errors += 1
                    else:
                        sample.append(
                            {
                                "queue_id": qid,
                                "left_id": left_id,
                                "right_id": right_id,
                                "decision": "merge",
                                "confidence": round(conf, 6),
                                "canonical_id": canonical_id,
                            }
                        )
                else:
                    cur.execute(
                        """
                        UPDATE entity_resolution_queue_core
                        SET status='rejected', llm_decision='reject', llm_confidence=%s,
                            llm_rationale=%s, attempts=%s, updated_at=%s
                        WHERE id=%s
                        """,
                        (conf, rationale or "not_confident_enough", attempts + 1, now, qid),
                    )
                    rejected += 1
                    if len(sample) < 25:
                        sample.append(
                            {
                                "queue_id": qid,
                                "left_id": left_id,
                                "right_id": right_id,
                                "decision": "reject",
                                "confidence": round(conf, 6),
                            }
                        )
            except Exception as exc:
                cur.execute(
                    """
                    UPDATE entity_resolution_queue_core
                    SET status='error', llm_decision='error', attempts=%s,
                        error_text=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (attempts + 1, str(exc)[:500], now, qid),
                )
                errors += 1

        con.commit()
        return {
            "ok": True,
            "apply": bool(apply),
            "min_confidence": min_conf,
            "selected": len(rows),
            "decided": decided,
            "merged": merged,
            "rejected": rejected,
            "errors": errors,
            "sample": sample[:25],
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "apply": bool(apply)}
    finally:
        con.close()
