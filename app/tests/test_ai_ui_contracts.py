import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


class TestAIUIContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_navigation_action_contract(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "show me my watchlist", "context": {"session_id": "t-ui-nav-1", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(j1.get("status"), "needs_confirmation")

        ctx = j1.get("context") or {}
        ctx["session_id"] = "t-ui-nav-1"
        res2 = self.client.post("/ai/command", json={"query": "yes", "context": ctx})
        j2 = res2.json()

        self.assertEqual(res2.status_code, 200)
        self.assertEqual(j2.get("status"), "ok")
        self.assertEqual(j2.get("intent"), "open_page")
        self.assertEqual((j2.get("action") or {}).get("type"), "NAVIGATE")
        self.assertEqual((j2.get("action") or {}).get("payload"), "/my_universe?tab=all")

    def test_tasks_confirmation_navigation_action_contract(self) -> None:
        res1 = self.client.post(
            "/ai/command",
            json={"query": "tell me my to do and tasks", "context": {"session_id": "t-ui-nav-2", "history": []}},
        )
        j1 = res1.json()
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(j1.get("intent"), "list_tasks")
        self.assertEqual(j1.get("status"), "ok")
        self.assertEqual((j1.get("action") or {}).get("type"), "NONE")
        self.assertEqual(((j1.get("ui") or {}).get("type") or ""), "task_list")

    def test_frontend_silent_navigation_and_task_markdown_hooks_exist(self) -> None:
        html = Path("app/templates/base.html").read_text(encoding="utf-8")

        # Task cards must render task text through markdown parser.
        self.assertIn("renderMarkdown(it.task || '')", html)

        # Silent navigation contract: return before assistant bubble push.
        nav_idx = html.find("if(navTarget){")
        push_idx = html.find("aiThread.push(assistantMsg);")
        self.assertTrue(nav_idx >= 0 and push_idx >= 0 and nav_idx < push_idx)
        self.assertIn("window.location.assign(navTarget)", html)
        self.assertIn("return;", html[nav_idx:push_idx])
        self.assertIn("data-rec-action=\"open_now\"", html)
        self.assertIn("data-rec-action=\"cancel\"", html)
        self.assertIn("data-rec-action=\"back_chat\"", html)
        self.assertIn("id=\"aiImageBtn\"", html)
        self.assertIn("id=\"aiImageInput\"", html)
        self.assertIn("id=\"aiUploadThumb\"", html)
        self.assertIn("id=\"aiVoiceBtn\"", html)
        self.assertIn("id=\"aiSpeakToggle\"", html)
        self.assertIn("id=\"aiVoiceLoopToggle\"", html)
        self.assertIn("id=\"aiVoiceRate\"", html)
        self.assertIn("SpeechRecognition", html)
        self.assertIn("speechSynthesis", html)
        self.assertIn("maybeResumeVoiceLoop", html)
        self.assertIn("scheduleVoiceSubmit", html)
        self.assertIn("image_data_url", html)
        self.assertIn("drop-active", html)
        self.assertIn("attachImageFile", html)

    def test_persona_prompt_contract_exists(self) -> None:
        orchestrator = Path("app/services/ai_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("Senior Investment Strategist and Product Architect", orchestrator)
        self.assertIn("Keep private reasoning internal; do not expose it.", orchestrator)

    def test_react_ui_payload_exists_for_morning_update(self) -> None:
        res = self.client.post(
            "/ai/command",
            json={"query": "What is in the morning update?", "context": {"session_id": "t-ui-react-1", "history": []}},
        )
        j = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(j.get("intent"), "morning_updates")
        ui = j.get("ui") or {}
        self.assertEqual(ui.get("type"), "brief_card")


if __name__ == "__main__":
    unittest.main()
