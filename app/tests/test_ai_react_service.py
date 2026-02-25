import unittest
from unittest.mock import patch

from app.services.ai_react_service import run_react_information


class TestAIReactService(unittest.TestCase):
    def test_notes_summary_react(self) -> None:
        with patch(
            "app.services.ai_react_service.list_recent_notes",
            return_value=[{"ticker": "CRM", "text": "Margins improving in enterprise segment."}],
        ):
            out = run_react_information("what are my notes today?")
        self.assertIsInstance(out, dict)
        self.assertEqual(str(out.get("intent") or ""), "notes_summary")
        self.assertEqual(str(out.get("status") or ""), "ok")
        self.assertIn("Notes summary", str(out.get("message") or ""))
        self.assertIsInstance(out.get("ui"), dict)

    def test_summarize_current_report_react(self) -> None:
        with patch(
            "app.services.ai_react_service.read_report_file",
            return_value=("Revenue grew with improving free cash flow and lower churn.\nOperating leverage expanded this quarter.", ""),
        ):
            out = run_react_information(
                "summarize this report",
                context={"current_report_name": "terminal_daily_brief_20260218.md"},
            )
        self.assertIsInstance(out, dict)
        self.assertEqual(str(out.get("intent") or ""), "summarize_current_report")
        self.assertIn("Current Report", str(out.get("message") or ""))

    def test_task_list_react_returns_structured_ui(self) -> None:
        with patch(
            "app.services.ai_react_service.list_tasks",
            return_value=[{"id": 7, "task": "Register for university", "due_date": "2026-02-19"}],
        ):
            out = run_react_information("what are my tasks today?")
        self.assertIsInstance(out, dict)
        self.assertEqual(str(out.get("intent") or ""), "list_tasks")
        ui = out.get("ui")
        self.assertIsInstance(ui, dict)
        self.assertEqual(str((ui or {}).get("type") or ""), "task_list")


if __name__ == "__main__":
    unittest.main()
