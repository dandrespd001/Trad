import json
import tempfile
import unittest
import urllib.error
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_monitor import (
    _alert,
    _build_monitor_summary,
    _dedupe_alerts,
    _paper_monitor_ledger_event,
    run_paper_monitor,
)


class ExplodingEnv(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"environment should not be read: {key}")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: str | None = None) -> str | None:
        raise AssertionError(f"environment should not be read: {key}")


class FakeTelegramResponse:
    status = 200

    def __enter__(self) -> "FakeTelegramResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return b'{"ok": true, "result": {"message_id": 123}}'


class FakeReadOnlyBrokerClient:
    def __init__(self, *, orders: list[object] | None = None) -> None:
        self.orders = orders or []
        self.calls: list[str] = []

    def get_account(self) -> object:
        self.calls.append("get_account")

        class Account:
            id = "sensitive-paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        self.calls.append("list_positions")

        class Position:
            symbol = "SPY"
            qty = "1"
            market_value = "500.00"

        return [Position()]

    def get_orders(self, filter: object | None = None) -> list[object]:
        self.calls.append("get_orders")
        return self.orders

    def submit_order(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("snapshot must not submit orders")

    def cancel_order_by_id(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("snapshot must not cancel orders")


class FakeOpenOrder:
    id = "broker-order-1"
    client_order_id = "external-open-order"
    symbol = "SPY"
    side = "buy"
    type = "market"
    order_type = "market"
    time_in_force = "day"
    status = "accepted"
    notional = "1"
    qty = None
    filled_qty = "0"
    filled_avg_price = None
    submitted_at = "2026-06-16T12:00:00Z"
    created_at = "2026-06-16T12:00:00Z"
    updated_at = "2026-06-16T12:00:01Z"
    expires_at = "2026-06-17T20:00:00Z"


class PaperMonitorTests(unittest.TestCase):
    def test_parser_defaults_for_monitor_and_telegram_opt_in(self) -> None:
        args = build_parser().parse_args(["paper-monitor"])

        self.assertEqual(args.sessions_root, "reports/tmp/paper_session")
        self.assertEqual(args.session_dir, [])
        self.assertEqual(args.ledger_input, [])
        self.assertEqual(args.output, "reports/tmp/paper_monitor/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_monitor/latest.md")
        self.assertEqual(args.as_of_date, "today")
        self.assertEqual(args.min_stable_sessions, 60)
        self.assertFalse(args.broker_read_only)
        self.assertFalse(args.confirm_paper)
        self.assertEqual(args.universe, "configs/universe.yml")
        self.assertEqual(args.risk, "configs/risk.yml")
        self.assertEqual(args.order_status, "open")
        self.assertIsNone(args.ledger_output)
        self.assertFalse(args.send_telegram)
        self.assertFalse(args.telegram_dry_run)
        self.assertFalse(args.telegram_send_warnings)

    def test_clean_session_execution_and_closeout_produces_ok_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_monitor_session(root / "sessions" / "latest", closeout_status="CLOSED")
            output = root / "monitor.json"
            markdown = root / "monitor.md"

            exit_code = main(
                [
                    "paper-monitor",
                    "--sessions-root",
                    str(root / "sessions"),
                    "--session-dir",
                    str(session_dir),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--as-of-date",
                    "2026-06-16",
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            markdown_exists = markdown.exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["alerts"], [])
        self.assertEqual(payload["stability"]["status"], "ACCUMULATING")
        self.assertEqual(payload["stability"]["stable_session_count"], 1)
        self.assertEqual(payload["broker_snapshot"]["status"], "SKIPPED")
        self.assertTrue(markdown_exists)

    def test_sixty_complete_sessions_pass_stability_without_authorizing_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(60):
                write_monitor_session(root / "sessions" / f"session-{index:02d}", closeout_status="CLOSED")
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.dashboard["status"], "OK")
        self.assertEqual(result.dashboard["stability"]["status"], "PASSED")
        self.assertEqual(result.dashboard["stability"]["stable_session_count"], 60)
        self.assertTrue(result.dashboard["stability"]["ready_for_live_review"])
        self.assertFalse(result.dashboard["stability"]["live_trading_authorized"])

    def test_fifty_nine_complete_sessions_are_still_accumulating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(59):
                write_monitor_session(root / "sessions" / f"session-{index:02d}", closeout_status="CLOSED")
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.dashboard["status"], "OK")
        self.assertEqual(result.dashboard["stability"]["status"], "ACCUMULATING")
        self.assertEqual(result.dashboard["stability"]["stable_session_count"], 59)

    def test_old_events_without_as_of_date_use_generated_at_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "ledger.jsonl"
            ledger.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event_type": "paper_session",
                                "generated_at": "2026-06-16T00:01:00+00:00",
                                "status": "READY",
                                "client_order_id": "legacy-order",
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "paper_execution",
                                "generated_at": "2026-06-16T00:02:00+00:00",
                                "status": "SUBMITTED",
                                "client_order_id": "legacy-order",
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "paper_closeout",
                                "generated_at": "2026-06-16T00:03:00+00:00",
                                "status": "CLOSED",
                                "client_order_id": "legacy-order",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            result = run_paper_monitor(
                sessions_root=root / "empty",
                ledger_inputs=[ledger],
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
                min_stable_sessions=1,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.dashboard["status"], "OK")
        self.assertEqual(result.dashboard["monitor_summary"]["latest_session_date"], "2026-06-16")
        self.assertEqual(result.dashboard["stability"]["status"], "PASSED")

    def test_blocked_session_is_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "blocked", ready=False, with_execution=False)
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.dashboard["status"], "CRITICAL")
        self.assertIn("paper_session_blocked", alert_codes(result.dashboard))

    def test_submitted_execution_without_closeout_is_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.dashboard["status"], "CRITICAL")
        self.assertIn("paper_execution_without_closeout", alert_codes(result.dashboard))

    def test_pending_and_unmatched_closeouts_are_critical(self) -> None:
        for closeout_status, expected_code in (
            ("PENDING", "paper_closeout_pending"),
            ("UNMATCHED", "paper_closeout_unmatched"),
        ):
            with self.subTest(closeout_status=closeout_status):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    write_monitor_session(root / "sessions" / "latest", closeout_status=closeout_status)
                    result = run_paper_monitor(
                        sessions_root=root / "sessions",
                        output=root / "monitor.json",
                        markdown_output=root / "monitor.md",
                        as_of_date="2026-06-16",
                    )

                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.dashboard["status"], "CRITICAL")
                self.assertIn(expected_code, alert_codes(result.dashboard))

    def test_invalid_or_missing_artifacts_are_critical_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_monitor_session(root / "sessions" / "bad", with_execution=False)
            (session_dir / "audit" / "paper_audit.json").unlink()
            (session_dir / "paper" / "paper_signal_order.json").write_text("{bad json", encoding="utf-8")
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.dashboard["status"], "CRITICAL")
        self.assertIn("observability_diagnostic", alert_codes(result.dashboard))

    def test_missing_requested_ledger_is_warning_not_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", closeout_status="CLOSED")
            result = run_paper_monitor(
                sessions_root=root / "sessions",
                ledger_inputs=[root / "missing.jsonl"],
                output=root / "monitor.json",
                markdown_output=root / "monitor.md",
                as_of_date="2026-06-16",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.dashboard["status"], "WARN")
        self.assertIn("ledger_missing", alert_codes(result.dashboard))

    def test_warning_aliases_are_normalized_for_counts_and_ledger_reasons(self) -> None:
        alerts = _dedupe_alerts(
            [
                _alert(
                    severity="WARNING",
                    code="ledger_missing",
                    message="legacy alias",
                ),
                _alert(
                    severity="WARN",
                    code="ledger_missing",
                    message="normalized alias",
                ),
            ]
        )
        self.assertEqual(len(alerts), 1)

        summary = _build_monitor_summary(
            SimpleNamespace(events=[]),
            alerts,
            as_of_date=date(2026, 6, 16),
            status="WARN",
        )
        self.assertEqual(summary["status"], "WARN")
        self.assertEqual(summary["critical_count"], 0)
        self.assertEqual(summary["warning_count"], 1)

        ledger_event = _paper_monitor_ledger_event(
            {
                "status": "WARN",
                "alerts": alerts,
                "generated_at": "2026-06-16T00:00:00+00:00",
            },
            exit_code=0,
            output_path=Path("paper-monitor-monitor.json"),
        )
        self.assertEqual(ledger_event["reasons"], ["ledger_missing"])

    def test_broker_read_only_requires_confirm_before_building_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "monitor.json"
            markdown = root / "monitor.md"
            with mock.patch(
                "trading_ai.execution.paper_monitor.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-monitor",
                        "--sessions-root",
                        str(root / "sessions"),
                        "--output",
                        str(output),
                        "--markdown-output",
                        str(markdown),
                        "--broker-read-only",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertFalse(output.exists())

    def test_broker_read_only_snapshot_queries_only_and_flags_unmatched_open_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", closeout_status="CLOSED")
            universe = write_monitor_universe(root / "universe.yml")
            risk = write_monitor_risk(root / "risk.yml")
            client = FakeReadOnlyBrokerClient(orders=[FakeOpenOrder()])

            with mock.patch("trading_ai.execution.paper_monitor.build_alpaca_paper_client", return_value=client):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    min_stable_sessions=1,
                    broker_read_only=True,
                    confirm_paper=True,
                    universe=universe,
                    risk=risk,
                    env={
                        "ALPACA_PAPER_API_KEY": "KEY",
                        "ALPACA_PAPER_SECRET_KEY": "SECRET",
                    },
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(client.calls, ["get_account", "list_positions", "get_orders"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["stability"]["status"], "BLOCKED")
        self.assertIn("broker_open_order_without_closed_closeout", alert_codes(payload))
        self.assertEqual(payload["broker_snapshot"]["status"], "OK")
        self.assertNotIn("sensitive-paper-account", json.dumps(payload))

    def test_broker_read_only_failure_writes_error_artifact_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", closeout_status="CLOSED")
            universe = write_monitor_universe(root / "universe.yml")
            risk = write_monitor_risk(root / "risk.yml")
            with mock.patch(
                "trading_ai.execution.paper_monitor.build_alpaca_paper_client",
                side_effect=RuntimeError("api_key=KEY secret_key=SECRET"),
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    broker_read_only=True,
                    confirm_paper=True,
                    universe=universe,
                    risk=risk,
                    env={
                        "ALPACA_PAPER_API_KEY": "KEY",
                        "ALPACA_PAPER_SECRET_KEY": "SECRET",
                    },
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))
            markdown = (root / "monitor.md").read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["broker_snapshot"]["status"], "ERROR")
        self.assertIn("broker_snapshot_error", alert_codes(payload))
        self.assertNotIn("KEY", json.dumps(payload))
        self.assertNotIn("SECRET", json.dumps(payload))
        self.assertNotIn("KEY", markdown)
        self.assertNotIn("SECRET", markdown)


