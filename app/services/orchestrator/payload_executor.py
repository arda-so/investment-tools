from __future__ import annotations

def _extract_query_tickers_for_tools(query: str, context: dict | None = None, max_items: int = 1) -> list[str]:
    from app.services.orchestrator.command_parser import extract_tickers_bulk
    return extract_tickers_bulk(query)[:max_items]

def _execute_financial_tool_call(name: str, args: dict[str, object], context: dict | None = None) -> dict[str, object]:
    nm = str(name or "").strip()
    a = dict(args or {})

    if nm == "fetch_historical_financials":
        from app.services.mini_statements_service import fetch_historical_financials
        t = str(a.get("ticker") or "").strip().upper()
        metric = str(a.get("metric") or "all").strip().lower()
        years_raw = a.get("years")
        refresh_raw = a.get("refresh")
        if not t:
            cands = _extract_query_tickers_for_tools("", context=context, max_items=1)
            t = str(cands[0] or "").strip().upper() if cands else ""
        try:
            years = int(years_raw) if years_raw is not None else 5
        except Exception:
            years = 5
        refresh = bool(refresh_raw) if isinstance(refresh_raw, bool) else (str(refresh_raw or "").strip().lower() in {"1", "true", "yes", "on"})
        return fetch_historical_financials(ticker=t, metric=metric or "all", years=years, refresh=refresh)

    if nm == "search_news":
        try:
            from app.services.web_search_service import search_news as _search_news
            query = str(a.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "missing required parameter: query"}
            max_results = max(1, min(10, int(a.get("max_results") or 6)))
            results = _search_news(query, max_results=max_results)
            return {"ok": True, "query": query, "count": len(results), "results": results}
        except Exception as exc:
            return {"ok": False, "error": f"search_news failed: {exc}"}

    if nm == "get_portfolio_summary":
        try:
            from app.services.portfolio_memory_service import get_live_portfolio_summary
            summary = get_live_portfolio_summary()
            return {"ok": True, "data": dict(summary or {})}
        except Exception as exc:
            return {"ok": False, "error": f"get_portfolio_summary failed: {exc}"}

    if nm == "get_proposals":
        try:
            from app.services.proactive_ai_service import list_action_proposals
            status = str(a.get("status") or "open").strip().lower()
            limit = max(1, min(20, int(a.get("limit") or 8)))
            proposals = list_action_proposals(status=status, limit=limit)
            return {"ok": True, "status": status, "count": len(proposals), "proposals": proposals}
        except Exception as exc:
            return {"ok": False, "error": f"get_proposals failed: {exc}"}

    return {"ok": False, "error": "unknown_tool", "name": nm}
