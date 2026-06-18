import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperCampaignReportCliTests(unittest.TestCase):
    def test_parser_defaults_for_campaign_report_are_read_only(self) -> None:
        parser = build_parser()
        self.assertIn("paper-campaign-report", parser.format_help())

        args = parser.parse_args(["paper-campaign-report"])

        self.assertEqual(args.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(args.readiness_root, "reports/tmp/paper_daily_prepare")
        self.assertEqual(args.decisions_root, "reports/tmp/paper_decisions")
        self.assertEqual(args.performance_root, "reports/tmp/paper_performance")
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.output, "reports/tmp/paper_campaign/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_campaign/latest.md")
        self.assertEqual(args.as_of_date, "today")

    def test_empty_campaign_report_writes_warning_progress_without_authorizing_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "campaign.json"
            markdown = root / "campaign.md"

            exit_code = run_campaign_report(
                sessions_root=root / "sessions",
                readiness_root=root / "readiness",
                output=output,
                markdown=markdown,
            )
            payload = read_json(output)
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["progress"]["target_sessions"], 60)
        self.assertEqual(payload["progress"]["complete_sessions"], 0)
        self.assertEqual(payload["progress"]["pending_sessions"], 0)
        self.assertEqual(payload["progress"]["remaining_sessions"], 60)
        self.assertFalse(payload["progress"]["live_trading_authorized"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["telegram_enabled"])
        self.assertEqual(payload["readiness"]["total"], 0)
        self.assertIn("no_sessions", blocker_codes(payload))
        self.assertIn("Live trading authorized: `False`", markdown_text)

    def test_complete_session_counts_progress_against_sixty_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_campaign_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            write_readiness(root / "readiness" / "core_etfs" / "1d" / "2026-06-16" / "readiness.json")
            output = root / "campaign.json"
            markdown = root / "campaign.md"

            exit_code = run_campaign_report(
                sessions_root=root / "sessions",
                readiness_root=root / "readiness",
                output=output,
                markdown=markdown,
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["progress"]["complete_sessions"], 1)
        self.assertEqual(payload["progress"]["pending_sessions"], 0)
        self.assertEqual(payload["progress"]["remaining_sessions"], 59)
        self.assertEqual(payload["sessions"]["latest_session_date"], "2026-06-16")
        self.assertEqual(payload["readiness"]["ready"], 1)
        self.assertFalse(payload["progress"]["ready_for_live_review"])
        self.assertFalse(payload["progress"]["live_trading_authorized"])

    def test_blockers_include_readiness_and_pending_session_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_campaign_session(root / "sessions" / "daily" / "2026-06-16", with_closeout=False)
            write_readiness(
                root / "readiness" / "core_etfs" / "1d" / "2026-06-16" / "readiness.json",
                status="BLOCKED",
                ready=False,
                reasons=["offline_smoke_blocked"],
            )
            output = root / "campaign.json"
            markdown = root / "campaign.md"

            exit_code = run_campaign_report(
                sessions_root=root / "sessions",
                readiness_root=root / "readiness",
                output=output,
                markdown=markdown,
            )
            payload = read_json(output)
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["progress"]["complete_sessions"], 0)
        self.assertEqual(payload["progress"]["pending_sessions"], 1)
        self.assertEqual(payload["readiness"]["blocked"], 1)
        self.assertIn("offline_smoke_blocked", blocker_codes(payload))
        self.assertIn("paper_execution_without_closeout", blocker_codes(payload))
        self.assertIn("offline_smoke_blocked", markdown_text)

    def test_campaign_report_redacts_secret_like_values_from_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "ledger.jsonl"
            ledger.write_text(
                json.dumps(
                    {
                        "event_type": "paper_session",
                        "generated_at": "2026-06-16T00:00:00+00:00",
                        "status": "BLOCKED",
                        "reasons": ["api_key=KEY secret_key=SECRET token=TOKEN"],
                    }
                ),
                encoding="utf-8",
            )
            write_readiness(
                root / "readiness" / "core_etfs" / "1d" / "2026-06-16" / "readiness.json",
                status="ERROR",
                ready=False,
                reasons=["api_key=KEY secret_key=SECRET token=TOKEN"],
            )
            output = root / "campaign.json"
            markdown = root / "campaign.md"

            exit_code = run_campaign_report(
                sessions_root=root / "sessions",
                readiness_root=root / "readiness",
                ledger_inputs=[ledger],
                output=output,
                markdown=markdown,
            )
            output_text = output.read_text(encoding="utf-8")
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertNotIn("KEY", output_text)
        self.assertNotIn("SECRET", output_text)
        self.assertNotIn("TOKEN", output_text)
        self.assertNotIn("KEY", markdown_text)
        self.assertNotIn("SECRET", markdown_text)
        self.assertNotIn("TOKEN", markdown_text)
        self.assertIn("[redacted]", output_text)

    def test_campaign_report_includes_latest_decisions_and_performance_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_campaign_session(root / "sessions" / "daily" / "2026-06-16", closeout_status="CLOSED")
            write_readiness(root / "readiness" / "core_etfs" / "1d" / "2026-06-16" / "readiness.json")
            write_decision(root / "decisions" / "2026-06-16" / "decision.json")
            write_performance(root / "performance" / "latest.json")
            output = root / "campaign.json"
            markdown = root / "campaign.md"

            exit_code = run_campaign_report(
                sessions_root=root / "sessions",
                readiness_root=root / "readiness",
                output=output,
                markdown=markdown,
                decisions_root=root / "decisions",
                performance_root=root / "performance",
            )
            payload = read_json(output)
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["decisions"]["latest"]["decision"], "CONTINUE")
        self.assertEqual(payload["performance"]["latest"]["status"], "WARN")
        self.assertEqual(payload["paper_vs_backtest"]["backtest_available"], False)
        self.assertIn("## Latest Decisions", markdown_text)
        self.assertIn("## Performance", markdown_text)


