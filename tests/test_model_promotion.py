import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.models.promotion import (
    EconomicPromotionPolicy,
    PromotionPolicy,
    evaluate_economic_promotion,
    evaluate_promotion,
    rank_economic_candidates,
)


class ModelPromotionTests(unittest.TestCase):
    def test_rejects_model_that_does_not_exceed_baseline_accuracy_margin(self) -> None:
        decision = evaluate_promotion(
            challenger_metrics={"accuracy": 0.54, "sample_count": 100},
            baseline_metrics={"accuracy": 0.53},
            policy=PromotionPolicy(min_accuracy_lift=0.02, min_test_samples=30),
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient_accuracy_lift", decision.reasons)

    def test_rejects_model_with_insufficient_out_of_sample_evidence(self) -> None:
        decision = evaluate_promotion(
            challenger_metrics={"accuracy": 0.80, "sample_count": 10},
            baseline_metrics={"accuracy": 0.50},
            policy=PromotionPolicy(min_accuracy_lift=0.02, min_test_samples=30),
        )

        self.assertFalse(decision.approved)
        self.assertIn("insufficient_test_samples", decision.reasons)

    def test_approves_challenger_that_clears_policy(self) -> None:
        decision = evaluate_promotion(
            challenger_metrics={"accuracy": 0.57, "sample_count": 100},
            baseline_metrics={"accuracy": 0.53},
            policy=PromotionPolicy(min_accuracy_lift=0.02, min_test_samples=30),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.actions, ("eligible_for_paper_challenger",))

    def test_promote_cli_writes_decision_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run = root / "run.json"
            baseline = root / "baseline.json"
            output = root / "promotion.json"
            run.write_text(
                json.dumps({"metrics": {"test": {"accuracy": 0.57, "sample_count": 100}}}),
                encoding="utf-8",
            )
            baseline.write_text(json.dumps({"accuracy": 0.53}), encoding="utf-8")

            exit_code = main(
                [
                    "promote",
                    "--run-id",
                    str(run),
                    "--baseline",
                    str(baseline),
                    "--output",
                    str(output),
                    "--min-accuracy-lift",
                    "0.02",
                    "--min-test-samples",
                    "30",
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["approved"])
        self.assertEqual(payload["actions"], ["eligible_for_paper_challenger"])

    def test_economic_ranking_prefers_calmar_over_accuracy(self) -> None:
        candidates = [
            {
                "candidate_id": "high_accuracy_bad_calmar",
                "metrics": {
                    "accuracy": 0.70,
                    "calmar": 0.5,
                    "net_return_after_costs": 0.04,
                    "max_drawdown": 0.08,
                    "turnover": 30.0,
                    "estimated_costs": 0.01,
                    "trade_count": 40.0,
                    "walk_forward_stability": 0.80,
                },
            },
            {
                "candidate_id": "lower_accuracy_better_calmar",
                "metrics": {
                    "accuracy": 0.55,
                    "calmar": 2.0,
                    "net_return_after_costs": 0.08,
                    "max_drawdown": 0.04,
                    "turnover": 35.0,
                    "estimated_costs": 0.01,
                    "trade_count": 42.0,
                    "walk_forward_stability": 0.80,
                },
            },
        ]

        ranked = rank_economic_candidates(candidates)

        self.assertEqual(ranked[0]["candidate_id"], "lower_accuracy_better_calmar")
        self.assertEqual(ranked[0]["economic_rank"], 1)

    def test_economic_gate_blocks_negative_net_return(self) -> None:
        decision = evaluate_economic_promotion(
            metrics={
                "net_return_after_costs": -0.01,
                "max_drawdown": 0.04,
                "turnover": 10.0,
                "estimated_costs": 0.01,
                "trade_count": 50.0,
                "walk_forward_stability": 0.80,
            },
            policy=EconomicPromotionPolicy(min_trade_count=20, min_walk_forward_stability=0.50),
        )

        self.assertFalse(decision.reviewable)
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("net_return_after_costs_not_positive", decision.reasons)

    def test_economic_gate_blocks_drawdown_costs_and_turnover_limits(self) -> None:
        decision = evaluate_economic_promotion(
            metrics={
                "net_return_after_costs": 0.10,
                "max_drawdown": 0.20,
                "turnover": 250.0,
                "estimated_costs": 0.08,
                "trade_count": 50.0,
                "walk_forward_stability": 0.80,
            },
            policy=EconomicPromotionPolicy(
                max_drawdown_pct=0.12,
                max_turnover=200.0,
                max_estimated_costs=0.05,
                min_trade_count=20,
                min_walk_forward_stability=0.50,
            ),
        )

        self.assertFalse(decision.reviewable)
        self.assertIn("max_drawdown_above_limit", decision.reasons)
        self.assertIn("turnover_above_limit", decision.reasons)
        self.assertIn("estimated_costs_above_limit", decision.reasons)

    def test_economic_gate_marks_stable_walk_forward_candidate_reviewable(self) -> None:
        decision = evaluate_economic_promotion(
            metrics={
                "net_return_after_costs": 0.10,
                "max_drawdown": 0.05,
                "turnover": 80.0,
                "estimated_costs": 0.02,
                "trade_count": 30.0,
                "walk_forward_stability": 0.75,
            },
            policy=EconomicPromotionPolicy(
                max_drawdown_pct=0.12,
                max_turnover=200.0,
                max_estimated_costs=0.05,
                min_trade_count=20,
                min_walk_forward_stability=0.50,
            ),
        )

        self.assertTrue(decision.reviewable)
        self.assertEqual(decision.status, "REVIEWABLE")
        self.assertEqual(decision.actions, ("review_paper_challenger",))

    def test_economic_gate_blocks_low_calmar_and_insufficient_oos_windows(self) -> None:
        decision = evaluate_economic_promotion(
            metrics={
                "net_return_after_costs": 0.02,
                "max_drawdown": 0.04,
                "turnover": 80.0,
                "estimated_costs": 0.02,
                "trade_count": 90.0,
                "walk_forward_stability": 0.70,
                "walk_forward_window_count": 2.0,
            },
            policy=EconomicPromotionPolicy(
                min_calmar=0.75,
                max_drawdown_pct=0.12,
                max_turnover=200.0,
                max_estimated_costs=0.05,
                min_trade_count=80,
                min_walk_forward_stability=0.60,
                min_oos_windows=3,
            ),
        )

        self.assertFalse(decision.reviewable)
        self.assertIn("calmar_below_minimum", decision.reasons)
        self.assertIn("insufficient_oos_windows", decision.reasons)


if __name__ == "__main__":
    unittest.main()
