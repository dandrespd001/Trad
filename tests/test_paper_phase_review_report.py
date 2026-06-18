import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperPhaseReviewReportTests(unittest.TestCase):
    def test_parser_defaults_for_phase_review_report_are_review_only(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-phase-review-report",
                "--as-of-date",
                "2026-06-16",
                "--campaign-report",
                "campaign.json",
                "--performance-report",
                "performance.json",
                "--operator-status",
                "operator.json",
                "--strategy-quality",
                "quality.json",
                "--evidence-index",
                "evidence.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.campaign_report, "campaign.json")
        self.assertEqual(args.performance_report, "performance.json")
        self.assertEqual(args.operator_status, "operator.json")
        self.assertEqual(args.strategy_quality, "quality.json")
        self.assertEqual(args.evidence_index, "evidence.json")
        self.assertIsNone(args.weekly_summary)
        self.assertEqual(args.min_stable_sessions, 60)
        self.assertIsNone(args.trial_day_root)
        self.assertEqual(args.output_dir, "reports/tmp/paper_phase_review")

    def test_phase_review_accumulates_at_fifty_nine_stable_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifacts = write_clean_phase_inputs(root, stable_sessions=59)

            exit_code = main(phase_args(root, artifacts))
            payload = read_json(root / "phase" / "2026-06-16" / "phase_review.json")
            markdown = (root / "phase" / "2026-06-16" / "phase_review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["phase_status"], "ACCUMULATING")
        self.assertEqual(payload["stable_sessions"]["clean_sessions"], 59)
        self.assertEqual(payload["stable_sessions"]["remaining_sessions"], 1)
        self.assertEqual(payload["paper_auto_campaign"]["clean_sessions"], 20)
        self.assertEqual(payload["next_action"], "continue_accumulating_stable_sessions")
        self.assertTrue(payload["review_only"])
        self.assertFalse(payload["live_trading_authorized"])
        self.assertIn("Phase status: **ACCUMULATING**", markdown)

    def test_phase_review_ready_requires_sixty_stable_and_twenty_clean_paper_auto_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifacts = write_clean_phase_inputs(root, stable_sessions=60, paper_auto_clean_sessions=20)

            exit_code = main(phase_args(root, artifacts))
            payload = read_json(root / "phase" / "2026-06-16" / "phase_review.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["phase_status"], "READY_FOR_REVIEW")
        self.assertEqual(payload["next_action"], "manual_phase_review")
        self.assertFalse(payload["authority"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])

    def test_phase_review_includes_real_money_consideration_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifacts = write_clean_phase_inputs(root, stable_sessions=60, paper_auto_clean_sessions=20)
            campaign = read_json(artifacts["campaign_report"])
            campaign["real_money_consideration"] = {
                "state": "PAPER_EVIDENCE_READY",
                "clean_trial_days": 30,
                "recovery_days": 0,
                "live_trading_authorized": False,
            }
            write_json(artifacts["campaign_report"], campaign)

            exit_code = main(phase_args(root, artifacts) + ["--trial-day-root", str(root / "trial_days")])
            payload = read_json(root / "phase" / "2026-06-16" / "phase_review.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["real_money_consideration"]["state"], "PAPER_EVIDENCE_READY")
        self.assertEqual(payload["sources"]["trial_day_root"], str(root / "trial_days"))

    def test_phase_review_blocks_on_operator_evidence_or_quality_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifacts = write_clean_phase_inputs(root, stable_sessions=60)
            write_json(
                artifacts["operator_status"],
                {
                    "status": "CRITICAL",
                    "as_of_date": "2026-06-16",
                    "clean_for_paper_auto": False,
                    "blockers": [{"severity": "CRITICAL", "code": "open_broker_orders", "message": "open order"}],
                    "safety": safe_flags(),
                },
            )

            exit_code = main(phase_args(root, artifacts))
            payload = read_json(root / "phase" / "2026-06-16" / "phase_review.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["phase_status"], "BLOCKED")
        self.assertEqual(payload["next_action"], "resolve_phase_blockers")
        self.assertIn("open_broker_orders", {item["code"] for item in payload["blockers"]})

    def test_phase_review_never_ready_for_defer_or_blocked_quality(self) -> None:
        for quality_status, expected_phase in (("DEFER", "ACCUMULATING"), ("BLOCKED", "BLOCKED")):
            with self.subTest(quality_status=quality_status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                artifacts = write_clean_phase_inputs(root, stable_sessions=60)
                write_json(
                    artifacts["strategy_quality"],
                    {
                        "status": "WARN" if quality_status == "DEFER" else "CRITICAL",
                        "quality_status": quality_status,
                        "blockers": ["llm_baseline_disagreement"] if quality_status == "BLOCKED" else [],
                        "safety": safe_flags(),
                    },
                )

                exit_code = main(phase_args(root, artifacts))
                payload = read_json(root / "phase" / "2026-06-16" / "phase_review.json")

            self.assertNotEqual(payload["phase_status"], "READY_FOR_REVIEW")
            self.assertEqual(payload["phase_status"], expected_phase)
            self.assertEqual(exit_code, 1 if expected_phase == "BLOCKED" else 0)


def phase_args(root: Path, artifacts: dict[str, Path]) -> list[str]:
    return [
        "paper-phase-review-report",
        "--as-of-date",
        "2026-06-16",
        "--campaign-report",
        str(artifacts["campaign_report"]),
        "--performance-report",
        str(artifacts["performance_report"]),
        "--operator-status",
        str(artifacts["operator_status"]),
        "--strategy-quality",
        str(artifacts["strategy_quality"]),
        "--evidence-index",
        str(artifacts["evidence_index"]),
        "--weekly-summary",
        str(artifacts["weekly_summary"]),
        "--output-dir",
        str(root / "phase"),
    ]


def write_clean_phase_inputs(
    root: Path,
    *,
    stable_sessions: int,
    paper_auto_clean_sessions: int = 20,
) -> dict[str, Path]:
    artifacts = {
        "campaign_report": root / "campaign.json",
        "performance_report": root / "performance.json",
        "operator_status": root / "operator.json",
        "strategy_quality": root / "quality.json",
        "evidence_index": root / "evidence.json",
        "weekly_summary": root / "weekly.json",
    }
    write_json(
        artifacts["campaign_report"],
        {
            "status": "OK",
            "as_of_date": "2026-06-16",
            "stability_campaign": {
                "state": "READY_FOR_REVIEW" if stable_sessions >= 60 else "ACCUMULATING",
                "target_clean_sessions": 60,
                "clean_sessions": stable_sessions,
                "remaining_clean_sessions": max(60 - stable_sessions, 0),
                "broker_confirmed_sessions": stable_sessions,
                "blocker_histogram": {},
            },
            "paper_auto_campaign": {
                "state": "READY_FOR_REVIEW" if paper_auto_clean_sessions >= 20 else "ACCUMULATING",
                "target_clean_sessions": 20,
                "clean_sessions": paper_auto_clean_sessions,
                "remaining_clean_sessions": max(20 - paper_auto_clean_sessions, 0),
                "broker_confirmed_sessions": paper_auto_clean_sessions,
                "blocker_histogram": {},
            },
            "blockers": [],
            "safety": safe_flags(),
        },
    )
    write_json(
        artifacts["performance_report"],
        {
            "status": "OK",
            "paper_metrics": {"fills": 60, "pending_closeouts": 0, "unmatched_closeouts": 0},
            "statement_status": {"status": "MATCHED", "unreconciled_fills": 0},
            "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0},
            "blockers": [],
            "safety": safe_flags(),
        },
    )
    write_json(
        artifacts["operator_status"],
        {
            "status": "OK",
            "as_of_date": "2026-06-16",
            "clean_for_paper_auto": True,
            "blockers": [],
            "safety": safe_flags(),
        },
    )
    write_json(
        artifacts["strategy_quality"],
        {"status": "OK", "quality_status": "PASS", "blockers": [], "safety": safe_flags()},
    )
    write_json(
        artifacts["evidence_index"],
        {"status": "OK", "issues": [], "artifacts": {}, "safety": safe_flags()},
    )
    write_json(
        artifacts["weekly_summary"],
        {"status": "OK", "blockers": [], "safety": safe_flags()},
    )
    return artifacts


def safe_flags() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
