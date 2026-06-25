import unittest
from pathlib import Path

from trading_ai.config import load_yaml_file


class IntradayCanaryPolicyTests(unittest.TestCase):
    def test_risk_config_records_intraday_economic_gate_and_future_canary_limits(self) -> None:
        risk = load_yaml_file("configs/risk.yml")

        intraday = risk["model_quality"]["intraday"]

        self.assertEqual(intraday["frequency"], "1h")
        self.assertEqual(intraday["primary_metric"], "calmar")
        self.assertEqual(intraday["min_calmar"], 0.75)
        self.assertEqual(intraday["min_oos_windows"], 3)
        self.assertGreater(intraday["min_trade_count"], 0)
        self.assertGreater(intraday["min_walk_forward_stability"], 0.0)
        self.assertEqual(intraday["initial_live_canary_usd_per_day"], 5.0)
        self.assertEqual(intraday["max_live_canary_usd_per_day"], 5.0)
        self.assertFalse(intraday["live_orders_enabled"])

    def test_runbook_documents_canary_is_deferred_until_clean_intraday_paper_review(self) -> None:
        runbook = Path("docs/paper-real-runbook.md").read_text(encoding="utf-8")

        self.assertIn("USD 5/day", runbook)
        self.assertIn("primer canary", runbook)
        self.assertIn("paper intraday", runbook)
        self.assertIn("does not enable live orders", runbook)


if __name__ == "__main__":
    unittest.main()
