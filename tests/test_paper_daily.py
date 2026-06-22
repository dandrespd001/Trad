import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data.io import write_records
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.execution.paper_daily import (
    PaperDailyResult,
    load_paper_daily_config,
    run_paper_daily_from_readiness,
)
from trading_ai.execution.paper_execute_session import PaperExecuteOperationalError
from trading_ai.execution.paper_monitor import PaperMonitorResult
from trading_ai.models.baseline import LogisticBaselineModel, save_model


class FakeExecutionClient:
    def __init__(self) -> None:
        self.submitted_orders: list[dict[str, object]] = []

    def get_account(self) -> object:
        return account()

    def list_positions(self) -> list[object]:
        return []

    def get_orders(self, filter: object | None = None) -> list[object]:
        return []

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted_orders.append(kwargs)
        return {"id": "submitted-order", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        if not self.submitted_orders:
            raise AssertionError("order status read happened before submit")
        submitted = self.submitted_orders[-1]
        return broker_order(
            client_order_id=client_order_id,
            symbol=str(submitted["symbol"]),
            status="accepted",
            filled_qty="0",
        )


class FakeCloseoutClient:
    def __init__(self, *, status: str = "filled", filled_qty: str = "0.002", with_position: bool = True) -> None:
        self.status = status
        self.filled_qty = filled_qty
        self.with_position = with_position

    def get_account(self) -> object:
        return account()

    def list_positions(self) -> list[object]:
        return [Position()] if self.with_position else []

    def get_orders(self, filter: object | None = None) -> list[object]:
        return []

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        return broker_order(client_order_id=client_order_id, status=self.status, filled_qty=self.filled_qty)


class Position:
    symbol = "SPY"
    qty = "0.002"
    market_value = "1.01"


class PaperDailyTests(unittest.TestCase):
    def test_parser_defaults_keep_broker_and_telegram_opt_in(self) -> None:
        args = build_parser().parse_args(["paper-daily"])

        self.assertEqual(args.config, "configs/paper_daily.yml")
        self.assertIsNone(args.source_csv)
        self.assertIsNone(args.start)
        self.assertIsNone(args.end)
        self.assertIsNone(args.as_of_date)
        self.assertFalse(args.confirm_paper)
        self.assertFalse(args.confirm_auto_close)
        self.assertFalse(args.confirm_auto_submit)
        self.assertFalse(args.send_telegram)
        self.assertFalse(args.telegram_dry_run)

    def test_from_readiness_parser_defaults_and_confirmations(self) -> None:
        args = build_parser().parse_args(["paper-daily-from-readiness", "--readiness", "readiness.json"])

        self.assertEqual(args.readiness, "readiness.json")
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.ledger_output)
        self.assertFalse(args.confirm_readiness)
        self.assertFalse(args.confirm_paper)
        self.assertFalse(args.confirm_auto_close)
        self.assertFalse(args.confirm_auto_submit)
        self.assertFalse(args.require_clean_state)

        confirmed = build_parser().parse_args(
            [
                "paper-daily-from-readiness",
                "--readiness",
                "readiness.json",
                "--confirm-readiness",
                "--confirm-paper",
                "--confirm-auto-close",
                "--confirm-auto-submit",
                "--require-clean-state",
            ]
        )

        self.assertTrue(confirmed.confirm_readiness)
        self.assertTrue(confirmed.confirm_paper)
        self.assertTrue(confirmed.confirm_auto_close)
        self.assertTrue(confirmed.confirm_auto_submit)
        self.assertTrue(confirmed.require_clean_state)

    def test_from_readiness_missing_confirmation_returns_two_without_loading_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = root / "readiness.json"

            with mock.patch(
                "trading_ai.execution.paper_daily.load_paper_daily_config",
                side_effect=AssertionError("config should not load without confirmations"),
            ), mock.patch(
                "trading_ai.execution.paper_daily.run_paper_daily",
                side_effect=AssertionError("paper daily should not run without confirmations"),
            ):
                exit_code = main(["paper-daily-from-readiness", "--readiness", str(readiness_path)])

            payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_confirmation:--confirm-readiness", payload["reasons"])
        self.assertIn("missing_confirmation:--confirm-paper", payload["reasons"])

    def test_from_readiness_requires_clean_state_confirmation_before_loading_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = root / "readiness.json"

            with mock.patch(
                "trading_ai.execution.paper_daily.load_paper_daily_config",
                side_effect=AssertionError("config should not load without clean-state confirmation"),
            ), mock.patch(
                "trading_ai.execution.paper_daily.run_paper_daily",
                side_effect=AssertionError("paper daily should not run without clean-state confirmation"),
            ):
                exit_code = main(
                    [
                        "paper-daily-from-readiness",
                        "--readiness",
                        str(readiness_path),
                        "--confirm-readiness",
                        "--confirm-paper",
                        "--confirm-auto-close",
                        "--confirm-auto-submit",
                    ]
                )

            payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("missing_confirmation:--require-clean-state", payload["reasons"])
        self.assertFalse(payload["confirmations"]["require_clean_state"])

    def test_from_readiness_blocks_unapproved_readiness_without_running_paper_daily(self) -> None:
        cases = [
            (
                "status",
                {"status": "BLOCKED", "ready_for_paper_daily": False, "exit_code": 1},
                "readiness_status_not_ready",
            ),
            ("smoke_ran", {"smoke_ran": False}, "offline_smoke_not_ran"),
            ("smoke_exit", {"smoke_exit_code": 1}, "offline_smoke_exit_code_not_zero"),
        ]
        for _name, overrides, expected_reason in cases:
            with self.subTest(_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
                readiness_path = write_readiness(root / "readiness.json", config_path, **overrides)

                with mock.patch(
                    "trading_ai.execution.paper_daily.run_paper_daily",
                    side_effect=AssertionError("paper daily should not run for blocked readiness"),
                ):
                    result = run_paper_daily_from_readiness(
                        readiness_path=readiness_path,
                        confirm_readiness=True,
                        confirm_paper=True,
                        confirm_auto_close=True,
                        confirm_auto_submit=True,
                        require_clean_state=True,
                    )

                payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn(expected_reason, payload["reasons"])

    def test_from_readiness_missing_generated_config_is_operational_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness_path = write_readiness(root / "readiness.json", root / "missing.yml")

            with mock.patch(
                "trading_ai.execution.paper_daily.run_paper_daily",
                side_effect=AssertionError("paper daily should not run without generated config"),
            ):
                result = run_paper_daily_from_readiness(
                    readiness_path=readiness_path,
                    confirm_readiness=True,
                    confirm_paper=True,
                    confirm_auto_close=True,
                    confirm_auto_submit=True,
                    require_clean_state=True,
                )

            payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("paper_daily_config_missing", payload["reasons"][0])

    def test_from_readiness_rejects_relative_today_dates_for_broker_confirmed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'as_of_date: "2026-06-16"',
                    "as_of_date: today",
                ),
                encoding="utf-8",
            )
            readiness_path = write_readiness(root / "readiness.json", config_path)

            result = run_paper_daily_from_readiness(
                readiness_path=readiness_path,
                confirm_readiness=True,
                confirm_paper=True,
                confirm_auto_close=True,
                confirm_auto_submit=True,
                require_clean_state=True,
            )
            payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("broker_confirmed_as_of_date_must_be_explicit", payload["reasons"])

    def test_from_readiness_happy_path_uses_broker_confirmed_paths_and_disables_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            readiness_path = write_readiness(root / "readiness.json", config_path)
            readiness_before = readiness_path.read_text(encoding="utf-8")
            config_before = config_path.read_text(encoding="utf-8")
            seen: dict[str, object] = {}

            def fake_run_paper_daily(**kwargs: object) -> PaperDailyResult:
                config = kwargs["config"]
                seen.update(kwargs)
                return PaperDailyResult(
                    exit_code=0,
                    status="OK",
                    output_path=config.output,
                    markdown_path=config.markdown_output,
                    payload={
                        "run_id": "paper-daily-2026-06-16-test",
                        "status": "OK",
                        "exit_code": 0,
                        "reasons": [],
                        "artifacts": {
                            "daily_json": str(config.output),
                            "daily_markdown": str(config.markdown_output),
                            "session_dir": str(config.session_dir),
                            "observability_json": str(config.observability_output),
                            "monitor_json": str(config.monitor_output),
                        },
                        "broker_actions": [{"action": "submit_new_session", "status": "SUBMITTED"}],
                        "final_monitor": {"status": "OK", "exit_code": 0},
                    },
                )

            with mock.patch("trading_ai.execution.paper_daily.run_paper_daily", side_effect=fake_run_paper_daily):
                result = run_paper_daily_from_readiness(
                    readiness_path=readiness_path,
                    confirm_readiness=True,
                    confirm_paper=True,
                    confirm_auto_close=True,
                    confirm_auto_submit=True,
                    require_clean_state=True,
                )

            broker_dir = root / "paper_daily" / "broker_confirmed"
            config = seen["config"]
            payload = read_json(broker_dir / "broker_run.json")
            markdown_exists = (broker_dir / "broker_run.md").exists()
            readiness_after = readiness_path.read_text(encoding="utf-8")
            config_after = config_path.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.status, "OK")
        self.assertTrue(seen["confirm_paper"])
        self.assertTrue(seen["confirm_auto_close"])
        self.assertTrue(seen["confirm_auto_submit"])
        self.assertFalse(seen["send_telegram"])
        self.assertEqual(config.output, (broker_dir / "daily.json").resolve(strict=False))
        self.assertEqual(config.markdown_output, (broker_dir / "daily.md").resolve(strict=False))
        self.assertEqual(config.sessions_root, (broker_dir / "sessions").resolve(strict=False))
        self.assertEqual(config.session_dir, (broker_dir / "sessions" / "daily" / "2026-06-16").resolve(strict=False))
        self.assertEqual(config.observability_output, (broker_dir / "observability.json").resolve(strict=False))
        self.assertEqual(config.monitor_output, (broker_dir / "monitor.json").resolve(strict=False))
        self.assertEqual(payload["paper_daily"]["status"], "OK")
        self.assertIn("broker_confirmed", payload["broker_confirmed_paths"]["daily_json"])
        self.assertTrue(markdown_exists)
        self.assertEqual(readiness_after, readiness_before)
        self.assertEqual(config_after, config_before)

    def test_from_readiness_propagates_paper_daily_exit_codes(self) -> None:
        cases = [(1, "BLOCKED"), (2, "ERROR")]
        for exit_code, status in cases:
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
                readiness_path = write_readiness(root / "readiness.json", config_path)

                def fake_run_paper_daily(**kwargs: object) -> PaperDailyResult:
                    config = kwargs["config"]
                    return PaperDailyResult(
                        exit_code=exit_code,
                        status=status,
                        output_path=config.output,
                        markdown_path=config.markdown_output,
                        payload={
                            "run_id": f"paper-daily-propagate-{exit_code}",
                            "status": status,
                            "exit_code": exit_code,
                            "reasons": ["simulated_paper_daily_result"],
                            "artifacts": {"daily_json": str(config.output)},
                            "broker_actions": [],
                            "final_monitor": None,
                        },
                    )

                with mock.patch("trading_ai.execution.paper_daily.run_paper_daily", side_effect=fake_run_paper_daily):
                    result = run_paper_daily_from_readiness(
                        readiness_path=readiness_path,
                        confirm_readiness=True,
                        confirm_paper=True,
                        confirm_auto_close=True,
                        confirm_auto_submit=True,
                        require_clean_state=True,
                    )

                payload = read_json(root / "paper_daily" / "broker_confirmed" / "broker_run.json")

            self.assertEqual(result.exit_code, exit_code)
            self.assertEqual(result.status, status)
            self.assertEqual(payload["exit_code"], exit_code)
            self.assertEqual(payload["status"], status)
            self.assertIn("simulated_paper_daily_result", payload["reasons"])

    def test_yaml_and_cli_overrides_resolve_paths_and_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_source = write_sample_source(root / "first.csv")
            second_source = write_sample_source(root / "second.csv")
            config_path = write_daily_config(root, source=first_source)

            config = load_paper_daily_config(
                config_path,
                source_csv=second_source,
                start="2026-04-01",
                end="2026-06-16",
                as_of_date="2026-06-16",
                session_dir=root / "override_session",
                output=root / "override.json",
            )

        self.assertEqual(config.source_csv, second_source.resolve(strict=False))
        self.assertEqual(config.start, "2026-04-01")
        self.assertEqual(config.end, "2026-06-16")
        self.assertEqual(config.as_of_date, "2026-06-16")
        self.assertEqual(config.session_dir, (root / "override_session").resolve(strict=False))
        self.assertEqual(config.output, (root / "override.json").resolve(strict=False))

    def test_missing_config_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code = main(["paper-daily", "--config", str(Path(temp_dir) / "missing.yml")])

        self.assertEqual(exit_code, 2)

    def test_without_confirmations_runs_offline_and_skips_broker_without_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("submit client should not be built"),
            ), mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("close client should not be built"),
            ):
                exit_code = main(["paper-daily", "--config", str(config_path)])
            payload = read_json(root / "paper_daily.json")
            session = read_json(root / "sessions" / "new" / "session.json")
            markdown_exists = (root / "paper_daily.md").exists()
            monitor_exists = (root / "monitor.json").exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertTrue(session["ready_for_paper_review"])
        self.assertEqual(payload["broker_actions"][0]["action"], "submit_new_session")
        self.assertEqual(payload["broker_actions"][0]["status"], "SKIPPED")
        self.assertTrue(markdown_exists)
        self.assertTrue(monitor_exists)

    def test_blocked_session_returns_one_and_does_not_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(
                root,
                source=write_sample_source(root / "source.csv"),
                extra="signal_threshold: 1.1\n",
            )

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("submit client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")

        self.assertEqual(exit_code, 1)
        self.assertIn(payload["status"], {"BLOCKED", "CRITICAL"})
        self.assertIn("paper_session_not_ready", payload["reasons"])
        self.assertEqual(payload["broker_actions"][0]["status"], "SKIPPED")

    def test_previous_submitted_execution_is_closed_before_new_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            write_submitted_session(root / "sessions" / "previous", root, client_order_id="signal-spy-20260615")
            execute_client = FakeExecutionClient()

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=[FakeCloseoutClient(), FakeCloseoutClient()],
            ), mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=execute_client,
            ):
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-close",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")
            actions = [(action["action"], action["status"]) for action in payload["broker_actions"]]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(
            actions,
            [
                ("close_previous_execution", "CLOSED"),
                ("submit_new_session", "SUBMITTED"),
                ("close_new_execution", "CLOSED"),
            ],
        )
        self.assertEqual(len(execute_client.submitted_orders), 1)

    def test_previous_nested_daily_execution_is_closed_before_new_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            write_submitted_session(
                root / "sessions" / "daily" / "2026-06-15",
                root,
                client_order_id="signal-spy-20260615",
            )
            execute_client = FakeExecutionClient()

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=[FakeCloseoutClient(), FakeCloseoutClient()],
            ), mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=execute_client,
            ):
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-close",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")
            actions = [(action["action"], action["status"]) for action in payload["broker_actions"]]

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(
            actions,
            [
                ("close_previous_execution", "CLOSED"),
                ("submit_new_session", "SUBMITTED"),
                ("close_new_execution", "CLOSED"),
            ],
        )
        self.assertEqual(len(execute_client.submitted_orders), 1)

    def test_previous_pending_closeout_blocks_new_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            write_submitted_session(root / "sessions" / "previous", root)

            with mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                return_value=FakeCloseoutClient(status="accepted", filled_qty="0", with_position=False),
            ), mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("submit client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-close",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")
            statuses = [action["status"] for action in payload["broker_actions"]]

        self.assertEqual(exit_code, 1)
        self.assertIn("PENDING", statuses)
        self.assertIn("SKIPPED", statuses)
        self.assertIn("open_execution_without_closed_closeout", payload["reasons"])

    def test_submit_operational_error_still_writes_final_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))

            with mock.patch(
                "trading_ai.execution.paper_daily.run_paper_execute_session",
                side_effect=PaperExecuteOperationalError("broker unavailable"),
            ):
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")
            monitor = read_json(root / "monitor.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(monitor["status"], "WARN")
        self.assertIn("broker unavailable", payload["reasons"])

    def test_monitor_error_before_submit_returns_two_and_does_not_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))
            submit_result = SimpleNamespace(
                status="SUBMITTED",
                exit_code=0,
                json_path=root / "execution.json",
                markdown_path=root / "execution.md",
                reasons=[],
            )
            with mock.patch(
                "trading_ai.execution.paper_daily.run_paper_monitor",
                side_effect=[
                    monitor_result(root, status="ERROR", exit_code=2),
                    monitor_result(root, status="OK", exit_code=0),
                ],
            ), mock.patch(
                "trading_ai.execution.paper_daily.run_paper_execute_session",
                return_value=submit_result,
            ) as submit:
                exit_code = main(
                    [
                        "paper-daily",
                        "--config",
                        str(config_path),
                        "--confirm-paper",
                        "--confirm-auto-submit",
                    ]
                )
            payload = read_json(root / "paper_daily.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("monitor_before_submit_operational_error", payload["reasons"])
        self.assertEqual(payload["broker_actions"][0]["status"], "SKIPPED")
        submit.assert_not_called()

    def test_ledger_output_writes_redacted_paper_daily_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "ledger.jsonl"
            config_path = write_daily_config(root, source=write_sample_source(root / "source.csv"))

            exit_code = main(["paper-daily", "--config", str(config_path), "--ledger-output", str(ledger)])
            events = read_jsonl(ledger)

        self.assertEqual(exit_code, 0)
        self.assertEqual(events[-1]["event_type"], "paper_daily")
        self.assertIn(events[-1]["status"], {"WARN", "OK"})
        self.assertNotIn("account", json.dumps(events))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json.dumps(events))


