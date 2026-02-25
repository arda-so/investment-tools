import unittest

from app.services.ai_orchestrator import ensure_ai_schema, get_adaptive_turn_policy_snapshot, get_ai_quality_report


class TestAIQualityReportAPI(unittest.TestCase):
    def test_quality_report_function_returns_shape(self):
        ensure_ai_schema()
        data = get_ai_quality_report(hours=24, limit=50)
        self.assertIn("window_hours", data)
        self.assertIn("total_events", data)
        self.assertIn("by_event", data)
        self.assertIn("recent", data)
        self.assertIsInstance(data.get("by_event"), list)
        self.assertIsInstance(data.get("recent"), list)

    def test_adaptive_policy_snapshot_shape_and_bounds(self):
        ensure_ai_schema()
        snap = get_adaptive_turn_policy_snapshot(force_refresh=True)
        self.assertIn("thresholds", snap)
        th = snap.get("thresholds") if isinstance(snap.get("thresholds"), dict) else {}
        self.assertIn("force_action_min_conf", th)
        self.assertIn("decision_action_min_conf", th)
        self.assertIn("clarify_min_conf", th)
        self.assertIn("mutation_min_conf", th)
        self.assertGreaterEqual(float(th.get("force_action_min_conf") or 0.0), 0.35)
        self.assertLessEqual(float(th.get("decision_action_min_conf") or 1.0), 0.82)


if __name__ == "__main__":
    unittest.main()
