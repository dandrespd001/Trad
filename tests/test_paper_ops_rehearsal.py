import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperOpsRehearsalTests(unittest.TestCase):
    def test_parser_defaults_for_paper_ops_rehearsal(self) -> None:
        args = build_parser().parse_args(["paper-ops-rehearsal", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.scenario, "complete")
        self.assertEqual(args.output_dir, "reports/tmp/paper_rehearsal")

    def test_complete_week_rehearsal_produces_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="complete"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")
            markdown = (root / "rehearsal" / "2026-06-16" / "rehearsal.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["scenario"], "complete")
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "OK")
        self.assertEqual(payload["artifacts"]["weekly_summary"]["status"], "OK")
        self.assertEqual(payload["artifacts"]["model_review_decision"]["status"], "RECORDED")
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertIn("Status: **OK**", markdown)

    def test_missing_performance_rehearsal_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="missing-performance"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("performance_skipped", payload["warnings"])
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "WARN")

    def test_stop_day_rehearsal_produces_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="stop"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "CRITICAL")

    def test_invalid_statement_rehearsal_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="invalid-statement"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["artifacts"]["statement_validate"]["status"], "ERROR")


def rehearsal_args(root: Path, *, scenario: str) -> list[str]:
    return [
        "paper-ops-rehearsal",
        "--as-of-date",
        "2026-06-16",
        "--scenario",
        scenario,
        "--output-dir",
        str(root / "rehearsal"),
    ]


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