def run_campaign_report(
    *,
    sessions_root: Path,
    readiness_root: Path,
    output: Path,
    markdown: Path,
    ledger_inputs: list[Path] | None = None,
    decisions_root: Path | None = None,
    performance_root: Path | None = None,
) -> int:
    args = [
        "paper-campaign-report",
        "--sessions-root",
        str(sessions_root),
        "--readiness-root",
        str(readiness_root),
        "--output",
        str(output),
        "--markdown-output",
        str(markdown),
        "--as-of-date",
        "2026-06-16",
    ]
    if decisions_root is not None:
        args.extend(["--decisions-root", str(decisions_root)])
    if performance_root is not None:
        args.extend(["--performance-root", str(performance_root)])
    for ledger in ledger_inputs or []:
        args.extend(["--ledger-input", str(ledger)])
    try:
        return main(args)
    except SystemExit as exc:  # pragma: no cover - clearer missing-command failure
        raise AssertionError(f"paper-campaign-report command is not registered: {exc}") from exc


def write_campaign_session(
    session_dir: Path,
    *,
    ready: bool = True,
    with_execution: bool = True,
    with_closeout: bool = True,
    closeout_status: str = "CLOSED",
) -> Path:
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    signal = signal_report()
    findings = [] if ready else [{"severity": "fail", "code": "freshness_blocked", "message": "blocked"}]
    write_json(
        session_dir / "session.json",
        {
            "schema_version": "1.0",
            "output_dir": str(session_dir),
            "as_of_date": "2026-06-16",
            "ready_for_paper_review": ready,
            "exit_code": 0 if ready else 1,
            "paths": {
                "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
                "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
                "audit_report": str(session_dir / "audit" / "paper_audit.json"),
            },
        },
    )
    write_json(
        session_dir / "audit" / "paper_audit.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-16T00:01:00+00:00",
            "ready_for_paper_review": ready,
            "findings": findings,
            "summary": {"fail_count": 0 if ready else 1},
        },
    )
    write_json(session_dir / "paper" / "paper_signal_order.json", signal)
    write_json(session_dir / "fresh_data" / "freshness.json", {"allowed": ready, "reasons": []})
    if with_execution:
        (session_dir / "execution").mkdir()
        write_json(
            session_dir / "execution" / "paper_execution.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-16T00:02:00+00:00",
                "status": "SUBMITTED",
                "session": {"session_dir": str(session_dir), "ready_for_paper_review": ready},
                "preflight": {"allowed": True, "reasons": []},
                "order_sent": signal["order_intent"],
                "broker_result": {"accepted": True, "status": "submitted", "reasons": []},
            },
        )
    if with_closeout:
        (session_dir / "closeout").mkdir()
        write_json(
            session_dir / "closeout" / "paper_closeout.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-16T00:03:00+00:00",
                "status": closeout_status,
                "session": {"session_dir": str(session_dir), "ready_for_paper_review": ready},
                "expected_order": signal["order_intent"],
                "reasons": [] if closeout_status == "CLOSED" else ["not_filled_yet"],
            },
        )
    return session_dir


def write_readiness(
    path: Path,
    *,
    status: str = "READY",
    ready: bool = True,
    reasons: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True)
    write_json(
        path,
        {
            "schema_version": 1,
            "generated_at": "2026-06-16T00:00:00+00:00",
            "status": status,
            "ready_for_paper_daily": ready,
            "exit_code": 0 if ready else 1,
            "as_of_date": "2026-06-16",
            "approved_dataset": {"dataset_id": "core_etfs", "frequency": "1d"},
            "offline_smoke": {"requested": True, "ran": True, "status": status, "exit_code": 0 if ready else 1},
            "reasons": reasons or [],
        },
    )
    return path


def write_decision(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    write_json(
        path,
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-16T23:59:00+00:00",
            "as_of_date": "2026-06-16",
            "decision": "CONTINUE",
            "state": "CONTINUE",
            "operator": "ops",
            "reason": "monitor ok",
            "blockers": [],
            "safety": {"live_trading_authorized": False},
        },
    )
    return path


def write_performance(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    write_json(
        path,
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-16T23:58:00+00:00",
            "status": "WARN",
            "warnings": ["missing_backtest_report"],
            "paper_metrics": {
                "complete_sessions": 1,
                "fills": 1,
                "performance_stable": False,
                "pnl": {"source": "proxy"},
            },
            "paper_vs_backtest": {"backtest_available": False, "warnings": ["missing_backtest_report"]},
            "safety": {"live_trading_authorized": False},
        },
    )
    return path


def signal_report() -> dict[str, object]:
    return {
        "preflight": {"allowed": True, "reasons": []},
        "submitted": True,
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "signal-spy-20260616",
            "type": "market",
            "time_in_force": "day",
            "notional": 1.0,
        },
    }


def blocker_codes(payload: dict[str, object]) -> set[str]:
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return set()
    return {str(blocker.get("code")) for blocker in blockers if isinstance(blocker, dict)}


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
