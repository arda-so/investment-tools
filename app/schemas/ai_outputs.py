"""
Schema contracts for LLM-generated outputs that get written to Postgres.

Usage:
    from app.schemas.ai_outputs import StructuredAnalysis, AgentRunOutput

    # After _parse_structured() in agent_worker:
    s = StructuredAnalysis.from_llm(raw_dict)

    # Before finish_agent_run():
    out = AgentRunOutput.from_dict(payload_dict)

Both methods: never raise, always return a usable object, emit structured
JSON warnings to stdout when the LLM produces unexpected values.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SENTIMENTS = {"bullish", "bearish", "neutral", "mixed", "cautious", ""}
_VALID_SEVERITIES = {"low", "medium", "high", "critical", ""}
_VALID_RISK_SEVERITIES = {"low", "medium", "high", "critical", ""}
_VALID_THESIS_STATUSES = {
    "on_track", "watchlist", "monitor", "confirmed", "watch",
    "breach_suspected", "breach_confirmed",
    "breach_revenue", "breach_margin", "breach_growth",
    "breach_moat", "breach_guidance", "breach_unspecified",
    "",
}


def _normalize_confidence(v: Any) -> str:
    """Normalize LLM confidence to a numeric string '0'-'100'.

    Accepts: '0.75', '75%', '75', 'high', 'medium', 'low', 75, 0.75
    Returns: string representation of an integer 0-100.
    On failure: returns '50' (unknown) and logs a warning.
    """
    text_map = {"high": "75", "medium": "50", "low": "25",
                "strong": "80", "weak": "30", "very high": "90"}
    if v is None or v == "":
        return ""
    sv = str(v).strip().lower()
    if sv in text_map:
        return text_map[sv]
    # strip % sign
    sv = sv.rstrip("%").strip()
    try:
        f = float(sv)
        # if 0 < f <= 1.0 treat as fraction, else treat as 0-100
        if 0 < f <= 1.0 and "." in str(v):
            return str(int(round(f * 100)))
        return str(int(round(min(100, max(0, f)))))
    except (ValueError, TypeError):
        return "50"  # unknown


def _emit_contract_warning(model: str, issues: list[str], raw: dict) -> None:
    """Emit structured JSON warning to stdout for Cloud Monitoring to pick up."""
    sys.stdout.write(json.dumps({
        "severity": "WARNING",
        "alert": "schema_contract_violation",
        "model": model,
        "issues": issues,
        "raw_keys": list(raw.keys()),
    }) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# StructuredAnalysis — validates _parse_structured() output from agent_worker
# ---------------------------------------------------------------------------

class StructuredAnalysis(BaseModel):
    """Validates the <STRUCTURED>...</STRUCTURED> block from LLM agent output."""
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    thesis_status: str = ""
    sentiment: str = ""
    confidence: str = ""       # normalized to '0'-'100' string
    breach_type: str = ""
    severity: str = ""
    breach_detail: str = ""
    actual_values: str = ""
    key_metric: str = ""
    cascade_tickers: str = ""
    risk_flags: str = ""       # Phase 4.2: comma-separated tail risk types
    risk_severity: str = ""    # Phase 4.2: low|medium|high|critical

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> str:
        return _normalize_confidence(v)

    @classmethod
    def from_llm(cls, raw: dict[str, Any]) -> "StructuredAnalysis":
        """Validate raw LLM dict. Never raises. Logs issues to stdout."""
        issues: list[str] = []
        sentiment = str(raw.get("sentiment") or "").strip().lower()
        if sentiment and sentiment not in _VALID_SENTIMENTS:
            issues.append(f"unexpected sentiment={sentiment!r}")
        severity = str(raw.get("severity") or "").strip().lower()
        if severity and severity not in _VALID_SEVERITIES:
            issues.append(f"unexpected severity={severity!r}")
        risk_severity = str(raw.get("risk_severity") or "").strip().lower()
        if risk_severity and risk_severity not in _VALID_RISK_SEVERITIES:
            issues.append(f"unexpected risk_severity={risk_severity!r}")
        thesis_status = str(raw.get("thesis_status") or "").strip().lower()
        if thesis_status and thesis_status not in _VALID_THESIS_STATUSES:
            issues.append(f"unexpected thesis_status={thesis_status!r}")
        if issues:
            _emit_contract_warning("StructuredAnalysis", issues, raw)
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            issues.append(f"pydantic_error={exc}")
            _emit_contract_warning("StructuredAnalysis", issues, raw)
            # Fallback: return default with whatever fields we can safely set
            safe = {k: str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
            safe["confidence"] = _normalize_confidence(raw.get("confidence"))
            try:
                return cls.model_validate(safe)
            except Exception:
                return cls()

    def to_dict(self) -> dict[str, str]:
        """Return as plain dict for use in existing code (replaces raw structured dict)."""
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# AgentRunOutput — validates finish_agent_run(output_payload=...) from agent_worker
# ---------------------------------------------------------------------------

class AgentRunOutput(BaseModel):
    """Validates the output_payload dict written to agent_runs_core.output_json."""
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    ticker: str = ""
    steps: int = 0
    report_chars: int = 0
    thesis_status: str = ""
    sentiment: str = ""
    confidence: str = ""  # normalized to '0'-'100' string

    @field_validator("steps", "report_chars", mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> int:
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> str:
        return _normalize_confidence(v)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentRunOutput":
        """Validate output_payload dict. Never raises. Logs issues to stdout."""
        issues: list[str] = []
        if not raw.get("ticker"):
            issues.append("missing ticker")
        if raw.get("steps") is not None:
            try:
                int(raw["steps"])
            except (ValueError, TypeError):
                issues.append(f"non-integer steps={raw['steps']!r}")
        if issues:
            _emit_contract_warning("AgentRunOutput", issues, raw)
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            _emit_contract_warning("AgentRunOutput", [f"pydantic_error={exc}"], raw)
            return cls()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
