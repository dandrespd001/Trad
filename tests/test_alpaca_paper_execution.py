import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data.io import write_records
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder, PaperOrderSnapshot, PaperPosition
from trading_ai.models.baseline import LogisticBaselineModel, save_model
from trading_ai.risk.policy import RiskLimits


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.orders: list[dict[str, object]] = []

    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10500.00"
            buying_power = "20000.00"

        return Account()

    def list_positions(self) -> list[object]:
        class Position:
            def __init__(self, symbol: str, qty: str, market_value: str) -> None:
                self.symbol = symbol
                self.qty = qty
                self.market_value = market_value

        return [Position("SPY", "3", "1500.00"), Position("QQQ", "2", "900.00")]

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.orders.append(kwargs)
        return {"id": "broker-order-1", **kwargs}

    def cancel_order_by_id(self, client_order_id: str) -> dict[str, object]:
        self.cancelled.append(client_order_id)
        return {"cancelled": client_order_id}


class FakeMarketDataClient:
    """Returns a fixed latest-trade price, matching the order's reference price."""

    def __init__(self, *, price: float = 1.0) -> None:
        self.price = price
        self.requests: list[object] = []

    def get_stock_latest_trade(self, request: object) -> dict[str, object]:
        self.requests.append(request)

        class Trade:
            price = self.price

        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: Trade()}


class FakeAlpacaPyOrderRequestClient:
    def __init__(self) -> None:
        self.orders: list[object] = []

    def submit_order(self, order_data: object) -> dict[str, object]:
        self.orders.append(order_data)
        notional = getattr(order_data, "notional", None)
        if notional is None and isinstance(order_data, dict):
            notional = order_data.get("notional")
        return {"id": "broker-order-1", "notional": notional}


class FakeAlpacaOrderManagementClient:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.filters: list[object] = []
        self.order = {
            "id": "broker-order-1",
            "client_order_id": "signal-spy-20240329",
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "order_type": "market",
            "time_in_force": "day",
            "status": "accepted",
            "notional": "1",
            "qty": None,
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": "2026-06-16T22:07:42.667183Z",
            "created_at": "2026-06-16T22:07:42.667183Z",
            "updated_at": "2026-06-16T22:07:42.668584Z",
            "expires_at": "2026-06-17T20:00:00Z",
        }

    def get_account(self) -> object:
        class Account:
            id = "paper-account"
            status = "ACTIVE"
            cash = "10000.00"
            equity = "10000.00"
            buying_power = "9999.00"

        return Account()

    def list_positions(self) -> list[object]:
        return []

    def get_orders(self, filter: object | None = None) -> list[dict[str, object]]:
        self.filters.append(filter)
        return [self.order]

    def get_order_by_id(self, order_id: str, filter: object | None = None) -> dict[str, object]:
        self.filters.append(filter)
        if order_id != self.order["id"]:
            raise ValueError("order not found")
        return self.order

    def get_order_by_client_id(self, client_id: str) -> dict[str, object]:
        if client_id != self.order["client_order_id"]:
            raise ValueError("order not found")
        return self.order

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)


class FakeOpenOrderForSymbolClient(FakeAlpacaOrderManagementClient):
    def __init__(self) -> None:
        super().__init__()
        self.submitted = False
        self.order = {**self.order, "client_order_id": "other-open-order"}

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted = True
        raise AssertionError("preflight should block submit_order")


class FakePositionBlockingClient(FakeAlpacaOrderManagementClient):
    def __init__(self) -> None:
        super().__init__()
        self.submitted = False

    def get_orders(self, filter: object | None = None) -> list[dict[str, object]]:
        self.filters.append(filter)
        return []

    def list_positions(self) -> list[object]:
        class Position:
            symbol = "SPY"
            qty = "1"
            market_value = "500.00"

        return [Position()]

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submitted = True
        raise AssertionError("preflight should block submit_order")


