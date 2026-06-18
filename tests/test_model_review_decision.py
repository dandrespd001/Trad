import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class ModelReviewDecisionTests(unittest.TestCase):
    def test_parser_defaults_for_model_review_decision(self) -> None:
        args = build_parser().parse_args(
            [
                "model-review-decision",
                "--challenger-report",
                "challenger.json",
                "--decision",
                "DEFER",
                "--reviewer",
                "ops",
                "--reason",
                "more paper evidence",
            ]
        )

        self.assertEqual(args.challenger_report, "challenger.json")
        self.assertEqual(args.decision, "DEFER")
        self.assertEqual(args.reviewer, "ops")
        self.assertEqual(args.reason, "more paper evidence")
        self.assertEqual(args.output_dir, "reports/tmp/model_challenger_decisions")

    def test_reviewable_report_can_be_approved_without_mutating_latest_model(self) -> None:
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = write_challenger_report(root / "challenger.json", status="REVIEWABLE")

            exit_code = main(review_args(report, root / "out", decision="APPROVE_FOR_NEXT_PAPER_CYCLE"))
            payload = read_json(root / "out" / "2026-06-18" / "decision.json")
            markdown = (root / "out" / "2026-06-18" / "decision.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "RECORDED")
        self.assertEqual(payload["decision"], "APPROVE_FOR_NEXT_PAPER_CYCLE")
        self.assertEqual(payload["challenger_report"]["status"], "REVIEWABLE")
        self.assertIn("sha256", payload["artifacts"]["challenger_report"])
        self.assertFalse(payload["authority"]["mutates_latest_model"])
        self.assertIn("Decision: **APPROVE_FOR_NEXT_PAPER_CYCLE**", markdown)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_rejected_report_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = write_challenger_report(root / "challenger.json", status="REJECTED")

            exit_code = main(review_args(report, root / "out", decision="APPROVE_FOR_NEXT_PAPER_CYCLE"))
            payload = read_json(root / "out" / "2026-06-18" / "decision.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("approve_requires_reviewable_report", error_codes(payload))

    def test_defer_accepts_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = write_challenger_report(root / "challenger.json", status="BLOCKED")

            exit_code = main(review_args(report, root / "out", decision="DEFER"))
            payload = read_json(root / "out" / "2026-06-18" / "decision.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "RECORDED")
        self.assertEqual(payload["decision"], "DEFER")

    def test_invalid_challenger_json_produces_error_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = root / "challenger.json"
            report.write_text("{bad json", encoding="utf-8")

            exit_code = main(review_args(report, root / "out", decision="DEFER"))
            outputs = list((root / "out").rglob("decision.json"))
            payload = read_json(outputs[0])

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_challenger_report", error_codes(payload))

    def test_secret_like_reason_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = write_challenger_report(root / "challenger.json", status="REVIEWABLE")
            args = review_args(report, root / "out", decision="DEFER")
            args[args.index("--reason") + 1] = "api_key=KEY secret_key=SECRET token=TOKEN"

            exit_code = main(args)
            output = (root / "out" / "2026-06-18" / "decision.json").read_text(encoding="utf-8")
            markdown = (root / "out" / "2026-06-18" / "decision.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", markdown)


def review_args(report: Path, output_dir: Path, *, decision: str) -> list[str]:
    return [
        "model-review-decision",
        "--challenger-report",
        str(report),
        "--decision",
        decision,
        "--reviewer",
        "ops-reviewer",
        "--reason",
        "paper evidence reviewed",
        "--output-dir",
        str(output_dir),
    ]


def write_challenger_report(path: Path, *, status: str) -> Path:
    write_json(
        path,
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-18T12:00:00+00:00",
            "status": status,
            "authority": {"mutates_latest_model": False, "automatic_champion_replacement": False},
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
    )
    return path


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def error_codes(payload: dict[str, object]) -> set[str]:
    return {str(error["code"]) for error in payload["errors"]}


if __name__ == "__main__":
    unittest.main()
