import unittest

from app.services.ai_orchestrator import run_ai_command


class TestAIConversationRegressions(unittest.TestCase):
    def test_correction_message_acks_instead_of_misrouting(self):
        res = run_ai_command("not this but this question indeed better for us", {})
        self.assertEqual(res.intent, "correction_ack")
        self.assertIn("what should i do instead", str(res.message).lower())

    def test_compare_phrase_uses_expected_entities(self):
        ctx = {
            "last_intent": "list_watchlist",
            "history": [
                {
                    "role": "assistant",
                    "intent": "list_watchlist",
                    "status": "needs_confirmation",
                    "text": "Please reply with `yes` to open the page or `no` to stay in chat.",
                }
            ],
        }
        res = run_ai_command("CRM OR HUBSPOT IS BETTER BUSINESS?", ctx)
        self.assertEqual(res.intent, "company_compare")
        msg = str(res.message or "")
        self.assertIn("CRM", msg)
        self.assertIn("HUBS", msg)
        self.assertNotIn(" vs IS ", msg)

    def test_blue_chip_remove_ignores_control_word_as_ticker(self):
        res = run_ai_command("NO REMOVE CRM FROM BLUE CHIPS", {})
        self.assertEqual(res.intent, "list_blue_chips")
        self.assertNotIn("NO", str(res.message or ""))

    def test_mutation_actions_route_through_single_gateway(self):
        res = run_ai_command("add task", {})
        self.assertEqual(res.intent, "add_task")
        traces = list(res.traces or [])
        route_details = [str(t.get("detail") or "") for t in traces if str(t.get("step") or "") == "route"]
        self.assertTrue(any(d.startswith("mutation_gateway:add_task") for d in route_details))
        ui = dict(res.ui or {})
        ex = ui.get("execution") if isinstance(ui.get("execution"), dict) else {}
        fc = ex.get("function_call") if isinstance(ex.get("function_call"), dict) else {}
        self.assertEqual(str(fc.get("name") or ""), "add_task")

    def test_candidate_arbiter_handles_natural_task_phrase(self):
        res = run_ai_command("can you remind me to register to university by thursday", {})
        self.assertEqual(res.intent, "add_task")
        traces = list(res.traces or [])
        self.assertTrue(any(str(t.get("step") or "") == "arb.action" and str(t.get("detail") or "") == "add_task" for t in traces))


if __name__ == "__main__":
    unittest.main()
