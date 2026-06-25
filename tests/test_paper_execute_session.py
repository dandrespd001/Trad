import hashlib
import json
import os
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_execute_session import run_paper_execute_session
from trading_ai.execution.paper_risk_state import RiskState, save_risk_state


class FakeMarketDataClient:
    """Returns a fixed latest-trade price, matching the fixture's reference price."""

    def __init__(self, *, price: float = 100.0) -> None:
        self.price = price

    def get_stock_latest_trade(self, request: object) -> dict[str, object]:
        class Trade:
            price = self.price

        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: Trade()}


class FakeApprovedExecutionClient:
    def __init__(self, *, positions: list[object] | None = None, equity: str = "10000.00") -> None:
        self.submitted_orders: list[dict[str, object]] = []
        self.get_orders_calls: list[object] = []
        self._positions = positions or []
        self._equity = equity

    def get_account(self) -> object:
        equity_value = self._equity

        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = equity_value
            equity = equity_value
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        return self._positions

    def get_orders(self, filter: object | None = None) -> list[object]:
        self.get_orders_calls.append(filter)
        return []

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted_orders.append(kwargs)
        return {"id": "broker-order-1", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        if not self.submitted_orders:
            raise AssertionError("order status read happened before submit")
        submitted = self.submitted_orders[-1]
        if submitted["client_order_id"] != client_order_id:
            raise AssertionError("unexpected client_order_id")
        return {
            "id": "broker-order-1",
            "client_order_id": client_order_id,
            "symbol": submitted["symbol"],
            "side": submitted["side"],
            "type": submitted["type"],
            "time_in_force": submitted["time_in_force"],
            "status": "accepted",
            "notional": submitted.get("notional"),
            "qty": submitted.get("qty"),
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": "2026-06-16T22:07:42.667183Z",
            "created_at": "2026-06-16T22:07:42.667183Z",
            "updated_at": "2026-06-16T22:07:42.668584Z",
            "expires_at": "2026-06-17T20:00:00Z",
        }


class FakeOpenOrderBlockingClient(FakeApprovedExecutionClient):
    def get_orders(self, filter: object | None = None) -> list[dict[str, object]]:
        self.get_orders_calls.append(filter)
        return [
            {
                "id": "open-order-1",
                "client_order_id": "other-order",
                "symbol": "SPY",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "status": "accepted",
                "notional": "1.0",
                "qty": None,
                "filled_qty": "0",
                "filled_avg_price": None,
                "submitted_at": "2026-06-16T22:07:42.667183Z",
                "created_at": "2026-06-16T22:07:42.667183Z",
                "updated_at": "2026-06-16T22:07:42.668584Z",
                "expires_at": "2026-06-17T20:00:00Z",
            }
        ]

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("preflight should block submit_order")


class Position:
    def __init__(self, symbol: str, qty: str = "0.5", market_value: str = "50.00") -> None:
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value


class PaperExecuteSessionTests(unittest.TestCase):
    def test_parser_defaults_keep_execution_explicit_and_bounded(self) -> None:
        args = build_parser().parse_args(["paper-execute-session", "--session-dir", "reports/tmp/paper_session/latest"])

        self.assertEqual(args.output_dir, None)
        self.assertEqual(args.as_of_date, "today")
        self.assertEqual(args.max_feature_age_days, 5)
        self.assertFalse(args.confirm_paper)
        self.assertFalse(args.confirm_submit)

    def test_approved_session_submits_exact_paper_order_and_writes_evidence(self) -> None:
        client = FakeApprovedExecutionClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ) as build_client,
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")
            markdown = (session_dir / "execution" / "paper_execution.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(build_client.call_count, 1)
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(
            client.submitted_orders[0],
            {
                "symbol": "SPY",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": "signal-spy-20260616",
                "notional": 1.0,
            },
        )
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(payload["order_sent"]["client_order_id"], "signal-spy-20260616")
        self.assertEqual(payload["order_sent"]["notional"], 1.0)
        self.assertTrue(payload["preflight"]["allowed"])
        self.assertEqual(payload["broker_result"]["status"], "submitted")
        self.assertEqual(payload["final_order"]["client_order_id"], "signal-spy-20260616")
        self.assertIn("Status: **SUBMITTED**", markdown)
        self.assertIn("Client order ID: `signal-spy-20260616`", markdown)

    def test_existing_selected_position_is_held_without_duplicate_open(self) -> None:
        client = FakeApprovedExecutionClient(positions=[Position("SPY")])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "HELD")
        self.assertEqual(len(client.submitted_orders), 0)
        self.assertEqual(payload["position_plan"]["summary"]["hold_count"], 1)

    def test_dynamic_close_requires_extra_confirmation(self) -> None:
        client = FakeApprovedExecutionClient(positions=[Position("QQQ")])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, universe_symbols=("SPY", "QQQ"))

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertEqual(payload["position_plan"]["summary"]["close_count"], 1)
        self.assertEqual(len(client.submitted_orders), 0)

    def test_dynamic_close_and_open_with_extra_confirmation(self) -> None:
        client = FakeApprovedExecutionClient(positions=[Position("QQQ", qty="0.25")])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, universe_symbols=("SPY", "QQQ"))
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--confirm-dynamic-position-actions",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(len(client.submitted_orders), 2)
        self.assertEqual(client.submitted_orders[0]["symbol"], "QQQ")
        self.assertEqual(client.submitted_orders[0]["side"], "sell")
        self.assertEqual(client.submitted_orders[0]["qty"], 0.25)
        self.assertEqual(client.submitted_orders[1]["symbol"], "SPY")
        self.assertEqual(client.submitted_orders[1]["side"], "buy")
        self.assertEqual(payload["position_plan"]["summary"]["close_count"], 1)
        self.assertEqual(payload["position_plan"]["summary"]["open_count"], 1)

    def test_missing_confirmations_return_two_without_client_or_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-submit",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse((session_dir / "execution").exists())

    def test_missing_confirm_submit_returns_two_without_client_or_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse((session_dir / "execution").exists())

    def test_custom_output_dir_writes_execution_evidence_without_touching_session_package(self) -> None:
        client = FakeApprovedExecutionClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)
            output_dir = root / "custom_execution"
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)
            offline_artifacts = {
                path: path.read_text(encoding="utf-8")
                for path in (
                    session_dir / "session.json",
                    session_dir / "audit" / "paper_audit.json",
                    session_dir / "paper" / "paper_signal_order.json",
                    session_dir / "fresh_data" / "freshness.json",
                )
            }

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--output-dir",
                        str(output_dir),
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )
            payload = read_json(output_dir / "paper_execution.json")
            markdown = (output_dir / "paper_execution.md").read_text(encoding="utf-8")
            default_execution_exists = (session_dir / "execution").exists()
            after_offline_artifacts = {path: path.read_text(encoding="utf-8") for path in offline_artifacts}

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(payload["output_dir"], str(output_dir))
        self.assertIn("Status: **SUBMITTED**", markdown)
        self.assertFalse(default_execution_exists)
        for path, before in offline_artifacts.items():
            self.assertEqual(after_offline_artifacts[path], before)

    def test_blocked_session_returns_one_without_building_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, ready=False, fail_count=1)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse((session_dir / "execution").exists())

    def test_real_preflight_open_order_blocks_and_writes_evidence_without_submit(self) -> None:
        client = FakeOpenOrderBlockingClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(client.submitted_orders, [])
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("open_order_exists", payload["preflight"]["reasons"])
        self.assertIsNone(payload["broker_result"])
        self.assertEqual(payload["open_orders"][0]["symbol"], "SPY")

    def test_broker_connection_error_writes_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=RuntimeError("broker unavailable secret=DO-NOT-KEEP"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("broker unavailable", payload["operational_error"])
        self.assertNotIn("DO-NOT-KEEP", json.dumps(payload))
        self.assertIsNone(payload["broker_result"])

    def test_manipulated_order_is_rejected_locally_without_client(self) -> None:
        cases = [
            ("symbol", "TSLA", "symbol_not_allowlisted"),
            ("side", "sell", "unsupported_order_side"),
            ("type", "limit", "unsupported_order_type"),
            ("notional", 1.01, "notional_exceeds_limit"),
        ]
        for field, value, reason in cases:
            with self.subTest(field=field, reason=reason):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    session_dir = write_approved_session(root)
                    signal = read_json(session_dir / "paper" / "paper_signal_order.json")
                    signal["order_intent"][field] = value
                    if field == "symbol":
                        signal["selected_signal"]["symbol"] = value
                    (session_dir / "paper" / "paper_signal_order.json").write_text(
                        json.dumps(signal, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )

                    with mock.patch(
                        "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                        side_effect=AssertionError("client should not be built"),
                    ):
                        exit_code = main(
                            [
                                "paper-execute-session",
                                "--session-dir",
                                str(session_dir),
                                "--confirm-paper",
                                "--confirm-submit",
                            ]
                        )

                self.assertEqual(exit_code, 1)
                self.assertFalse((session_dir / "execution").exists())

    def test_custom_paper_notional_from_risk_limits_is_enforced(self) -> None:
        client = FakeApprovedExecutionClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, signal_notional=2.0, risk_notional=2.0)
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

            self.assertEqual(payload["order_sent"]["notional"], 2.0)
            self.assertEqual(payload["status"], "SUBMITTED")
            self.assertEqual(payload["broker_result"]["broker_response"]["notional"], 2.0)
            self.assertEqual(exit_code, 0)
            self.assertEqual(client.submitted_orders[0]["notional"], 2.0)

    def test_session_fails_when_campaign_evidence_hash_changes_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, signal_notional=2.0, risk_notional=2.0)
            campaign = root / "campaign.json"
            campaign_payload = read_json(campaign)
            campaign_payload["real_money_consideration"]["recovery_days"] = 1
            campaign.write_text(json.dumps(campaign_payload, indent=2, sort_keys=True), encoding="utf-8")

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertFalse((session_dir / "execution").exists())

    def test_session_fails_when_signal_notional_does_not_match_risk_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, signal_notional=1.0, risk_notional=2.0)

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse((session_dir / "execution").exists())

    def test_invalid_json_returns_two_without_execution_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)
            (session_dir / "session.json").write_text("{bad json", encoding="utf-8")

            exit_code = main(
                [
                    "paper-execute-session",
                    "--session-dir",
                    str(session_dir),
                    "--confirm-paper",
                    "--confirm-submit",
                    "--as-of-date",
                    "2026-06-16",
                ]
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse((session_dir / "execution").exists())

    def test_session_artifact_path_outside_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)
            outside_signal = root / "outside_signal.json"
            outside_signal.write_text(
                (session_dir / "paper" / "paper_signal_order.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            session = read_json(session_dir / "session.json")
            session["paths"]["signal_report"] = str(outside_signal)
            (session_dir / "session.json").write_text(
                json.dumps(session, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse((session_dir / "execution").exists())

    def test_legacy_relative_session_paths_resolve_from_working_directory(self) -> None:
        client = FakeApprovedExecutionClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root)
            session = read_json(session_dir / "session.json")
            session["inputs"]["config"] = "universe.yml"
            session["inputs"]["risk"] = "risk.yml"
            (session_dir / "session.json").write_text(
                json.dumps(session, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                working_directory(root),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(client.submitted_orders), 1)

    def test_fixed_notional_sizing_mode_explicit_matches_default_behavior(self) -> None:
        client = FakeApprovedExecutionClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(root, sizing_mode="fixed_notional")
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(payload["order_sent"]["notional"], 1.0)
        self.assertEqual(client.submitted_orders[0]["notional"], 1.0)

    def test_vol_target_sizing_computes_notional_from_equity_and_volatility(self) -> None:
        client = FakeApprovedExecutionClient(equity="10.00")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(
                root,
                sizing_mode="vol_target",
                risk_notional=5.0,
                target_volatility=0.20,
                max_leverage=1.0,
                realized_volatility=0.40,
            )
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        # weight = min(0.20/0.40, max_leverage=1.0) = 0.5, capped by max_single_position
        # 0.30 -> 0.30; notional = equity(10.0) * 0.30 = 3.0, well under stage_cap_usd (5.0).
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(payload["order_sent"]["notional"], 3.0)
        self.assertEqual(client.submitted_orders[0]["notional"], 3.0)

    def test_vol_target_notional_never_exceeds_stage_cap(self) -> None:
        client = FakeApprovedExecutionClient(equity="100000.00")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(
                root,
                sizing_mode="vol_target",
                risk_notional=5.0,
                target_volatility=0.20,
                max_leverage=1.0,
                realized_volatility=0.40,
            )
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                        "--risk-state-path",
                        str(risk_state_path),
                    ]
                )

            payload = read_json(session_dir / "execution" / "paper_execution.json")

        # 100000 * 0.30 = 30000, far above the stage_cap_usd (5.0) cap -> clamped to 5.0.
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order_sent"]["notional"], 5.0)
        self.assertEqual(client.submitted_orders[0]["notional"], 5.0)

    def test_vol_target_preapproved_notional_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(
                root,
                sizing_mode="vol_target",
                risk_notional=5.0,
                target_volatility=0.20,
                max_leverage=1.0,
                realized_volatility=0.40,
                order_intent_overrides={"notional": 5.0},
            )

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse((session_dir / "execution").exists())

    def test_vol_target_policy_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(
                root,
                sizing_mode="vol_target",
                risk_notional=5.0,
                target_volatility=0.20,
                max_leverage=1.0,
                realized_volatility=0.40,
                # Approved order intent claims a different target_volatility than the
                # currently active risk_limits -- a stale or manipulated policy.
                order_intent_overrides={"target_volatility": 0.99},
            )

            with mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("client should not be built"),
            ):
                exit_code = main(
                    [
                        "paper-execute-session",
                        "--session-dir",
                        str(session_dir),
                        "--confirm-paper",
                        "--confirm-submit",
                        "--as-of-date",
                        "2026-06-16",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse((session_dir / "execution").exists())

    def test_vol_target_without_matching_open_action_blocks_with_missing_notional(self) -> None:
        client = FakeApprovedExecutionClient(positions=[Position("SPY")], equity="10.00")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = write_approved_session(
                root,
                sizing_mode="vol_target",
                risk_notional=5.0,
                target_volatility=0.20,
                max_leverage=1.0,
                realized_volatility=0.40,
            )
            risk_state_path = root / "risk_state.json"
            save_risk_state(RiskState(), risk_state_path)

            with (
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                    return_value=client,
                ),
                mock.patch(
                    "trading_ai.execution.paper_execute_session.build_alpaca_market_data_client",
                    return_value=FakeMarketDataClient(),
                ),
            ):
                result = run_paper_execute_session(
                    session_dir=session_dir,
                    confirm_paper=True,
                    confirm_submit=True,
                    confirm_dynamic_position_actions=True,
                    as_of_date="2026-06-16",
                    risk_state_path=risk_state_path,
                )

        # The symbol is already held, so build_position_plan emits a HOLD action, not an
        # OPEN one -- there is no notional to resolve, and the order must never be sent
        # with notional=None.
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.reasons, ("missing_notional",))
        self.assertEqual(client.submitted_orders, [])


def write_approved_session(
    root: Path,
    *,
    ready: bool = True,
    fail_count: int = 0,
    freshness_allowed: bool = True,
    symbol: str = "SPY",
    universe_symbols: tuple[str, ...] = ("SPY",),
    signal_notional: float = 1.0,
    risk_notional: float = 1.0,
    sizing_mode: str = "fixed_notional",
    target_volatility: float = 0.0,
    max_leverage: float = 1.0,
    realized_volatility: float | None = None,
    order_intent_overrides: dict[str, object] | None = None,
) -> Path:
    session_dir = root / "paper_session"
    (session_dir / "audit").mkdir(parents=True)
    (session_dir / "paper").mkdir()
    (session_dir / "fresh_data").mkdir()
    config = write_universe(root / "universe.yml", universe_symbols)
    risk = write_risk(
        root / "risk.yml",
        paper_notional_usd=risk_notional,
        sizing_mode=sizing_mode,
        target_volatility=target_volatility,
        max_leverage=max_leverage,
    )
    campaign = None
    if risk_notional != 1.0:
        campaign = root / "campaign.json"
        campaign.write_text(
            json.dumps(
                {
                    "real_money_consideration": {
                        "state": "PAPER_EVIDENCE_READY",
                        "clean_trial_days": 30,
                        "target_trial_days": 30,
                        "recovery_days": 0,
                        "error_days": 0,
                    }
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    graduation = paper_graduation_payload(paper_notional_usd=risk_notional, campaign=campaign)
    session = {
        "schema_version": "1.0",
        "output_dir": str(session_dir),
        "as_of_date": "2026-06-16",
        "ready_for_paper_review": ready,
        "exit_code": 0 if ready else 1,
        "inputs": {
            "config": str(config),
            "risk": str(risk),
            "campaign_report": str(campaign) if campaign is not None else None,
            "phase_review": None,
        },
        "paths": {
            "freshness_report": str(session_dir / "fresh_data" / "freshness.json"),
            "signal_report": str(session_dir / "paper" / "paper_signal_order.json"),
            "audit_report": str(session_dir / "audit" / "paper_audit.json"),
        },
        "summary": {"fail_count": fail_count, "freshness_allowed": freshness_allowed},
        "paper_graduation": graduation,
    }
    audit = {
        "schema_version": "1.0",
        "ready_for_paper_review": ready,
        "findings": [],
        "summary": {
            "fail_count": fail_count,
            "freshness_allowed": freshness_allowed,
            "selected_symbol": symbol,
            "signal_action": "buy",
            "order_accepted": True,
        },
    }
    signal = {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": freshness_allowed,
        "preflight": {"allowed": True, "reasons": [], "checked_at": "2026-06-16", "max_feature_age_days": 5},
        "open_orders": [],
        "positions": [],
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-16",
            "symbol": symbol,
            "probability": 0.93,
            "threshold": 0.5,
            "action": "buy",
            **({"realized_volatility": realized_volatility} if realized_volatility is not None else {}),
        },
        "signals": [
            {
                "timestamp": "2026-06-16",
                "symbol": symbol,
                "probability": 0.93,
                "threshold": 0.5,
                "action": "buy",
            },
            *[
                {
                    "timestamp": "2026-06-16",
                    "symbol": other_symbol,
                    "probability": 0.41,
                    "threshold": 0.5,
                    "action": "hold",
                }
                for other_symbol in universe_symbols
                if other_symbol != symbol
            ],
        ],
        "signal_quality": {
            "allowed": True,
            "reasons": [],
            "buy_signal_count": 1,
            "max_buy_signals": 3,
            "selected_margin": 0.43,
            "min_signal_margin": 0.05,
        },
        "order_intent": _order_intent_payload(
            symbol=symbol,
            sizing_mode=sizing_mode,
            signal_notional=signal_notional,
            risk_notional=risk_notional,
            target_volatility=target_volatility,
            max_leverage=max_leverage,
            overrides=order_intent_overrides,
        ),
        "order_result": {
            "accepted": True,
            "status": "dry_run_accepted",
            "reasons": [],
            "dry_run": True,
            "broker_response": None,
        },
        "account": {"dry_run": True},
        "paper_graduation": graduation,
    }
    freshness = {"allowed": freshness_allowed, "reasons": [] if freshness_allowed else ["stale_symbol"]}
    (session_dir / "session.json").write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    (session_dir / "audit" / "paper_audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (session_dir / "paper" / "paper_signal_order.json").write_text(
        json.dumps(signal, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (session_dir / "fresh_data" / "freshness.json").write_text(
        json.dumps(freshness, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return session_dir


def _order_intent_payload(
    *,
    symbol: str,
    sizing_mode: str,
    signal_notional: float,
    risk_notional: float,
    target_volatility: float,
    max_leverage: float,
    overrides: dict[str, object] | None,
) -> dict[str, object]:
    base = {
        "symbol": symbol,
        "side": "buy",
        "client_order_id": "signal-spy-20260616",
        "type": "market",
        "time_in_force": "day",
        "reference_price": 100.0,
    }
    if sizing_mode == "vol_target":
        base.update(
            {
                "sizing_mode": "vol_target",
                "target_volatility": target_volatility,
                "max_leverage": max_leverage,
                "max_single_position": 0.30,
                "stage_cap_usd": risk_notional,
            }
        )
    else:
        base["notional"] = signal_notional
    if overrides:
        base.update(overrides)
    return base


def write_universe(path: Path, symbols: tuple[str, ...]) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(symbols)}]
            """
        ),
        encoding="utf-8",
    )
    return path


def write_risk(
    path: Path,
    *,
    paper_notional_usd: float = 1.0,
    sizing_mode: str = "fixed_notional",
    target_volatility: float = 0.0,
    max_leverage: float = 1.0,
) -> Path:
    stage_lines = ""
    if paper_notional_usd != 1.0:
        stage_lines = """
              paper_stage: SCALE_UP
              paper_stage_reviewer: reviewer@example.com
              paper_stage_reason: clean paper campaign
"""
    sizing_lines = ""
    if sizing_mode != "fixed_notional":
        sizing_lines = f"""
              sizing_mode: {sizing_mode}
              target_volatility: {target_volatility}
              max_leverage: {max_leverage}
"""
    path.write_text(
        textwrap.dedent(
            f"""
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              paper_notional_usd: {paper_notional_usd}
{stage_lines.rstrip()}
{sizing_lines.rstrip()}
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def paper_graduation_payload(*, paper_notional_usd: float, campaign: Path | None = None) -> dict[str, object]:
    stage = "CANARY" if paper_notional_usd == 1.0 else "SCALE_UP"
    campaign_evidence: dict[str, object] = {"provided": stage != "CANARY"}
    if campaign is not None:
        campaign_evidence["path"] = str(campaign)
        campaign_evidence["sha256"] = sha256_file(campaign)
    return {
        "stage": stage,
        "paper_notional_usd": paper_notional_usd,
        "stage_cap_usd": 1.0 if stage == "CANARY" else 5.0,
        "reviewer": None if stage == "CANARY" else "reviewer@example.com",
        "reason": None if stage == "CANARY" else "clean paper campaign",
        "allowed": True,
        "blockers": [],
        "evidence": {"campaign_report": campaign_evidence, "phase_review": {"provided": False}},
    }


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
