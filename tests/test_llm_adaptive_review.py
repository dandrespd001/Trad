import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class LlmAdaptiveReviewTests(unittest.TestCase):
    def test_adaptive_review_accumulates_clean_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "feedback.jsonl"
            ledger.write_text(
                json.dumps({"role_id": "paper_ops_reviewer", "human_corrected": False}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            eval_report = write_eval(root / "eval.json", state="PASSED")

            exit_code = main(
                [
                    "llm-adaptive-review",
                    "--role",
                    "paper_ops_reviewer",
                    "--feedback-ledger",
                    str(ledger),
                    "--eval-report",
                    str(eval_report),
                    "--output-dir",
                    str(root / "adaptive"),
                ]
            )
            payload = read_json(root / "adaptive" / "paper_ops_reviewer" / "adaptive_review.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["adaptive_state"], "ACCUMULATING")
        self.assertEqual(payload["feedback_count"], 1)
        self.assertEqual(payload["authority"]["llm_authority"], "none")

    def test_adaptive_review_marks_ready_for_supervision_on_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "feedback.jsonl"
            ledger.write_text(
                "\n".join(
                    json.dumps({"role_id": "paper_ops_reviewer", "human_corrected": True}, sort_keys=True)
                    for _ in range(3)
                )
                + "\n",
                encoding="utf-8",
            )
            eval_report = write_eval(root / "eval.json", state="FAILED")

            exit_code = main(
                [
                    "llm-adaptive-review",
                    "--role",
                    "paper_ops_reviewer",
                    "--feedback-ledger",
                    str(ledger),
                    "--eval-report",
                    str(eval_report),
                    "--output-dir",
                    str(root / "adaptive"),
                ]
            )
            payload = read_json(root / "adaptive" / "paper_ops_reviewer" / "adaptive_review.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["adaptive_state"], "READY_FOR_SUPERVISION")
        self.assertEqual(payload["correction_count"], 3)


def write_eval(path: Path, *, state: str) -> Path:
    path.write_text(
        json.dumps({"eval_state": state, "metrics": {"schema_pass_rate": 1.0}}, sort_keys=True), encoding="utf-8"
    )
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
