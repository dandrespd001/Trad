import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.models.promotion import PromotionPolicy, evaluate_promotion


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


if __name__ == "__main__":
    unittest.main()
