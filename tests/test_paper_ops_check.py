import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperOpsCheckTests(unittest.TestCase):
    def test_parser_defaults_for_ops_check(self) -> None:
        args = build_parser().parse_args(["paper-ops-check", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.readiness_root, "reports/tmp/paper_daily_prepare")
        self.assertEqual(args.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(args.monitor_root, "reports/tmp/paper_monitor")
        self.assertEqual(args.campaign_root, "reports/tmp/paper_campaign")
        self.assertEqual(args.decisions_root, "reports/tmp/paper_decisions")
        self.assertEqual(args.performance_root, "reports/tmp/paper_performance")
        self.assertIsNone(args.position_watch)
        self.assertIsNone(args.eod_position_plan)
        self.assertIsNone(args.telegram_status)
        self.assertIsNone(args.telegram_history)
        self.assertIsNone(args.telegram_dispatch)
        self.assertIsNone(args.ai_value_report)
        self.assertIsNone(args.cross_asset_session_plan)
        self.assertFalse(args.require_ai_value_ready)
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.output_dir, "reports/tmp/paper_ops_check")

    def test_complete_continue_day_produces_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")
            markdown = (root / "ops" / "2026-06-16" / "ops_check.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["artifacts"]["readiness"]["status"], "READY")
        self.assertEqual(payload["artifacts"]["decision"]["decision"], "CONTINUE")
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertIn("Status: **OK**", markdown)

    def test_missing_performance_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE", include_performance=False)

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("missing_performance", issue_codes(payload))

    def test_stop_decision_produces_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="STOP")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertIn("decision_stop", issue_codes(payload))

    def test_invalid_required_json_produces_error_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            (root / "readiness" / "2026-06-16" / "readiness.json").write_text("{bad json", encoding="utf-8")

            exit_code = main(ops_args(root))
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("invalid_readiness_json", issue_codes(payload))

    def test_secret_like_values_are_redacted_in_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            write_json(
                root / "decisions" / "2026-06-16" / "decision.json",
                {
                    "status": "OK",
                    "decision": "CONTINUE",
                    "as_of_date": "2026-06-16",
                    "reason": "api_key=KEY secret_key=SECRET token=TOKEN",
                    "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
                },
            )

            exit_code = main(ops_args(root))
            output = (root / "ops" / "2026-06-16" / "ops_check.json").read_text(encoding="utf-8")
            markdown = (root / "ops" / "2026-06-16" / "ops_check.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("KEY", output)
        self.assertNotIn("SECRET", output)
        self.assertNotIn("TOKEN", output)
        self.assertNotIn("KEY", markdown)
        self.assertNotIn("SECRET", markdown)
        self.assertNotIn("TOKEN", markdown)
        self.assertIn("[redacted]", output)

    def test_periodic_ops_check_surfaces_position_eod_telegram_and_ai_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            position_watch = root / "position_watch.json"
            eod_plan = root / "eod_plan.json"
            telegram_status = root / "telegram_status.json"
            telegram_dispatch = root / "telegram_dispatch.json"
            ai_value = root / "ai_value.json"
            write_json(
                position_watch,
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "positions": [{"symbol": "SPY", "quantity": 1.0}],
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )
            write_json(
                eod_plan,
                {
                    "status": "CRITICAL",
                    "as_of_date": "2026-06-16",
                    "summary": {"open_position_count": 1, "close_required_count": 1, "longer_term_hold_count": 0},
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )
            write_json(
                telegram_status,
                {
                    "status": "WARN",
                    "as_of_date": "2026-06-16",
                    "blockers": [],
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )
            write_json(
                telegram_dispatch,
                {
                    "status": "BLOCKED",
                    "as_of_date": "2026-06-16",
                    "summary": {"ready_count": 0, "blocked_count": 1},
                    "blockers": ["gate_not_allowed"],
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                        "subprocess_started": False,
                    },
                },
            )
            write_json(
                ai_value,
                {
                    "status": "AI_VALUE_READY",
                    "as_of_date": "2026-06-16",
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )

            exit_code = main(
                [
                    *ops_args(root),
                    "--position-watch",
                    str(position_watch),
                    "--eod-position-plan",
                    str(eod_plan),
                    "--telegram-status",
                    str(telegram_status),
                    "--telegram-dispatch",
                    str(telegram_dispatch),
                    "--ai-value-report",
                    str(ai_value),
                    "--require-ai-value-ready",
                ]
            )
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["artifacts"]["position_watch"]["status"], "OK")
        self.assertEqual(payload["artifacts"]["eod_position_plan"]["status"], "CRITICAL")
        self.assertEqual(payload["artifacts"]["telegram_dispatch"]["status"], "BLOCKED")
        self.assertEqual(payload["artifacts"]["ai_value_report"]["status"], "AI_VALUE_READY")
        codes = issue_codes(payload)
        self.assertIn("eod_close_required", codes)
        self.assertIn("telegram_dispatch_blocked", codes)
        self.assertIn("telegram_status_warn", codes)
        self.assertNotIn("ai_value_not_ready", codes)

    def test_required_ai_value_report_blocks_periodic_ops_when_missing_or_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            ai_value = root / "ai_value.json"

            missing_exit_code = main([*ops_args(root), "--require-ai-value-ready"])
            missing_payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

            write_json(
                ai_value,
                {
                    "status": "AI_VALUE_INSUFFICIENT",
                    "as_of_date": "2026-06-16",
                    "blockers": ["ai_candidate_did_not_clear_thresholds"],
                    "safety": {
                        "paper_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )

            exit_code = main([*ops_args(root), "--ai-value-report", str(ai_value), "--require-ai-value-ready"])
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(missing_exit_code, 1)
        self.assertEqual(missing_payload["status"], "CRITICAL")
        self.assertIn("missing_ai_value_report", issue_codes(missing_payload))
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertIn("ai_value_not_ready", issue_codes(payload))

    def test_cross_asset_session_plan_blocks_periodic_ops_when_close_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            cross_asset = root / "cross_asset_session_plan.json"
            write_json(
                cross_asset,
                {
                    "status": "CRITICAL",
                    "as_of_date": "2026-06-19",
                    "summary": {"close_required_count": 1, "longer_term_hold_count": 0, "review_count": 0},
                    "safety": {
                        "paper_only": True,
                        "read_only": True,
                        "orders_submitted": False,
                        "credentials_read": False,
                        "live_trading_allowed": False,
                        "live_trading_authorized": False,
                    },
                },
            )

            exit_code = main([*ops_args(root), "--cross-asset-session-plan", str(cross_asset)])
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["artifacts"]["cross_asset_session_plan"]["status"], "CRITICAL")
        self.assertIn("cross_asset_session_close_required", issue_codes(payload))

    def test_stale_direct_operational_artifacts_block_periodic_ops_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_complete_day(root, decision="CONTINUE")
            position_watch = root / "position_watch.json"
            telegram_status = root / "telegram_status.json"
            cross_asset = root / "cross_asset_session_plan.json"
            stale_safety = {
                "paper_only": True,
                "read_only": True,
                "orders_submitted": False,
                "credentials_read": False,
                "live_trading_allowed": False,
                "live_trading_authorized": False,
            }
            write_json(
                position_watch,
                {
                    "status": "OK",
                    "as_of_date": "2026-06-15",
                    "positions": [],
                    "safety": stale_safety,
                },
            )
            write_json(
                telegram_status,
                {
                    "status": "OK",
                    "as_of_date": "2026-06-15",
                    "blockers": [],
                    "safety": stale_safety,
                },
            )
            write_json(
                cross_asset,
                {
                    "status": "OK",
                    "as_of_date": "2026-06-15",
                    "summary": {"close_required_count": 0, "longer_term_hold_count": 0, "review_count": 0},
                    "safety": stale_safety,
                },
            )

            exit_code = main(
                [
                    *ops_args(root),
                    "--position-watch",
                    str(position_watch),
                    "--telegram-status",
                    str(telegram_status),
                    "--cross-asset-session-plan",
                    str(cross_asset),
                ]
            )
            payload = read_json(root / "ops" / "2026-06-16" / "ops_check.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        codes = issue_codes(payload)
        self.assertIn("position_watch_stale", codes)
        self.assertIn("telegram_status_stale", codes)
        self.assertIn("cross_asset_session_plan_stale", codes)


def ops_args(root: Path) -> list[str]:
    return [
        "paper-ops-check",
        "--as-of-date",
        "2026-06-16",
        "--readiness-root",
        str(root / "readiness"),
        "--sessions-root",
        str(root / "sessions"),
        "--monitor-root",
        str(root / "monitor"),
        "--campaign-root",
        str(root / "campaign"),
        "--decisions-root",
        str(root / "decisions"),
        "--performance-root",
        str(root / "performance"),
        "--output-dir",
        str(root / "ops"),
    ]


def write_complete_day(root: Path, *, decision: str, include_performance: bool = True) -> None:
    write_json(
        root / "readiness" / "2026-06-16" / "readiness.json",
        {"status": "READY", "ready_for_paper_daily": True, "as_of_date": "2026-06-16", "reasons": []},
    )
    write_json(
        root / "monitor" / "2026-06-16" / "monitor.json",
        {
            "status": "OK",
            "monitor_summary": {
                "as_of_date": "2026-06-16",
                "critical_count": 0,
                "warning_count": 0,
                "pending_closeout_count": 0,
                "unmatched_closeout_count": 0,
            },
            "alerts": [],
        },
    )
    write_json(root / "campaign" / "2026-06-16" / "campaign.json", {"status": "OK", "as_of_date": "2026-06-16"})
    write_json(
        root / "decisions" / "2026-06-16" / "decision.json",
        {
            "status": "OK",
            "decision": decision,
            "as_of_date": "2026-06-16",
            "blockers": [] if decision == "CONTINUE" else [{"severity": "CRITICAL", "code": "manual_stop"}],
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
    )
    if include_performance:
        write_json(
            root / "performance" / "2026-06-16" / "performance.json",
            {
                "status": "OK",
                "paper_metrics": {"pending_closeouts": 0, "unmatched_closeouts": 0},
                "statement_reconciliation": {"status": "MATCHED"},
            },
        )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def issue_codes(payload: dict[str, Any]) -> set[str]:
    return {str(issue["code"]) for issue in payload["issues"]}


if __name__ == "__main__":
    unittest.main()
