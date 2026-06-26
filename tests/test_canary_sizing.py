import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.position_sizing import (
    SizingDecision,
    build_canary_sizing_decision,
    write_canary_sizing_report,
)


class CanarySizingTests(unittest.TestCase):
    def test_decision_tracks_units_and_caps_live_canary_to_one_dollar(self) -> None:
        decision = build_canary_sizing_decision(
            bankroll_usd=10_000.0,
            risk_budget_pct=0.001,
            stop_loss_pct=0.02,
            slippage_bps=10.0,
            cost_bps=5.0,
            fixed_fees_usd=0.01,
            expected_edge_bps=50.0,
            stage_cap_usd=1.0,
        )

        self.assertIsInstance(decision, SizingDecision)
        self.assertEqual(decision.notional_usd, 1.0)
        self.assertEqual(decision.cap_usd, 1.0)
        self.assertEqual(decision.bankroll_usd, 10_000.0)
        self.assertEqual(decision.stop_loss_pct, 0.02)
        self.assertEqual(decision.slippage_bps, 10.0)
        self.assertAlmostEqual(decision.slippage_usd, 0.001)
        self.assertAlmostEqual(decision.cost_usd, 0.0005)
        self.assertAlmostEqual(decision.fees_usd, 0.01)
        self.assertAlmostEqual(decision.expected_edge_usd, 0.005)
        self.assertLess(decision.net_edge_usd, 0.0)
        self.assertIn("edge_net_not_positive", decision.blockers)
        self.assertIn("USD 1", decision.rationale)

    def test_positive_edge_keeps_future_scale_up_conditional(self) -> None:
        decision = build_canary_sizing_decision(
            bankroll_usd=10_000.0,
            risk_budget_pct=0.001,
            stop_loss_pct=0.02,
            slippage_bps=1.0,
            cost_bps=1.0,
            fixed_fees_usd=0.0,
            expected_edge_bps=50.0,
            stage_cap_usd=100.0,
        )

        self.assertEqual(decision.notional_usd, 1.0)
        self.assertEqual(decision.cap_usd, 100.0)
        self.assertAlmostEqual(decision.max_loss_usd, 0.02)
        self.assertGreater(decision.net_edge_usd, 0.0)
        self.assertEqual(decision.blockers, [])
        self.assertEqual(decision.future_scale_range_usd, [50.0, 100.0])

    def test_invalid_stop_or_bankroll_blocks(self) -> None:
        decision = build_canary_sizing_decision(
            bankroll_usd=0.0,
            risk_budget_pct=0.001,
            stop_loss_pct=0.0,
            slippage_bps=1.0,
            cost_bps=1.0,
            fixed_fees_usd=0.0,
            expected_edge_bps=50.0,
            stage_cap_usd=100.0,
        )

        self.assertIn("bankroll_usd_invalid", decision.blockers)
        self.assertIn("stop_loss_pct_invalid", decision.blockers)
        self.assertEqual(decision.notional_usd, 0.0)

    def test_write_canary_sizing_report_outputs_json_and_markdown_without_submit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = write_canary_sizing_report(
                output_dir=Path(tmp),
                as_of_date="2026-06-25",
                bankroll_usd=10_000.0,
                risk_budget_pct=0.001,
                stop_loss_pct=0.02,
                slippage_bps=1.0,
                cost_bps=1.0,
                fixed_fees_usd=0.0,
                expected_edge_bps=50.0,
                stage_cap_usd=100.0,
            )
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            markdown = result.markdown_path.read_text(encoding="utf-8")

        self.assertEqual(payload["recommendation"]["first_live_notional_usd"], 1.0)
        self.assertEqual(payload["recommendation"]["future_scale_range_usd"], [50.0, 100.0])
        self.assertEqual(payload["safety"]["orders_submitted"], False)
        self.assertEqual(payload["safety"]["live_trading_authorized"], False)
        self.assertIn("Canary Sizing", markdown)
        self.assertIn("USD 1", markdown)


if __name__ == "__main__":
    unittest.main()
