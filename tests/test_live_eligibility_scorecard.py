import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.research.live_eligibility_scorecard import (
    build_live_eligibility_scorecard,
    write_live_eligibility_scorecard,
)


class LiveEligibilityScorecardTests(unittest.TestCase):
    def test_blocks_missing_benchmark_costs_and_oos_period(self) -> None:
        scorecard = build_live_eligibility_scorecard(
            data_cutoff="2026-06-25",
            timezone="America/New_York",
            universe=["SPY"],
            benchmark=None,
            fees_bps=None,
            slippage_bps=None,
            estimated_edge_bps=10.0,
            max_drawdown=0.08,
            turnover=4.0,
            exposure=0.5,
            hit_rate=0.55,
            sharpe=1.2,
            oos_period=None,
            leakage_checks={"feature_cutoff_enforced": True},
        )

        self.assertFalse(scorecard["eligible_for_live_review"])
        self.assertIn("benchmark_missing", scorecard["blockers"])
        self.assertIn("fees_bps_missing", scorecard["blockers"])
        self.assertIn("slippage_bps_missing", scorecard["blockers"])
        self.assertIn("oos_period_missing", scorecard["blockers"])

    def test_blocks_non_positive_net_edge_after_costs(self) -> None:
        scorecard = build_live_eligibility_scorecard(
            data_cutoff="2026-06-25",
            timezone="America/New_York",
            universe=["SPY"],
            benchmark="SPY buy-and-hold",
            fees_bps=3.0,
            slippage_bps=4.0,
            estimated_edge_bps=7.0,
            max_drawdown=0.08,
            turnover=4.0,
            exposure=0.5,
            hit_rate=0.55,
            sharpe=1.2,
            oos_period={"start": "2026-01-01", "end": "2026-06-25"},
            leakage_checks={"feature_cutoff_enforced": True, "target_shifted": True},
        )

        self.assertEqual(scorecard["net_edge_bps"], 0.0)
        self.assertIn("edge_not_positive_after_costs", scorecard["blockers"])
        self.assertFalse(scorecard["eligible_for_live_review"])

    def test_blocks_failed_leakage_check(self) -> None:
        scorecard = build_live_eligibility_scorecard(
            data_cutoff="2026-06-25",
            timezone="America/New_York",
            universe=["SPY"],
            benchmark="SPY buy-and-hold",
            fees_bps=1.0,
            slippage_bps=1.0,
            estimated_edge_bps=20.0,
            max_drawdown=0.08,
            turnover=4.0,
            exposure=0.5,
            hit_rate=0.55,
            sharpe=1.2,
            oos_period={"start": "2026-01-01", "end": "2026-06-25"},
            leakage_checks={"feature_cutoff_enforced": False},
        )

        self.assertIn("leakage_check_failed:feature_cutoff_enforced", scorecard["blockers"])
        self.assertFalse(scorecard["eligible_for_live_review"])

    def test_valid_scorecard_writes_json_and_markdown_without_live_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = write_live_eligibility_scorecard(
                output_dir=Path(tmp),
                as_of_date="2026-06-25",
                data_cutoff="2026-06-25",
                timezone="America/New_York",
                universe=["SPY", "TLT"],
                benchmark="60/40 SPY/TLT buy-and-hold",
                fees_bps=1.0,
                slippage_bps=1.0,
                estimated_edge_bps=15.0,
                max_drawdown=0.08,
                turnover=4.0,
                exposure=0.5,
                hit_rate=0.55,
                sharpe=1.2,
                oos_period={"start": "2026-01-01", "end": "2026-06-25"},
                leakage_checks={"feature_cutoff_enforced": True, "target_shifted": True},
                assumptions=["fills use conservative close-to-close costs"],
                failure_modes=["market regime shift"],
            )

            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            markdown = result.markdown_path.read_text(encoding="utf-8")

        self.assertTrue(payload["eligible_for_live_review"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["net_edge_bps"], 13.0)
        self.assertEqual(payload["safety"]["orders_submitted"], False)
        self.assertEqual(payload["safety"]["live_trading_authorized"], False)
        self.assertIn("Live Eligibility Scorecard", markdown)
        self.assertIn("eligible_for_live_review: true", markdown)


if __name__ == "__main__":
    unittest.main()