def write_daily_config(root: Path, *, source: Path, extra: str = "") -> Path:
    universe = write_universe(root / "universe.yml")
    risk = write_risk(root / "risk.yml")
    model = write_buy_model(root / "model.json")
    config = root / "paper_daily.yml"
    config.write_text(
        textwrap.dedent(
            f"""
            paper_daily:
              source_csv: {source}
              from: "2026-03-01"
              to: "2026-06-16"
              as_of_date: "2026-06-16"
              session_dir: {root / "sessions" / "new"}
              sessions_root: {root / "sessions"}
              output: {root / "paper_daily.json"}
              markdown_output: {root / "paper_daily.md"}
              observability_output: {root / "observability.json"}
              observability_markdown_output: {root / "observability.md"}
              monitor_output: {root / "monitor.json"}
              monitor_markdown_output: {root / "monitor.md"}
              config: {universe}
              risk: {risk}
              signal_model: {model}
              max_age_days: 5
              max_feature_age_days: 5
            """
        )
        + textwrap.indent(extra, "  "),
        encoding="utf-8",
    )
    return config


def write_readiness(
    path: Path,
    config_path: Path,
    *,
    status: str = "READY",
    ready_for_paper_daily: bool = True,
    exit_code: int = 0,
    smoke_requested: bool = True,
    smoke_ran: bool = True,
    smoke_exit_code: int = 0,
    reasons: list[str] | None = None,
) -> Path:
    write_json(
        path,
        {
            "schema_version": 1,
            "generated_at": "2026-06-16T00:00:00+00:00",
            "status": status,
            "ready_for_paper_daily": ready_for_paper_daily,
            "exit_code": exit_code,
            "as_of_date": "2026-06-16",
            "paper_daily_config_path": str(config_path),
            "offline_smoke": {
                "requested": smoke_requested,
                "ran": smoke_ran,
                "status": "OK" if smoke_exit_code == 0 else "BLOCKED",
                "exit_code": smoke_exit_code,
                "config_path": str(config_path),
                "artifacts": {
                    "daily_json": str(path.parent / "paper_daily" / "daily.json"),
                    "session_dir": str(path.parent / "paper_daily" / "sessions" / "daily" / "2026-06-16"),
                    "monitor_json": str(path.parent / "paper_daily" / "monitor.json"),
                },
                "reasons": [],
            },
            "reasons": reasons or [],
        },
    )
    return path


