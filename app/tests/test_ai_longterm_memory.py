import unittest
from unittest.mock import patch

from app.services.portfolio_memory_service import (
    ingest_recent_report_facts,
    learn_investor_style_from_answer,
    query_report_facts,
)


class TestAILongTermMemory(unittest.TestCase):
    def test_learn_style_from_structured_answer(self) -> None:
        out = learn_investor_style_from_answer("priority=moat, horizon=3-5y, risk=low")
        keys = {str(x.get("key") or "") for x in out}
        self.assertIn("compare_priority", keys)
        self.assertIn("compare_horizon", keys)
        self.assertIn("compare_risk", keys)

    def test_report_fact_ingest_and_query(self) -> None:
        with patch(
            "app.services.portfolio_memory_service.list_reports",
            return_value=[
                {
                    "name": "daily_brief_20260219.md",
                    "kind": "Daily Brief",
                    "modified": "2026-02-19 08:00:00",
                    "ticker_hits": "CRM, HUBS",
                }
            ],
        ), patch(
            "app.services.portfolio_memory_service.read_report_file",
            return_value=(
                "- CRM earnings results: margin improved and guidance raised.\n"
                "- HUBS risk: slower SMB demand this quarter.\n",
                "",
            ),
        ):
            stats = ingest_recent_report_facts(limit_reports=1, facts_per_report=6)
            self.assertGreaterEqual(int(stats.get("scanned") or 0), 1)
            rows = query_report_facts(query="earnings", tickers=["CRM", "HUBS"], limit=6)
            self.assertIsInstance(rows, list)
            self.assertGreaterEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()

