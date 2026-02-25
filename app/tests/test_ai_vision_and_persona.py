import base64
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class TestAIVisionAndPersona(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_image_upload_routes_to_vision_intent(self) -> None:
        tiny_png = base64.b64encode(b"fake_image_bytes_for_test").decode("ascii")
        data_url = f"data:image/png;base64,{tiny_png}"
        with patch("app.routers.ai.ask_ai_vision", return_value="Pattern looks like consolidation with rising volume."):
            res = self.client.post(
                "/ai/command",
                json={
                    "query": "what does this chart suggest?",
                    "image_data_url": data_url,
                    "context": {"session_id": "t-vision-1", "history": []},
                },
            )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body.get("intent"), "vision_analysis")
        self.assertEqual(body.get("status"), "ok")
        self.assertTrue(str(body.get("message") or "").startswith("Pattern looks like"))


if __name__ == "__main__":
    unittest.main()
