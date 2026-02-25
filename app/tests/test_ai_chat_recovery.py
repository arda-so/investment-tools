import unittest

from fastapi.testclient import TestClient

from app.main import app


class TestAIChatRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_morning_brief_endpoint_exists(self) -> None:
        res = self.client.get("/ai/morning_brief")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIsInstance(body, dict)
        self.assertIn("ok", body)

    def test_watchlist_then_portfolio_switch(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-1", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-1"
        res2 = self.client.post("/ai/command", json={"query": "show me my portfolio", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("intent"), "list_portfolio")
        self.assertEqual(j2.get("status"), "needs_confirmation")

    def test_watchlidt_typo_routes_to_watchlist(self) -> None:
        res = self.client.post(
            "/ai/command",
            json={"query": "SHOW ME MY WATCHLIDT", "context": {"session_id": "t-recovery-2", "history": []}},
        )
        j = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(j.get("intent"), "list_watchlist")
        self.assertEqual(j.get("status"), "needs_confirmation")

    def test_task_query_interrupts_watchlist_confirmation(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-3", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-3"
        res2 = self.client.post("/ai/command", json={"query": "tell me my to do and tasks", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("intent"), "list_tasks")
        self.assertEqual(j2.get("status"), "ok")

    def test_manager_interrupt_handles_short_topic_switch_word(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-4", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-4"
        res2 = self.client.post("/ai/command", json={"query": "portfolio", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("intent"), "list_portfolio")
        self.assertEqual(j2.get("status"), "needs_confirmation")

    def test_manager_interrupt_from_interview_to_portfolio(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "start portfolio interview", "context": {"session_id": "t-recovery-5", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(j1.get("intent"), "portfolio_interview")
        self.assertEqual(j1.get("status"), "needs_input")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-5"
        res2 = self.client.post("/ai/command", json={"query": "portfolio", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("intent"), "list_portfolio")
        self.assertEqual(j2.get("status"), "needs_confirmation")

    def test_manager_interrupt_watchlist_to_open_company(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-6", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-6"
        res2 = self.client.post("/ai/command", json={"query": "open company CRM", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("intent"), "open_company")
        self.assertEqual(j2.get("status"), "ok")

    def test_manager_interrupt_watchlist_to_notes_summary(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-7", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-7"
        res2 = self.client.post("/ai/command", json={"query": "what are my notes today?", "context": ctx})
        j2 = res2.json()
        self.assertEqual(res2.status_code, 200)
        # Must switch away from watchlist confirmation flow.
        self.assertFalse(j2.get("intent") == "list_watchlist" and j2.get("status") == "needs_confirmation")

    def test_confirmation_recovery_after_repeated_unclear(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-8", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-8"
        res2 = self.client.post("/ai/command", json={"query": "what?", "context": ctx})
        j2 = res2.json()
        self.assertEqual(j2.get("status"), "needs_confirmation")

        ctx2 = j2.get("context") or {}
        ctx2["session_id"] = "t-recovery-8"
        res3 = self.client.post("/ai/command", json={"query": "hmm?", "context": ctx2})
        j3 = res3.json()
        self.assertEqual(j3.get("intent"), "list_watchlist")
        self.assertEqual(j3.get("status"), "needs_clarification")
        self.assertIn("Choose one: Open now, Cancel, or Back to chat.", str(j3.get("message") or ""))

    def test_open_now_recovery_command_executes_navigation(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-9", "history": []}},
        )
        j1 = res1.json()
        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-9"
        _ = self.client.post("/ai/command", json={"query": "what?", "context": ctx})
        res3 = self.client.post("/ai/command", json={"query": "open now", "context": ctx})
        j3 = res3.json()
        self.assertEqual(j3.get("intent"), "open_page")
        self.assertEqual(j3.get("status"), "ok")

    def test_morning_updates_question_answers_in_chat(self) -> None:
        res = self.client.post(
            "/ai/command",
            json={
                "query": "what is morning updates? what was important today",
                "context": {"session_id": "t-recovery-10", "history": []},
            },
        )
        j = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(j.get("intent"), "morning_updates")
        self.assertEqual(j.get("status"), "ok")
        self.assertIn("Morning updates", str(j.get("message") or ""))
        self.assertEqual(j.get("matched_by"), "react_tool_registry")

    def test_morning_updates_interrupts_watchlist_confirmation(self) -> None:
        r1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-11", "history": []}},
        )
        j1 = r1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")
        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-11"
        r2 = self.client.post(
            "/ai/command",
            json={"query": "what is in morning briefing today?", "context": ctx},
        )
        j2 = r2.json()
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(j2.get("intent"), "morning_updates")
        self.assertEqual(j2.get("status"), "ok")

    def test_morning_update_exact_question_never_prompts_open_page(self) -> None:
        res = self.client.post(
            "/ai/command",
            json={
                "query": "What is in the morning update?",
                "context": {"session_id": "t-recovery-12", "history": []},
            },
        )
        j = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(j.get("intent"), "morning_updates")
        self.assertEqual(j.get("status"), "ok")
        self.assertNotIn("reply with `yes` to open the page", str(j.get("message") or "").lower())

    def test_morning_updates_short_phrase_returns_summary_in_chat(self) -> None:
        res = self.client.post(
            "/ai/command",
            json={
                "query": "morning updates",
                "context": {"session_id": "t-recovery-13", "history": []},
            },
        )
        j = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(j.get("intent"), "morning_updates")
        self.assertEqual(j.get("status"), "ok")
        self.assertEqual(j.get("redirect_url"), "")

    def test_compare_question_bypasses_yes_no_trap(self) -> None:
        r1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-recovery-14", "history": []}},
        )
        j1 = r1.json()
        self.assertEqual(j1.get("intent"), "list_watchlist")
        self.assertEqual(j1.get("status"), "needs_confirmation")
        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-recovery-14"
        r2 = self.client.post(
            "/ai/command",
            json={"query": "CRM or HubSpot is better business?", "context": ctx},
        )
        j2 = r2.json()
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(j2.get("status"), "needs_confirmation")
        self.assertEqual(j2.get("intent"), "company_compare")
        self.assertEqual(j2.get("status"), "needs_input")


if __name__ == "__main__":
    unittest.main()