def write_universe(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            universe:
              symbols: [SPY]
            """
        ),
        encoding="utf-8",
    )
    return path


def write_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def write_buy_model(path: Path) -> Path:
    save_model(
        LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
        str(path),
    )
    return path


def write_sample_source(path: Path, *, end: str = "2026-06-16") -> Path:
    write_records(generate_sample_ohlcv(symbols=("SPY",), start="2026-03-01", end=end), path)
    return path


def write_submitted_session(session_dir: Path, root: Path, *, client_order_id: str = "signal-spy-20260615") -> Path:
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    (session_dir / "execution").mkdir()
    signal = signal_report(client_order_id=client_order_id)
    write_json(
        session_dir / "session.json",
        {
            "schema_version": "1.0",
            "output_dir": str(session_dir),
            "as_of_date": "2026-06-15",
            "ready_for_paper_review": True,
            "exit_code": 0,
            "inputs": {"config": str(root / "universe.yml"), "risk": str(root / "risk.yml")},
            "paths": {
                "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
                "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
                "audit_report": str(session_dir / "audit" / "paper_audit.json"),
            },
            "summary": {"fail_count": 0, "freshness_allowed": True},
        },
    )
    write_json(
        session_dir / "audit" / "paper_audit.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-15T00:01:00+00:00",
            "ready_for_paper_review": True,
            "findings": [],
            "summary": {"fail_count": 0, "freshness_allowed": True},
        },
    )
    write_json(session_dir / "paper" / "paper_signal_order.json", signal)
    write_json(session_dir / "fresh_data" / "freshness.json", {"allowed": True, "reasons": []})
    write_json(
        session_dir / "execution" / "paper_execution.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-15T00:02:00+00:00",
            "status": "SUBMITTED",
            "session": {"session_dir": str(session_dir), "ready_for_paper_review": True},
            "preflight": {"allowed": True, "reasons": []},
            "order_sent": signal["order_intent"],
            "broker_result": {"accepted": True, "status": "submitted", "reasons": []},
        },
    )
    return session_dir


def signal_report(*, client_order_id: str = "signal-spy-20260615") -> dict[str, object]:
    return {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": True,
        "preflight": {"allowed": True, "reasons": [], "checked_at": "2026-06-15", "max_feature_age_days": 5},
        "open_orders": [],
        "positions": [],
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-15",
            "symbol": "SPY",
            "probability": 0.93,
            "threshold": 0.5,
            "action": "buy",
        },
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": client_order_id,
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


def account() -> object:
    class Account:
        id = "paper-account"
        status = "ACTIVE"
        cash = "10000.00"
        equity = "10000.00"
        buying_power = "9999.00"

    return Account()


def broker_order(
    *,
    client_order_id: str,
    symbol: str = "SPY",
    status: str = "filled",
    filled_qty: str = "0.002",
) -> dict[str, object]:
    return {
        "id": "broker-order-1",
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "status": status,
        "notional": "1.0",
        "qty": None,
        "filled_qty": filled_qty,
        "filled_avg_price": "500.0" if filled_qty != "0" else None,
        "submitted_at": "2026-06-16T22:07:42Z",
        "created_at": "2026-06-16T22:07:42Z",
        "updated_at": "2026-06-16T22:07:43Z",
        "expires_at": "2026-06-17T20:00:00Z",
    }


def monitor_result(root: Path, *, status: str, exit_code: int) -> PaperMonitorResult:
    return PaperMonitorResult(
        exit_code=exit_code,
        status=status,
        output_path=root / "monitor.json",
        markdown_path=root / "monitor.md",
        dashboard={
            "status": status,
            "exit_code": exit_code,
            "monitor_summary": {
                "action_required": "resolve_operational_error" if status == "ERROR" else "continue_daily_flow"
            },
            "alerts": [],
            "telegram": {"status": "SKIPPED"},
        },
    )


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
