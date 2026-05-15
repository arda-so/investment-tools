from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AICommandResult:
    status: str
    intent: str
    message: str
    confidence: float
    redirect_url: str = ""
    citations: list[dict[str, str]] | None = None
    matched_by: str = "rule"
    version: str = "v1.2.0"
    traces: list[dict[str, str]] | None = None
    ui: dict | None = None


@dataclass
class ParsedCommand:
    action: str = ""
    ticker: str = ""
    form: str = ""
    due_date: str = ""
    body: str = ""
    notes: str = ""


@dataclass
class Understanding:
    intent_class: str = ""
    confidence: float = 0.0
    topic: str = ""
    clarifying_question: str = ""
    missing_info: list[str] | None = None


@dataclass
class DecisionContract:
    intent_type: str = "chat"  # chat | action | mixed
    confidence: float = 0.0
    needs_clarification: bool = False
    proposed_action: str = ""
    reason: str = ""


@dataclass
class IntentCandidate:
    intent: str = ""
    confidence: float = 0.0
    source: str = "heuristic"
    ticker: str = ""
    due_date: str = ""
    rationale: str = ""
