import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class ModelReviewCycleReportTests(unittest.TestCase):
    def test_parser_defaults_for_model_review_cycle_report(self) -> None:
        args = build_parser().parse_args(
            [
                "model-review-cycle-report",
                "--challenger-report",
                "challenger.json",
                "--review-decision",
                "decision.json",
            ]
        )

        self.assertEqual(args.challenger_report, "challenger.json")
        self.assertEqual(args.review_decision, "decision.json")
        self.assertEqual(args.output_dir, "reports/tmp/model_challenger_cycles")

    def test_approved_cycle_report_recommends_next_paper_cycle_without_mutating_champion(self) -> None:
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            challenger = write_challenger(root / "challenger.json", status="REVIEWABLE")
            decision = write_decision(root / "decision.json", decision="APPROVE_FOR_NEXT_PAPER_CYCLE")

            exit_code = main(cycle_args(challenger, decision, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "cycle_report.json")
            markdown = (root / "out" / "2026-06-18" / "cycle_report.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["recommended_next_state"], "READY_FOR_NEXT_PAPER_CYCLE")
        self.assertIn("sha256", payload["artifacts"]["challenger_report"])
        self.assertFalse(payload["authority"]["mutates_latest_model"])
        self.assertIn("READY_FOR_NEXT_PAPER_CYCLE", markdown)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_reject_cycle_report_is_recorded_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            challenger = write_challenger(root / "challenger.json", status="REJECTED")
            decision = write_decision(root / "decision.json", decision="REJECT")

            exit_code = main(cycle_args(challenger, decision, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "cycle_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["recommended_next_state"], "REJECTED_NO_PROMOTION")

    def test_defer_cycle_report_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            challenger = write_challenger(root / "challenger.json", status="BLOCKED")
            decision = write_decision(root / "decision.json", decision="DEFER")

            exit_code = main(cycle_args(challenger, decision, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "cycle_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["recommended_next_state"], "DEFERRED")

    def test_invalid_artifact_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            challenger = root / "challenger.json"
            challenger.write_text("{bad json", encoding="utf-8")
            decision = write_decision(root / "decision.json", decision="DEFER")

            exit_code = main(cycle_args(challenger, decision, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "cycle_report.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_challenger_report", error_codes(payload))


def cycle_args(challenger: Path, decision: Path, output_dir: Path) -> list[str]:
    return [
        "model-review-cycle-report",
        "--challenger-report",
        str(challenger),
        "--review-decision",
        str(decision),
        "--output-dir",
        str(output_dir),
    ]


def write_challenger(path: Path, *, status: str) -> Path:
    write_json(path, {"generated_at": "2026-06-18T12:00:00+00:00", "status": status})
    return path


def write_decision(path: Path, *, decision: str) -> Path:
    write_json(
        path,
        {
            "generated_at": "2026-06-18T13:00:00+00:00",
            "decision_date": "2026-06-18",
            "status": "RECORDED",
            "decision": decision,
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