class AlpacaPaperExecutionTests(unittest.TestCase):
    def test_paper_cli_default_output_uses_tmp_latest_report(self) -> None:
        args = build_parser().parse_args(["paper", "--broker", "alpaca", "--dry-run", "--list-orders"])

        self.assertEqual(args.output, "reports/tmp/paper/latest.json")

    def test_paper_cli_rejects_conflicting_dry_run_and_real_paper_modes(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["paper", "--broker", "alpaca", "--dry-run", "--real-paper"])

    def test_read_account_normalizes_broker_account_snapshot(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY", "QQQ"),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        account = broker.read_account()

        self.assertEqual(account.account_id, "paper-account")
        self.assertEqual(account.status, "active")
        self.assertEqual(account.cash, 10000.0)
        self.assertEqual(account.equity, 10500.0)
        self.assertEqual(account.buying_power, 20000.0)

    def test_read_positions_normalizes_allowlisted_positions(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY", "QQQ"),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        positions = broker.read_positions()

        self.assertEqual([position.symbol for position in positions], ["SPY", "QQQ"])
        self.assertEqual(positions[0].quantity, 3.0)
        self.assertEqual(positions[0].market_value, 1500.0)

    def test_read_positions_supports_alpaca_py_get_all_positions(self) -> None:
        class AlpacaPyClient:
            def get_all_positions(self) -> list[object]:
                class Position:
                    def __init__(self, symbol: str, qty: str, market_value: str) -> None:
                        self.symbol = symbol
                        self.qty = qty
                        self.market_value = market_value

                return [Position("SPY", "3", "1500.00"), Position("TSLA", "2", "400.00")]

        broker = AlpacaPaperBroker(
            client=AlpacaPyClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        positions = broker.read_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "SPY")

    def test_cancel_order_is_idempotent_and_only_calls_broker_once(self) -> None:
        client = FakeAlpacaClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        first = broker.cancel_order("order-1")
        second = broker.cancel_order("order-1")

        self.assertTrue(first.accepted)
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(second.status, "duplicate_cancelled")
        self.assertEqual(client.cancelled, ["order-1"])

    def test_kill_switch_rejects_new_orders_but_still_allows_cancellation(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )
        broker.activate_kill_switch("manual_test")

        order_result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", quantity=1, client_order_id="o-1"))
        cancel_result = broker.cancel_order("o-1")

        self.assertFalse(order_result.accepted)
        self.assertIn("kill_switch_active", order_result.reasons)
        self.assertTrue(cancel_result.accepted)

    def test_buy_order_is_rejected_when_today_is_not_a_trading_day(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 3, 30),  # Saturday
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"))

        self.assertFalse(result.accepted)
        self.assertIn("market_closed_not_a_trading_day", result.reasons)

    def test_buy_order_is_accepted_when_today_is_a_trading_day(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"))

        self.assertTrue(result.accepted)

    def test_sell_order_is_not_blocked_when_today_is_not_a_trading_day(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 3, 30),  # Saturday
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="sell", notional=1.0, client_order_id="o-1"))

        self.assertNotIn("market_closed_not_a_trading_day", result.reasons)

    def test_real_buy_order_is_accepted_when_live_price_is_within_band(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
            market_data=FakeMarketDataClient(price=101.0),
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=100.0)
        )

        self.assertTrue(result.accepted)

    def test_real_buy_order_is_rejected_when_live_price_exceeds_deviation_band(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
            market_data=FakeMarketDataClient(price=110.0),
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=100.0)
        )

        self.assertFalse(result.accepted)
        self.assertIn("price_sanity_band_exceeded", result.reasons)

    def test_real_buy_order_is_rejected_without_market_data_client(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=100.0)
        )

        self.assertFalse(result.accepted)
        self.assertIn("market_data_unavailable", result.reasons)

    def test_real_buy_order_is_rejected_when_reference_price_is_missing(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
            market_data=FakeMarketDataClient(price=100.0),
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"))

        self.assertFalse(result.accepted)
        self.assertIn("price_sanity_reference_missing", result.reasons)

    def test_real_sell_order_is_not_blocked_without_market_data_client(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="sell", quantity=1, client_order_id="o-1"))

        self.assertNotIn("market_data_unavailable", result.reasons)

    def test_dry_run_buy_order_does_not_require_market_data(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"))

        self.assertTrue(result.accepted)

    def test_notional_dry_run_order_is_accepted_inside_risk_limits(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
        )

        result = broker.submit_order(PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1"))

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "dry_run_accepted")

    def test_real_paper_notional_order_supports_alpaca_py_request_object(self) -> None:
        client = FakeAlpacaPyOrderRequestClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            market_data=FakeMarketDataClient(price=1.0),
        )

        result = broker.submit_order(
            PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=1.0)
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(len(client.orders), 1)
        order_data = client.orders[0]
        notional = getattr(order_data, "notional", None)
        symbol = getattr(order_data, "symbol", None)
        if isinstance(order_data, dict):
            notional = order_data.get("notional")
            symbol = order_data.get("symbol")
        self.assertEqual(symbol, "SPY")
        self.assertEqual(notional, 1.0)

    def test_reconcile_positions_reports_quantity_mismatches_and_unexpected_symbols(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaClient(),
            allowlist=("SPY", "QQQ"),
            risk_limits=RiskLimits(),
            dry_run=False,
        )
        expected = (
            PaperPosition(symbol="SPY", quantity=3.0, market_value=1500.0),
            PaperPosition(symbol="IWM", quantity=1.0, market_value=200.0),
        )

        report = broker.reconcile_positions(expected)

        self.assertFalse(report.matched)
        self.assertIn("unexpected_broker_position: QQQ", report.differences)
        self.assertIn("missing_broker_position: IWM", report.differences)

    def test_list_orders_normalizes_broker_order_snapshots(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaOrderManagementClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        orders = broker.list_orders(status="open")

        self.assertEqual(len(orders), 1)
        self.assertIsInstance(orders[0], PaperOrderSnapshot)
        self.assertEqual(orders[0].order_id, "broker-order-1")
        self.assertEqual(orders[0].client_order_id, "signal-spy-20240329")
        self.assertEqual(orders[0].symbol, "SPY")
        self.assertEqual(orders[0].status, "accepted")
        self.assertEqual(orders[0].notional, 1.0)
        self.assertEqual(orders[0].filled_quantity, 0.0)

    def test_get_order_supports_order_id_and_client_order_id(self) -> None:
        broker = AlpacaPaperBroker(
            client=FakeAlpacaOrderManagementClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        by_order_id = broker.get_order(order_id="broker-order-1")
        by_client_id = broker.get_order_by_client_id("signal-spy-20240329")

        self.assertEqual(by_order_id.order_id, "broker-order-1")
        self.assertEqual(by_client_id.client_order_id, "signal-spy-20240329")

    def test_cancel_order_by_client_order_id_resolves_broker_order_id(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        result = broker.cancel_order(client_order_id="signal-spy-20240329")

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(client.cancelled, ["broker-order-1"])

    def test_paper_cli_kill_switch_test_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "kill_switch.json"

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--universe",
                    "configs/universe.yml",
                    "--risk",
                    "configs/risk.yml",
                    "--kill-switch-test",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["kill_switch_active"])
        self.assertFalse(payload["order_result"]["accepted"])
        self.assertIn("kill_switch_active", payload["order_result"]["reasons"])
        self.assertTrue(payload["cancel_result"]["accepted"])

    def test_paper_cli_signal_order_dry_run_submits_one_dollar_notional_buy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(5.0,)),
                str(model_path),
            )
            write_records(
                [
                    {"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"},
                    {"timestamp": "2024-03-29", "symbol": "QQQ", "momentum_20": "-0.20"},
                ],
                features_path,
            )

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--signal-model",
                    str(model_path),
                    "--features",
                    str(features_path),
                    "--submit-signal-order",
                    "--as-of-date",
                    "2024-04-01",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["submitted"])
        self.assertTrue(payload["preflight"]["allowed"])
        self.assertEqual(payload["preflight"]["reasons"], [])
        self.assertEqual(payload["selected_signal"]["symbol"], "SPY")
        self.assertEqual(payload["selected_signal"]["action"], "buy")
        self.assertEqual(payload["order_intent"]["side"], "buy")
        self.assertEqual(payload["order_intent"]["notional"], 1.0)
        self.assertTrue(payload["order_result"]["accepted"])

    def test_paper_cli_signal_order_blocks_stale_features_without_submitting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(5.0,)),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--signal-model",
                    str(model_path),
                    "--features",
                    str(features_path),
                    "--submit-signal-order",
                    "--as-of-date",
                    "2026-06-16",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["submitted"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("stale_features", payload["preflight"]["reasons"])
        self.assertIsNotNone(payload["order_intent"])
        self.assertIsNone(payload["order_result"])

    def test_paper_cli_signal_order_blocks_open_order_for_symbol(self) -> None:
        client = FakeOpenOrderForSymbolClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(5.0,)),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--signal-model",
                        str(model_path),
                        "--features",
                        str(features_path),
                        "--submit-signal-order",
                        "--as-of-date",
                        "2024-04-01",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(client.submitted)
        self.assertFalse(payload["submitted"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("open_order_exists", payload["preflight"]["reasons"])
        self.assertEqual(payload["open_orders"][0]["client_order_id"], "other-open-order")
        self.assertIsNone(payload["order_result"])

    def test_paper_cli_signal_order_blocks_existing_position_for_symbol(self) -> None:
        client = FakePositionBlockingClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(5.0,)),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--signal-model",
                        str(model_path),
                        "--features",
                        str(features_path),
                        "--submit-signal-order",
                        "--as-of-date",
                        "2024-04-01",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(client.submitted)
        self.assertFalse(payload["submitted"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("position_exists", payload["preflight"]["reasons"])
        self.assertEqual(payload["positions"][0]["symbol"], "SPY")
        self.assertIsNone(payload["order_result"])

    def test_paper_cli_signal_order_dry_run_does_not_submit_when_signal_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(5.0,)),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-01-01", "symbol": "SPY", "momentum_20": "-0.20"}],
                features_path,
            )

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--signal-model",
                    str(model_path),
                    "--features",
                    str(features_path),
                    "--submit-signal-order",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["submitted"])
        self.assertIsNone(payload["selected_signal"])
        self.assertIsNone(payload["order_intent"])
        self.assertIsNone(payload["order_result"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertEqual(payload["preflight"]["reasons"], ["no_buy_signal"])

    def test_paper_cli_list_orders_dry_run_writes_empty_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "orders.json"

            exit_code = main(["paper", "--broker", "alpaca", "--dry-run", "--list-orders", "--output", str(output)])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["orders"], [])

    def test_paper_cli_get_order_by_client_order_id_writes_normalized_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "order.json"

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=FakeAlpacaOrderManagementClient()):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--get-order",
                        "--client-order-id",
                        "signal-spy-20240329",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order"]["order_id"], "broker-order-1")
        self.assertEqual(payload["order"]["client_order_id"], "signal-spy-20240329")
        self.assertEqual(payload["order"]["status"], "accepted")

    def test_paper_cli_cancel_order_requires_explicit_cancel_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cancel.json"

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--cancel-order",
                    "--client-order-id",
                    "signal-spy-20240329",
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertFalse(output.exists())

    def test_paper_cli_cancel_order_by_client_id_resolves_and_writes_result(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cancel.json"

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=client):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--cancel-order",
                        "--client-order-id",
                        "signal-spy-20240329",
                        "--confirm-cancel",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.cancelled, ["broker-order-1"])
        self.assertTrue(payload["cancel_result"]["accepted"])
        self.assertEqual(payload["resolved_order"]["order_id"], "broker-order-1")

    def test_paper_cli_reconcile_order_report_detects_accepted_order_without_position(self) -> None:
        source_report = {
            "order_intent": {
                "client_order_id": "signal-spy-20240329",
                "symbol": "SPY",
                "side": "buy",
                "notional": 1.0,
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "signal_order.json"
            output = root / "reconcile.json"
            source.write_text(json.dumps(source_report), encoding="utf-8")

            with mock.patch("trading_ai.cli.build_alpaca_paper_client", return_value=FakeAlpacaOrderManagementClient()):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--reconcile-order",
                        "--source-report",
                        str(source),
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("not_filled_yet", payload["reconciliation"]["differences"])
        self.assertEqual(payload["expected_order"]["client_order_id"], "signal-spy-20240329")


if __name__ == "__main__":
    unittest.main()
