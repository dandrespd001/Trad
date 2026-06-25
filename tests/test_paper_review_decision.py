import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperReviewDecisionTests(unittest.TestCase):
    def test_parser_defaults_for_paper_review_decision(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-review-decision",
                "--as-of-date",
                "2026-06-16",
                "--decision",
                "APPROVE_PAPER_CONFIRMATION",
                "--reviewer",
                "ops",
                "--reason",
                "readiness and paper evidence reviewed",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.decision, "APPROVE_PAPER_CONFIRMATION")
        self.assertEqual(args.reviewer, "ops")
        self.assertEqual(args.reason, "readiness and paper evidence reviewed")
        self.assertEqual(args.output_dir, "reports/tmp/paper_reviews")

    def test_approval_review_is_recorded_without_broker_or_live_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(review_args(root, decision="APPROVE_PAPER_CONFIRMATION"))
            payload = read_json(root / "reviews" / "2026-06-16" / "review.json")
            markdown = (root / "reviews" / "2026-06-16" / "review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "RECORDED")
        self.assertEqual(payload["decision"], "APPROVE_PAPER_CONFIRMATION")
        self.assertFalse(payload["authority"]["orders_submitted"])
        self.assertFalse(payload["authority"]["broker_client_built"])
        self.assertFalse(payload["authority"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertIn("Decision: **APPROVE_PAPER_CONFIRMATION**", markdown)

    def test_defer_and_reject_are_valid_review_decisions(self) -> None:
        for decision in ("DEFER", "REJECT"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)

                exit_code = main(review_args(root, decision=decision))
                payload = read_json(root / "reviews" / "2026-06-16" / "review.json")

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "RECORDED")
            self.assertEqual(payload["decision"], decision)

    def test_invalid_review_decision_writes_error_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(review_args(root, decision="CONTINUE"))
            payload = read_json(root / "reviews" / "2026-06-16" / "review.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_decision", error_codes(payload))

    def test_secret_like_reason_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            args = review_args(root, decision="DEFER")
            args[args.index("--reason") + 1] = "api_key=KEY secret_key=SECRET token=TOKEN"

            exit_code = main(args)
            output = (root / "reviews" / "2026-06-16" / "review.json").read_text(encoding="utf-8")
            markdown = (root / "reviews" / "2026-06-16" / "review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", markdown)
        self.assertNotIn("TOKEN", output)
        self.assertIn("[redacted]", output)


def review_args(root: Path, *, decision: str) -> list[str]:
    return [
        "paper-review-decision",
        "--as-of-date",
        "2026-06-16",
        "--decision",
        decision,
        "--reviewer",
        "ops-reviewer",
        "--reason",
        "paper evidence reviewed",
        "--output-dir",
        str(root / "reviews"),
    ]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def error_codes(payload: dict[str, Any]) -> set[str]:
    return {str(error["code"]) for error in payload["errors"]}


if __name__ == "__main__":
    unittest.main()
