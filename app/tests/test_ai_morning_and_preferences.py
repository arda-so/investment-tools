import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.user_preferences_service import learn_preferences_from_text


class TestAIMorningAndPreferences(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_morning_brief_prefers_cached_snapshot(self) -> None:
        cached = {
            "ok": True,
            "asof": "2026-02-18T08:00:00",
            "top_holdings": ["CRM"],
            "bullets": ["Cached morning brief line."],
        }
        with patch("app.routers.ai.get_cached_morning_brief", return_value=cached), patch(
            "app.routers.ai.save_morning_brief_snapshot"
        ) as save_mock:
            res = self.client.get("/ai/morning_brief")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("bullets"), ["Cached morning brief line."])
        save_mock.assert_not_called()

    def test_preference_learning_concise_and_no_crypto(self) -> None:
        saved = learn_preferences_from_text("Be concise. I hate crypto ideas.")
        keys = {str(x.get("key") or "") for x in saved}
        self.assertIn("concise_summaries_only", keys)
        self.assertIn("avoid_crypto_default", keys)


if __name__ == "__main__":
    unittest.main()
