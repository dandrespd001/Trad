import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperEvidenceIndexTests(unittest.TestCase):
    def test_parser_defaults_for_evidence_index(self) -> None:
        args = build_parser().parse_args(["paper-evidence-index", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness_root, "reports/tmp/paper_daily_prepare")
        self.assertEqual(args.output_dir, "reports/tmp/paper_evidence_index")

    def test_complete_evidence_index_produces_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_evidence(root)

            exit_code = main(index_args(root))
            payload = read_json(root / "index" / "2026-06-16" / "evidence_index.json")
            markdown = (root / "index" / "2026-06-16" / "evidence_index.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["artifacts"]["readiness"]["status"], "READY")
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "OK")
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertIn("Status: **OK**", markdown)

    def test_missing_optional_statement_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_evidence(root)
            statement = root / "statements" / "2026-06-16" / "statement.normalized.json"
            statement.unlink()

            exit_code = main(index_args(root))
            payload = read_json(root / "index" / "2026-06-16" / "evidence_index.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("missing_statement", issue_codes(payload))

    def test_missing_weekly_summary_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_evidence(root)
            weekly = root / "weekly" / "2026-W25" / "weekly_summary.json"
            weekly.unlink()

            exit_code = main(index_args(root))
            payload = read_json(root / "index" / "2026-06-16" / "evidence_index.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("missing_weekly_summary", issue_codes(payload))

    def test_required_invalid_json_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_evidence(root)
            (root / "readiness" / "2026-06-16" / "readiness.json").write_text("{bad json", encoding="utf-8")

            exit_code = main(index_args(root))
            payload = read_json(root / "index" / "2026-06-16" / "evidence_index.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_readiness_json", issue_codes(payload))

    def test_secret_like_values_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_evidence(root)
            write_json(
                root / "decisions" / "2026-06-16" / "decision.json",
                {"decision": "CONTINUE", "reason": "api_key=KEY secret_key=SECRET token=TOKEN"},
            )

            exit_code = main(index_args(root))
            output = (root / "index" / "2026-06-16" / "evidence_index.json").read_text(encoding="utf-8")
            markdown = (root / "index" / "2026-06-16" / "evidence_index.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", markdown)


def index_args(root: Path) -> list[str]:
    return [
        "paper-evidence-index",
        "--as-of-date",
        "2026-06-16",
        "--readiness-root",
        str(root / "readiness"),
        "--monitor-root",
        str(root / "monitor"),
        "--campaign-root",
        str(root / "campaign"),
        "--decisions-root",
        str(root / "decisions"),
        "--performance-root",
        str(root / "performance"),
        "--ops-root",
        str(root / "ops"),
        "--weekly-root",
        str(root / "weekly"),
        "--statement-root",
        str(root / "statements"),
        "--challenger-decisions-root",
        str(root / "challenger_decisions"),
        "--output-dir",
        str(root / "index"),
    ]


def write_complete_evidence(root: Path) -> None:
    write_json(root / "readiness" / "2026-06-16" / "readiness.json", {"status": "READY", "as_of_date": "2026-06-16"})
    write_json(root / "monitor" / "2026-06-16" / "monitor.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(root / "campaign" / "2026-06-16" / "campaign.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(
        root / "decisions" / "2026-06-16" / "decision.json", {"decision": "CONTINUE", "as_of_date": "2026-06-16"}
    )
    write_json(root / "performance" / "2026-06-16" / "performance.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(root / "ops" / "2026-06-16" / "ops_check.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(root / "weekly" / "2026-W25" / "weekly_summary.json", {"status": "OK", "week": "2026-W25"})
    write_json(
        root / "statements" / "2026-06-16" / "statement.normalized.json", {"status": "OK", "as_of_date": "2026-06-16"}
    )
    write_json(
        root / "challenger_decisions" / "2026-06-16" / "decision.json", {"status": "RECORDED", "decision": "DEFER"}
    )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def issue_codes(payload: dict[str, object]) -> set[str]:
    return {str(issue["code"]) for issue in payload["issues"]}


if __name__ == "__main__":
    unittest.main()
