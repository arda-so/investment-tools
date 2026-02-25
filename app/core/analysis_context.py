from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class AnalysisContext:
    analysis_id: str = ""
    source: str = ""
    trace_id: str = ""

    def normalized(self) -> "AnalysisContext":
        return AnalysisContext(
            analysis_id=str(self.analysis_id or "").strip()[:160],
            source=str(self.source or "").strip()[:80],
            trace_id=str(self.trace_id or "").strip()[:120],
        )

    def to_query_dict(self) -> dict[str, str]:
        n = self.normalized()
        out: dict[str, str] = {}
        if n.analysis_id:
            out["analysis_id"] = n.analysis_id
        if n.source:
            out["source"] = n.source
        if n.trace_id:
            out["trace_id"] = n.trace_id
        return out

    def to_dict(self) -> dict[str, str]:
        return self.to_query_dict()


def build_analysis_context(analysis_id: str = "", source: str = "", trace_id: str = "") -> AnalysisContext:
    return AnalysisContext(
        analysis_id=str(analysis_id or ""),
        source=str(source or ""),
        trace_id=str(trace_id or ""),
    ).normalized()


def format_citation_url(base_url: str, context: AnalysisContext, extra_query: dict[str, str] | None = None) -> str:
    base = str(base_url or "").strip() or "/dashboard"
    parsed = urlparse(base)
    current = {k: v for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    current.update(context.to_query_dict())
    if isinstance(extra_query, dict):
        for k, v in extra_query.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip()
            if kk and vv:
                current[kk] = vv
    query = urlencode(current, doseq=False)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))


def build_execute_payload(route: str, context: AnalysisContext, payload: dict | None = None) -> dict:
    out: dict = dict(payload or {})
    out["route"] = str(route or "/dashboard").strip() or "/dashboard"
    out["analysis_context"] = context.to_dict()
    return out


def build_chat_hydration_payload(context: AnalysisContext, payload: dict | None = None) -> dict:
    out: dict = dict(payload or {})
    out["analysis_context"] = context.to_dict()
    return out
