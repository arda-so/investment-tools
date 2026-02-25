import time
import unittest

from app.services.portfolio_memory_service import (
    list_active_rules,
    list_rule_candidates,
    remember_compact_memory,
    run_learning_cycle,
)


class TestAILearningV1(unittest.TestCase):
    def test_learning_cycle_promotes_rule(self):
        txt = f"always protect downside first {int(time.time())}"
        out = remember_compact_memory(txt, bucket="process_rule", source="test", reliability=0.95)
        self.assertTrue(bool(out.get("ok")))

        # Run cycle multiple times to build support and trigger promotion.
        promoted_any = False
        for _ in range(6):
            cyc = run_learning_cycle()
            self.assertTrue(bool(cyc.get("ok")))
            if int(cyc.get("promoted") or 0) > 0:
                promoted_any = True

        cands = list_rule_candidates(limit=120)
        self.assertTrue(any(txt in str(c.get("rule_text") or "") for c in cands))

        rules = list_active_rules(limit=120)
        self.assertTrue(any(txt in str(r.get("rule_text") or "") for r in rules) or promoted_any)


if __name__ == "__main__":
    unittest.main()
