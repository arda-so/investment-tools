from __future__ import annotations

# Routing map structure extracted from orchestrator
def get_routes() -> list[tuple[set[str], str, str]]:
    return [
        ({"dashboard", "home", "main", "main page", "landing", "start page", "briefing", "market"}, "/dashboard", "Dashboard"),
        ({"intel feed", "feed", "live wire"}, "/dashboard", "Intel Feed"),
        ({"portfolio", "positions", "holdings", "my stocks", "my portfolio", "investments", "my money", "account"}, "/organizer/portfolio", "Portfolio"),
        ({"watch list", "watchlist", "targets", "radar"}, "/organizer/watchlist", "Watchlist"),
        ({"notes", "stickies", "memos", "drafts", "notebook", "log", "logs", "knowledge base"}, "/organizer/notes", "Active Notes"),
        ({"reports", "sec findings", "filings", "my reports", "library", "research library", "inbox"}, "/reports", "Research Library"),
        ({"settings", "config", "account settings", "preferences"}, "/organizer/settings", "Settings"),
        ({"tasks", "todos", "action items"}, "/organizer/home", "Organizer Tasks"),
        ({"earnings", "calendar", "earnings calendar", "upcoming earnings"}, "/organizer/home", "Earnings Calendar"),
        ({"blue chips", "top companies", "market titans", "blue chip", "tech giants"}, "/organizer/blue_chips", "Blue Chips Universe"),
        ({"supply chain", "dependencies", "suppliers", "customers", "network map", "value chain"}, "/supply-chain", "Supply Chain Network"),
        ({"system logs", "trace logs", "agent logs", "reflexion logs", "events", "system status", "observability", "metrics"}, "/observability", "System Observability"),
    ]

def get_mapped_route(fallback: str, raw: str) -> str:

    mapped = _mapped_route_impl(fallback, raw)
    return mapped

def _mapped_route_impl(fallback: str, q: str) -> str:
    from app.core.normalize import normalize_text as _norm
    low = _norm(q)
    
    # 1. Exact alias match against routes map
    for triggers, url, label in get_routes():
        if any(f" {t} " in f" {low} " for t in triggers):
            return url
            
    # 2. Heuristic specific routing matching
    if "open" in low and ("company" in low or "ticker" in low):
        from app.services.orchestrator.command_parser import extract_ticker
        tk = extract_ticker(low)
        if tk:
            return f"/company_file?t={tk}"
            
    if "open" in low and ("report" in low or "latest report" in low):
        return "/reports"
        
    if "open" in low and "portfolio" in low:
        return "/organizer/portfolio"
        
    return fallback
