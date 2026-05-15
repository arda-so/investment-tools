"""Multi-agent debate service — Phase 3.5.

Runs 3 parallel LLM perspectives (bull / bear / risk) then a sequential
judge to gate high-confidence proposals before they go live.

Table: proposal_debate_artifacts
"""
from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.services.postgres_core_service import pg_connect

try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]


_DEBATE_SIGNAL_CHARS = int(os.getenv("DEBATE_SIGNAL_CHARS", "1200"))
_DEBATE_REASONING_CHARS = int(os.getenv("DEBATE_REASONING_CHARS", "1000"))
_DEBATE_ADVOCATE_CHARS = int(os.getenv("DEBATE_ADVOCATE_CHARS", "1500"))
_DEBATE_JUDGE_SIGNAL_CHARS = int(os.getenv("DEBATE_JUDGE_SIGNAL_CHARS", "800"))
_DEBATE_ADVOCATE_WORDS = os.getenv("DEBATE_ADVOCATE_WORDS", "250-350")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_debate_schema() -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_debate_artifacts (
                id          BIGSERIAL PRIMARY KEY,
                proposal_id BIGINT,
                ticker      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                bull_thesis TEXT,
                bear_rebuttal TEXT,
                risk_review TEXT,
                judge_decision TEXT,
                judge_rationale TEXT,
                approved    BOOLEAN
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS pdx_proposal ON proposal_debate_artifacts(proposal_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS pdx_debate_ticker ON proposal_debate_artifacts(ticker)"
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Core debate runner
# ---------------------------------------------------------------------------

def run_proposal_debate(
    ticker: str,
    signal: str,
    proposed_stance: str,
    reasoning_summary: str,
    proposal_id: int | None = None,
) -> dict[str, Any]:
    """Run a multi-agent debate and return approved flag + artifacts.

    Stage A/B/C run in parallel (bull / bear / risk reviewers).
    Stage D is sequential (judge reads all three, renders verdict).

    Returns:
        {
            "approved": bool,
            "artifacts": {
                "bull_thesis": str,
                "bear_rebuttal": str,
                "risk_review": str,
                "judge_decision": str,  # "APPROVED" | "REJECTED"
                "judge_rationale": str,
            }
        }
    If ask_ai is unavailable, returns approved=True with empty artifacts
    so proposals are never silently blocked by a missing LLM engine.
    """
    if ask_ai is None:
        return {"approved": True, "artifacts": {}}

    tk = str(ticker or "").upper()
    header = (
        f"Ticker: {tk}\n"
        f"Signal: {signal[:_DEBATE_SIGNAL_CHARS]}\n"
        f"Proposed action: {proposed_stance}\n"
        f"Reasoning: {reasoning_summary[:_DEBATE_REASONING_CHARS]}"
    )

    def _bull() -> str:
        prompt = (
            f"{header}\n\n"
            "Write the STRONGEST bull case for taking this proposed action. Include:\n"
            "1. Why the signal is a genuine opportunity\n"
            "2. How it aligns with the investment thesis\n"
            "3. Market conditions that support it\n"
            f"Be specific and concise ({_DEBATE_ADVOCATE_WORDS} words)."
        )
        try:
            return str(
                ask_ai(prompt, f"Bull analyst for {tk}. Be constructive and specific.", mode="smart", temperature=0.3) or ""
            )
        except Exception as exc:
            return f"[bull error: {exc}]"

    def _bear() -> str:
        prompt = (
            f"{header}\n\n"
            "Write the STRONGEST bear rebuttal against this proposed action. Include:\n"
            "1. What the signal may be missing or misreading\n"
            "2. Downside risks and red flags\n"
            "3. Alternative explanations that invalidate the thesis\n"
            f"Be specific and concise ({_DEBATE_ADVOCATE_WORDS} words)."
        )
        try:
            return str(
                ask_ai(prompt, f"Bear analyst for {tk}. Be skeptical and rigorous.", mode="smart", temperature=0.3) or ""
            )
        except Exception as exc:
            return f"[bear error: {exc}]"

    def _risk() -> str:
        prompt = (
            f"{header}\n\n"
            "Review this proposal from a RISK MANAGEMENT perspective. Include:\n"
            "1. Position sizing concerns (concentration / correlation risk)\n"
            "2. Macro or sector tail risks\n"
            "3. Liquidity or timing risks\n"
            f"Be specific and concise ({_DEBATE_ADVOCATE_WORDS} words)."
        )
        try:
            return str(
                ask_ai(prompt, f"Risk manager for {tk}. Focus on capital preservation.", mode="smart", temperature=0.2) or ""
            )
        except Exception as exc:
            return f"[risk error: {exc}]"

    # --- Stage A/B/C: parallel ---
    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_bull): "bull_thesis",
            pool.submit(_bear): "bear_rebuttal",
            pool.submit(_risk): "risk_review",
        }
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                results[key] = fut.result()
            except Exception as exc:
                results[key] = f"[error: {exc}]"

    bull = results.get("bull_thesis", "")
    bear = results.get("bear_rebuttal", "")
    risk = results.get("risk_review", "")

    # --- Stage D: sequential judge ---
    judge_prompt = (
        f"You are a senior investment committee judge.\n\n"
        f"PROPOSAL: {proposed_stance} on {tk}\n"
        f"SIGNAL: {signal[:_DEBATE_JUDGE_SIGNAL_CHARS]}\n\n"
        f"BULL THESIS:\n{bull[:_DEBATE_ADVOCATE_CHARS]}\n\n"
        f"BEAR REBUTTAL:\n{bear[:_DEBATE_ADVOCATE_CHARS]}\n\n"
        f"RISK REVIEW:\n{risk[:_DEBATE_ADVOCATE_CHARS]}\n\n"
        "Render your verdict. Respond ONLY in valid JSON:\n"
        '{"approved": true, "rationale": "2-3 sentence explanation"}\n'
        "Approve if the bull case clearly outweighs risks. "
        "Reject if risks or the bear case are compelling. Default to caution."
    )
    judge_raw = ""
    judge_decision = "APPROVED"
    judge_rationale = ""
    approved = True
    try:
        judge_raw = str(
            ask_ai(
                judge_prompt,
                f"Investment committee judge for {tk}. JSON only.",
                mode="smart",
                json_mode=True,
                temperature=0.1,
            )
            or ""
        )
        parsed = json.loads(judge_raw)
        approved = bool(parsed.get("approved", True))
        judge_rationale = str(parsed.get("rationale", ""))
        judge_decision = "APPROVED" if approved else "REJECTED"
    except Exception:
        # Judge error → default approve so debate never silently drops proposals
        approved = True
        judge_decision = "APPROVED (judge error)"
        judge_rationale = f"Judge parse failed; defaulting to approve. raw={judge_raw[:200]}"

    _store_artifacts(
        proposal_id=proposal_id,
        ticker=tk,
        bull_thesis=bull,
        bear_rebuttal=bear,
        risk_review=risk,
        judge_decision=judge_decision,
        judge_rationale=judge_rationale,
        approved=approved,
    )

    return {
        "approved": approved,
        "artifacts": {
            "bull_thesis": bull,
            "bear_rebuttal": bear,
            "risk_review": risk,
            "judge_decision": judge_decision,
            "judge_rationale": judge_rationale,
        },
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _store_artifacts(
    *,
    proposal_id: int | None,
    ticker: str,
    bull_thesis: str,
    bear_rebuttal: str,
    risk_review: str,
    judge_decision: str,
    judge_rationale: str,
    approved: bool,
) -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO proposal_debate_artifacts
            (proposal_id, ticker, created_at,
             bull_thesis, bear_rebuttal, risk_review,
             judge_decision, judge_rationale, approved)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                proposal_id,
                str(ticker or "").upper(),
                dt.datetime.now().isoformat(),
                str(bull_thesis or "")[:3000],
                str(bear_rebuttal or "")[:3000],
                str(risk_review or "")[:3000],
                str(judge_decision or "")[:200],
                str(judge_rationale or "")[:1000],
                bool(approved),
            ),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def list_debate_artifacts(proposal_id: int) -> list[dict[str, Any]]:
    """Return debate artifacts for a given proposal (for audit display)."""
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT bull_thesis, bear_rebuttal, risk_review, judge_decision, judge_rationale, approved, created_at "
            "FROM proposal_debate_artifacts WHERE proposal_id=%s ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        )
        rows = cur.fetchall() or []
        result = []
        for r in rows:
            result.append(
                {
                    "bull_thesis": r[0],
                    "bear_rebuttal": r[1],
                    "risk_review": r[2],
                    "judge_decision": r[3],
                    "judge_rationale": r[4],
                    "approved": r[5],
                    "created_at": r[6],
                }
            )
        return result
    except Exception:
        return []
    finally:
        con.close()