class PaperMonitorTelegramTests(unittest.TestCase):
    def test_without_telegram_flag_does_not_read_env_or_call_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            with mock.patch(
                "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                side_effect=AssertionError("network should not be called"),
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    env=ExplodingEnv(),
                )

        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("telegram", result.dashboard)

    def test_telegram_dry_run_writes_redacted_preview_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            with mock.patch(
                "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                side_effect=AssertionError("network should not be called"),
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    telegram_dry_run=True,
                    env=ExplodingEnv(),
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(payload["telegram"]["status"], "DRY_RUN")
        self.assertTrue(payload["telegram"]["would_send"])
        self.assertIn("Paper monitor CRITICAL", payload["telegram"]["message_preview"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json.dumps(payload))

    def test_send_telegram_uses_env_and_sends_one_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            with mock.patch(
                "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                return_value=FakeTelegramResponse(),
            ) as urlopen:
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    send_telegram=True,
                    env={
                        "TELEGRAM_BOT_TOKEN": "SECRET_TOKEN",
                        "TELEGRAM_CHAT_ID": "123456",
                    },
                )
            request = urlopen.call_args.args[0]
            request_body = json.loads(request.data.decode("utf-8"))
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("botSECRET_TOKEN/sendMessage", request.full_url)
        self.assertEqual(request_body["chat_id"], "123456")
        self.assertIn("Paper monitor CRITICAL", request_body["text"])
        self.assertEqual(payload["telegram"]["status"], "SENT")
        self.assertEqual(payload["telegram"]["sent"], True)
        self.assertNotIn("SECRET_TOKEN", json.dumps(payload))
        self.assertNotIn("message_id", json.dumps(payload))

    def test_send_telegram_missing_credentials_returns_operational_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            with mock.patch(
                "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                side_effect=AssertionError("network should not be called without credentials"),
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    send_telegram=True,
                    env={},
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["telegram"]["status"], "FAILED")
        self.assertEqual(payload["telegram"]["reason"], "missing_telegram_credentials")

    def test_send_telegram_rejects_non_https_api_base_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            with (
                mock.patch("trading_ai.execution.paper_monitor.TELEGRAM_API_BASE", "file:///tmp"),
                mock.patch(
                    "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                    side_effect=AssertionError("network should not be called for non-HTTPS API base"),
                ) as urlopen,
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    send_telegram=True,
                    env={
                        "TELEGRAM_BOT_TOKEN": "SECRET_TOKEN",
                        "TELEGRAM_CHAT_ID": "123456",
                    },
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(urlopen.call_count, 0)
        self.assertEqual(payload["telegram"]["status"], "FAILED")
        self.assertEqual(payload["telegram"]["reason"], "invalid_telegram_api_base")

    def test_send_telegram_http_error_returns_operational_error_without_token_in_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_monitor_session(root / "sessions" / "latest", with_closeout=False)
            http_error = urllib.error.HTTPError(
                url="https://api.telegram.org/botSECRET_TOKEN/sendMessage",
                code=500,
                msg="server error",
                hdrs=None,
                fp=None,
            )
            with mock.patch(
                "trading_ai.execution.paper_monitor.urllib.request.urlopen",
                side_effect=http_error,
            ):
                result = run_paper_monitor(
                    sessions_root=root / "sessions",
                    output=root / "monitor.json",
                    markdown_output=root / "monitor.md",
                    as_of_date="2026-06-16",
                    send_telegram=True,
                    env={
                        "TELEGRAM_BOT_TOKEN": "SECRET_TOKEN",
                        "TELEGRAM_CHAT_ID": "123456",
                    },
                )
            payload = json.loads((root / "monitor.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(payload["telegram"]["status"], "FAILED")
        self.assertEqual(payload["telegram"]["reason"], "telegram_http_error_500")
        self.assertNotIn("SECRET_TOKEN", json.dumps(payload))


def alert_codes(dashboard: Mapping[str, object]) -> set[str]:
    alerts = dashboard.get("alerts")
    if not isinstance(alerts, list):
        return set()
    return {str(alert.get("code")) for alert in alerts if isinstance(alert, Mapping)}


def write_monitor_session(
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
    findings = []
    if not ready:
        findings.append({"severity": "fail", "code": "freshness_blocked", "message": "blocked", "source": "test"})
    signal = signal_report()
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
    write_json(
        session_dir / "fresh_data" / "freshness.json", {"allowed": ready, "reasons": [] if ready else ["stale_symbol"]}
    )
    if with_execution:
        (session_dir / "execution").mkdir()
        write_json(
            session_dir / "execution" / "paper_execution.json",
            {
                "schema_version": "1.0",
                "generated_at": "2026-06-16T00:02:00+00:00",
                "status": "SUBMITTED",
                "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
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
                "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
                "expected_order": signal["order_intent"],
                "reasons": [] if closeout_status == "CLOSED" else ["not_filled_yet"],
            },
        )
    return session_dir


def signal_report() -> dict[str, object]:
    return {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": True,
        "preflight": {"allowed": True, "reasons": [], "checked_at": "2026-06-16", "max_feature_age_days": 5},
        "open_orders": [],
        "positions": [],
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-16",
            "symbol": "SPY",
            "probability": 0.93,
            "threshold": 0.5,
            "action": "buy",
        },
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "signal-spy-20260616",
            "type": "market",
            "time_in_force": "day",
            "notional": 1.0,
        },
        "order_result": {
            "accepted": True,
            "status": "dry_run_accepted",
            "reasons": [],
            "dry_run": True,
            "broker_response": None,
        },
    }


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_monitor_universe(path: Path) -> Path:
    path.write_text(
        """
universe:
  symbols: [SPY]
""".lstrip(),
        encoding="utf-8",
    )
    return path


def write_monitor_risk(path: Path) -> Path:
    path.write_text(
        """
risk_limits:
  max_daily_loss_pct: 0.02
  max_drawdown_pct: 0.10
  max_gross_exposure: 1.0
  max_single_position: 0.30
  live_trading_allowed: false
""".lstrip(),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
