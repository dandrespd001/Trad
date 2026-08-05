import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.data.io import write_records
from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    IncompleteOrderSnapshotError,
    InvalidAccountSnapshotError,
    InvalidOrderSnapshotError,
    InvalidPositionSnapshotError,
    PaperAccount,
    PaperOrder,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
)
from trading_ai.execution.order_journal import (
    BrokerOrderIdCollisionError,
    DurableOrderJournal,
)
from trading_ai.execution.paper_executor_client import PaperExecutorMutationReceipt
from trading_ai.execution.paper_executor_ipc import (
    ExecutorTarget,
    PaperExecutorOutcomeUnknownError,
)
from trading_ai.models.baseline import LogisticBaselineModel, save_model
from trading_ai.risk.policy import RiskLimits


class _NotFoundError(RuntimeError):
    status_code = 404


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.orders: list[dict[str, Any]] = []

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

    def submit_order(self, **kwargs: object) -> dict[str, Any]:
        self.orders.append(kwargs)
        return {"id": "broker-order-1", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        for order in self.orders:
            if order.get("client_order_id") == client_order_id:
                return {"id": "broker-order-1", "status": "accepted", **order}
        raise _NotFoundError("order not found")

    def cancel_order_by_id(self, client_order_id: str) -> dict[str, Any]:
        self.cancelled.append(client_order_id)
        return {"cancelled": client_order_id}


class FakeMarketDataClient:
    """Returns a fixed latest-trade price, matching the order's reference price."""

    def __init__(self, *, price: float = 1.0) -> None:
        self.price = price
        self.requests: list[object] = []

    def get_stock_latest_trade(self, request: object) -> dict[str, Any]:
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

    def submit_order(self, order_data: object) -> dict[str, Any]:
        self.orders.append(order_data)
        if isinstance(order_data, dict):
            payload = dict(order_data)
        else:
            payload = {
                "symbol": getattr(order_data, "symbol", None),
                "side": getattr(getattr(order_data, "side", None), "value", getattr(order_data, "side", None)),
                "type": getattr(getattr(order_data, "type", None), "value", getattr(order_data, "type", None)),
                "time_in_force": getattr(
                    getattr(order_data, "time_in_force", None),
                    "value",
                    getattr(order_data, "time_in_force", None),
                ),
                "client_order_id": getattr(order_data, "client_order_id", None),
                "qty": getattr(order_data, "qty", None),
                "notional": getattr(order_data, "notional", None),
            }
        return {"id": "broker-order-1", "status": "accepted", **payload}

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        raise _NotFoundError(f"order not found: {client_order_id}")


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

    def get_orders(self, filter: object | None = None) -> list[dict[str, Any]]:
        self.filters.append(filter)
        return [self.order]

    def get_order_by_id(self, order_id: str, filter: object | None = None) -> dict[str, Any]:
        self.filters.append(filter)
        if order_id != self.order["id"]:
            raise ValueError("order not found")
        return self.order

    def get_order_by_client_id(self, client_id: str) -> dict[str, Any]:
        if client_id != self.order["client_order_id"]:
            raise ValueError("order not found")
        return self.order

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.order["status"] = "pending_cancel"


def _paper_order_snapshot(*, client_order_id: str = "signal-spy-20240329") -> PaperOrderSnapshot:
    return PaperOrderSnapshot(
        order_id="broker-order-1",
        client_order_id=client_order_id,
        symbol="SPY",
        side="buy",
        order_type="market",
        time_in_force="day",
        status="accepted",
        notional=1.0,
        quantity=None,
        filled_quantity=0.0,
        filled_avg_price=None,
        submitted_at="2026-06-16T22:07:42.667183Z",
        created_at="2026-06-16T22:07:42.667183Z",
        updated_at="2026-06-16T22:07:42.668584Z",
        expires_at="2026-06-17T20:00:00Z",
    )


def _paper_executor_target() -> ExecutorTarget:
    return ExecutorTarget(
        account_scope_sha256="a" * 64,
        policy_sha256="b" * 64,
        authz_policy_sha256="c" * 64,
        run_id="1" * 32,
        fence_epoch=1,
    )


def _paper_executor_target_payload() -> dict[str, object]:
    target = _paper_executor_target()
    return {
        "account_scope_sha256": target.account_scope_sha256,
        "policy_sha256": target.policy_sha256,
        "authz_policy_sha256": target.authz_policy_sha256,
        "run_id": target.run_id,
        "fence_epoch": target.fence_epoch,
    }


class FakePaperExecutorBrokerClient:
    """High-level executor facade; deliberately exposes no Alpaca SDK methods."""

    def __init__(
        self,
        *,
        open_orders: tuple[PaperOrderSnapshot, ...] = (),
        positions: tuple[PaperPosition, ...] = (),
        opening_orders_allowed: bool = True,
    ) -> None:
        self.open_orders = open_orders
        self.positions = positions
        self.opening_orders_allowed = opening_orders_allowed
        self.order = _paper_order_snapshot()
        self.submitted_orders: list[PaperOrder] = []
        self.cancelled: list[tuple[str | None, str | None]] = []
        self.listed_statuses: list[str] = []
        self.health_calls = 0

    def health(self) -> dict[str, object]:
        self.health_calls += 1
        return {
            "status": "ready",
            "mutations_allowed": True,
            "opening_orders_allowed": self.opening_orders_allowed,
            "capability_mode": "full" if self.opening_orders_allowed else "reduce_only",
            "account_scope_sha256": "a" * 64,
            "fence_epoch": 1,
            "policy_sha256": "b" * 64,
            "authz_policy_sha256": "c" * 64,
            "run_id": "1" * 32,
            "pending_recovery": 0,
            "kill_switch_active": False,
        }

    def read_account(self) -> PaperAccount:
        return PaperAccount(
            account_id="paper-account",
            status="ACTIVE",
            cash=10_000.0,
            equity=10_000.0,
            buying_power=9_999.0,
            last_equity=10_000.0,
        )

    def read_positions(self) -> tuple[PaperPosition, ...]:
        return self.positions

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        if status not in {"open", "closed", "all"}:
            raise AssertionError(f"unexpected order status: {status}")
        self.listed_statuses.append(status)
        return self.open_orders

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        if order_id != self.order.order_id:
            raise AssertionError(f"unexpected order id: {order_id}")
        return self.order

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        if client_order_id != self.order.client_order_id:
            raise AssertionError(f"unexpected client order id: {client_order_id}")
        return self.order

    def cancel_order(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
    ) -> PaperOrderResult:
        self.cancelled.append((client_order_id, order_id))
        return PaperOrderResult(
            accepted=True,
            status="cancel_requested",
            reasons=(),
            dry_run=False,
        )

    def submit_order(self, order: PaperOrder) -> PaperOrderResult:
        self.submitted_orders.append(order)
        return PaperOrderResult(
            accepted=True,
            status="submitted",
            reasons=(),
            dry_run=False,
        )

    def submit_order_with_receipt(
        self,
        order: PaperOrder,
    ) -> PaperExecutorMutationReceipt:
        result = self.submit_order(order)
        return PaperExecutorMutationReceipt(
            request_id="d" * 32,
            operation="submit_order",
            target=_paper_executor_target(),
            outcome="completed" if result.accepted else "rejected",
            result=result,
        )

    def cancel_order_with_receipt(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
    ) -> PaperExecutorMutationReceipt:
        result = self.cancel_order(client_order_id, order_id=order_id)
        return PaperExecutorMutationReceipt(
            request_id="e" * 32,
            operation="cancel_order",
            target=_paper_executor_target(),
            outcome="completed" if result.accepted else "rejected",
            result=result,
        )


class AlpacaPaperExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.order_journal_path = Path(self._temp_dir.name) / "orders.sqlite3"

    def test_paper_cli_default_output_uses_tmp_latest_report(self) -> None:
        args = build_parser().parse_args(["paper", "--broker", "alpaca", "--dry-run", "--list-orders"])

        self.assertEqual(args.output, "reports/tmp/paper/latest.json")

    def test_paper_cli_rejects_conflicting_dry_run_and_real_paper_modes(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["paper", "--broker", "alpaca", "--dry-run", "--real-paper"])

    def test_paper_cli_rejects_unknown_order_status_at_parse_boundary(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["paper", "--broker", "alpaca", "--list-orders", "--order-status", "pending"]
            )

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

    def test_read_account_rejects_non_finite_risk_fields(self) -> None:
        class NonFiniteAccountClient:
            def __init__(self, invalid_field: str) -> None:
                self.invalid_field = invalid_field

            def get_account(self) -> dict[str, str]:
                account = {
                    "id": "paper-account",
                    "status": "ACTIVE",
                    "cash": "10000.00",
                    "equity": "10500.00",
                    "buying_power": "20000.00",
                }
                account[self.invalid_field] = "NaN"
                return account

        for field in ("cash", "equity", "buying_power"):
            with self.subTest(field=field):
                broker = AlpacaPaperBroker(
                    client=NonFiniteAccountClient(field),
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )

                with self.assertRaisesRegex(InvalidAccountSnapshotError, field):
                    broker.read_account()

    def test_read_account_rejects_none_or_boolean_identity_fields(self) -> None:
        for field, value in (("id", None), ("id", True), ("status", None), ("status", False)):
            with self.subTest(field=field, value=value):
                account = {
                    "id": "paper-account",
                    "status": "ACTIVE",
                    "cash": "10000.00",
                    "equity": "10500.00",
                    "buying_power": "20000.00",
                }
                account[field] = value
                client = mock.Mock()
                client.get_account.return_value = account
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )

                with self.assertRaises(InvalidAccountSnapshotError):
                    broker.read_account()

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

    def test_read_positions_rejects_missing_or_non_finite_quantity(self) -> None:
        class InvalidPositionClient:
            def __init__(self, position: dict[str, object]) -> None:
                self.position = position

            def list_positions(self) -> list[dict[str, object]]:
                return [self.position]

        invalid_positions = (
            {"symbol": "SPY", "market_value": "100.00"},
            {"symbol": "SPY", "qty": "NaN", "market_value": "100.00"},
        )
        for position in invalid_positions:
            with self.subTest(position=position):
                broker = AlpacaPaperBroker(
                    client=InvalidPositionClient(position),
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )

                with self.assertRaisesRegex(InvalidPositionSnapshotError, "quantity"):
                    broker.read_positions()

    def test_read_positions_rejects_market_value_with_inconsistent_sign(self) -> None:
        class InconsistentPositionClient:
            def list_positions(self) -> list[dict[str, str]]:
                return [{"symbol": "SPY", "qty": "-2", "market_value": "200.00"}]

        broker = AlpacaPaperBroker(
            client=InconsistentPositionClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        with self.assertRaisesRegex(InvalidPositionSnapshotError, "inconsistent"):
            broker.read_positions()

    def test_read_positions_retains_non_allowlisted_positions_for_account_reconciliation(self) -> None:
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

        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0].symbol, "SPY")
        self.assertEqual(positions[1].symbol, "TSLA")

    def test_cancel_order_is_idempotent_and_only_calls_broker_once(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        with tempfile.TemporaryDirectory() as tmp:
            broker = AlpacaPaperBroker(
                client=client,
                allowlist=("SPY",),
                risk_limits=RiskLimits(),
                dry_run=False,
                order_journal_path=Path(tmp) / "orders.sqlite3",
            )

            first = broker.cancel_order(client_order_id="signal-spy-20240329")
            second = broker.cancel_order(client_order_id="signal-spy-20240329")
            event_types = [
                event.event_type
                for event in DurableOrderJournal(
                    Path(tmp) / "orders.sqlite3"
                ).events("signal-spy-20240329")
            ]

        self.assertTrue(first.accepted)
        self.assertEqual(first.status, "cancel_requested")
        self.assertFalse(second.accepted)
        self.assertEqual(second.status, "cancel_pending")
        self.assertEqual(client.cancelled, ["broker-order-1"])
        self.assertEqual(
            event_types[-2:],
            ["cancel_dispatch_attempted", "cancel_request_accepted"],
        )

    def test_ambiguous_cancel_is_not_retried_when_order_remains_partially_filled(self) -> None:
        class AmbiguousCancelClient(FakeAlpacaOrderManagementClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancel_attempts = 0

            def cancel_order_by_id(self, order_id: str) -> None:
                del order_id
                self.cancel_attempts += 1
                raise TimeoutError("cancel response timed out")

        client = AmbiguousCancelClient()
        with tempfile.TemporaryDirectory() as tmp:
            broker = AlpacaPaperBroker(
                client=client,
                allowlist=("SPY",),
                risk_limits=RiskLimits(),
                dry_run=False,
                order_journal_path=Path(tmp) / "orders.sqlite3",
            )
            first = broker.cancel_order(client_order_id="signal-spy-20240329")
            client.order["status"] = "partially_filled"
            client.order["filled_qty"] = "0.5"
            second = broker.cancel_order(client_order_id="signal-spy-20240329")

        self.assertFalse(first.accepted)
        self.assertEqual(first.status, "cancel_unresolved")
        self.assertFalse(second.accepted)
        self.assertEqual(second.status, "cancel_unresolved")
        self.assertEqual(client.cancel_attempts, 1)

    def test_flat_account_reconciles_terminal_order_journal_atomically(self) -> None:
        class FlatClient(FakeAlpacaOrderManagementClient):
            def get_orders(self, filter: object | None = None) -> list[dict[str, Any]]:
                self.filters.append(filter)
                return []

        client = FlatClient()
        client.order["status"] = "filled"
        client.order["filled_qty"] = "1"
        journal = DurableOrderJournal(self.order_journal_path)
        journal.record_intent(
            "signal-spy-20240329",
            {
                "symbol": "SPY",
                "side": "buy",
                "quantity": None,
                "notional": 1.0,
                "order_type": "market",
                "time_in_force": "day",
                "limit_price": None,
                "position_intent": "open",
                "reference_price": None,
            },
        )
        journal.transition(
            "signal-spy-20240329",
            "filled",
            broker_order_id="broker-order-1",
        )
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            order_journal_path=self.order_journal_path,
        )

        attestation = broker.reconcile_flat_account_order_journal()

        self.assertIsNotNone(attestation)
        assert attestation is not None
        self.assertEqual(attestation.record_count, 1)
        self.assertEqual(
            DurableOrderJournal(self.order_journal_path)
            .get("signal-spy-20240329")
            .state.value,
            "reconciled",
        )

    def test_flat_account_does_not_reconcile_nonterminal_lookup(self) -> None:
        class FlatListClient(FakeAlpacaOrderManagementClient):
            def get_orders(self, filter: object | None = None) -> list[dict[str, Any]]:
                self.filters.append(filter)
                return []

        client = FlatListClient()
        journal = DurableOrderJournal(self.order_journal_path)
        journal.record_intent(
            "signal-spy-20240329",
            {
                "symbol": "SPY",
                "side": "buy",
                "quantity": None,
                "notional": 1.0,
                "order_type": "market",
                "time_in_force": "day",
                "limit_price": None,
                "position_intent": "open",
                "reference_price": None,
            },
        )
        journal.transition(
            "signal-spy-20240329",
            "acknowledged",
            broker_order_id="broker-order-1",
        )
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            order_journal_path=self.order_journal_path,
        )

        self.assertIsNone(broker.reconcile_flat_account_order_journal())
        self.assertEqual(
            DurableOrderJournal(self.order_journal_path)
            .get("signal-spy-20240329")
            .state.value,
            "acknowledged",
        )

    def test_kill_switch_rejects_new_orders_but_still_allows_cancellation(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        with tempfile.TemporaryDirectory() as tmp:
            broker = AlpacaPaperBroker(
                client=client,
                allowlist=("SPY",),
                risk_limits=RiskLimits(),
                dry_run=False,
                order_journal_path=Path(tmp) / "orders.sqlite3",
            )
            broker.activate_kill_switch("manual_test")

            order_result = broker.submit_order(
                PaperOrder(symbol="SPY", side="buy", quantity=1, client_order_id="o-1")
            )
            cancel_result = broker.cancel_order(client_order_id="signal-spy-20240329")

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

    def test_buy_order_is_rejected_on_observed_independence_day_2026(self) -> None:
        broker = AlpacaPaperBroker(
            client=None,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=True,
            today=lambda: date(2026, 7, 3),  # Friday observed Independence Day closure
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
            order_journal_path=self.order_journal_path,
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

    def test_real_buy_order_rejects_nonfinite_price_sanity_inputs_without_submit(self) -> None:
        nonfinite_values = (float("nan"), float("inf"), float("-inf"))
        for value in nonfinite_values:
            with self.subTest(source="reference", value=value):
                client = FakeAlpacaClient()
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    today=lambda: date(2024, 4, 1),
                    market_data=FakeMarketDataClient(price=100.0),
                )

                result = broker.submit_order(
                    PaperOrder(
                        symbol="SPY",
                        side="buy",
                        notional=1.0,
                        client_order_id="o-reference",
                        reference_price=value,
                    )
                )

                self.assertFalse(result.accepted)
                self.assertEqual(result.reasons, ("price_sanity_reference_missing",))
                self.assertEqual(client.orders, [])

            with self.subTest(source="broker", value=value):
                client = FakeAlpacaClient()
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    today=lambda: date(2024, 4, 1),
                    market_data=FakeMarketDataClient(price=value),
                )

                result = broker.submit_order(
                    PaperOrder(
                        symbol="SPY",
                        side="buy",
                        notional=1.0,
                        client_order_id="o-broker",
                        reference_price=100.0,
                    )
                )

                self.assertFalse(result.accepted)
                self.assertEqual(result.reasons, ("price_sanity_unavailable",))
                self.assertIsNone(broker.latest_trade_price("SPY"))
                self.assertEqual(client.orders, [])

    def test_real_buy_order_rejects_nonfinite_price_sanity_policy_without_submit(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                client = FakeAlpacaClient()
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(max_price_deviation_pct=value),
                    dry_run=False,
                    today=lambda: date(2024, 4, 1),
                    market_data=FakeMarketDataClient(price=100.0),
                )

                result = broker.submit_order(
                    PaperOrder(
                        symbol="SPY",
                        side="buy",
                        notional=1.0,
                        client_order_id="o-policy",
                        reference_price=100.0,
                    )
                )

                self.assertFalse(result.accepted)
                self.assertEqual(result.reasons, ("price_sanity_policy_invalid",))
                self.assertEqual(client.orders, [])

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
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
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
            today=lambda: date(2024, 4, 1),  # Monday, regular trading day
            market_data=FakeMarketDataClient(price=1.0),
            order_journal_path=self.order_journal_path,
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

    def test_list_orders_rejects_non_finite_quantities(self) -> None:
        for field in ("qty", "filled_qty"):
            with self.subTest(field=field):
                client = FakeAlpacaOrderManagementClient()
                client.order["qty"] = "1"
                client.order[field] = "NaN"
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )

                with self.assertRaisesRegex(InvalidOrderSnapshotError, "quantity"):
                    broker.list_orders(status="open")

    def test_list_orders_rejects_filled_quantity_above_order_quantity(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        client.order["qty"] = "1"
        client.order["filled_qty"] = "1.01"
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        with self.assertRaisesRegex(InvalidOrderSnapshotError, "exceeds"):
            broker.list_orders(status="open")

    def test_order_snapshot_preserves_signed_finite_realized_pnl(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        client.order["realized_pnl"] = "-1.25"
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        self.assertEqual(broker.list_orders(status="all")[0].realized_pnl, -1.25)
        client.order["realized_pnl"] = "nan"
        with self.assertRaisesRegex(InvalidOrderSnapshotError, "not finite"):
            broker.list_orders(status="all")

    def test_list_orders_blocks_replaced_status_without_validated_replacement_chain(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        client.order["status"] = "replaced"
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        with self.assertRaisesRegex(InvalidOrderSnapshotError, "status is unsupported"):
            broker.list_orders(status="all")

    def test_list_orders_requests_official_maximum_limit(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        broker.list_orders(status="open")

        self.assertEqual(client.filters[0].limit, 500)

    def test_list_orders_rejects_response_at_api_cap_as_potentially_incomplete(self) -> None:
        class CappedOrderClient(FakeAlpacaOrderManagementClient):
            def get_orders(self, filter: object | None = None) -> list[dict[str, Any]]:
                self.filters.append(filter)
                return [self.order] * 500

        broker = AlpacaPaperBroker(
            client=CappedOrderClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
        )

        with self.assertRaisesRegex(
            IncompleteOrderSnapshotError,
            "snapshot completeness is unknown",
        ):
            broker.list_orders(status="open")

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

    def test_journal_sync_rejects_changed_broker_id_even_when_state_is_unchanged(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            order_journal_path=self.order_journal_path,
        )
        first = broker.cancel_order(client_order_id="signal-spy-20240329")
        self.assertTrue(first.accepted)
        self.assertEqual(client.order["status"], "pending_cancel")
        client.order["id"] = "different-broker-order"

        with self.assertRaises(BrokerOrderIdCollisionError):
            broker.get_order_by_client_id("signal-spy-20240329")

    def test_cancel_order_by_client_order_id_resolves_broker_order_id(self) -> None:
        client = FakeAlpacaOrderManagementClient()
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            order_journal_path=self.order_journal_path,
        )

        result = broker.cancel_order(client_order_id="signal-spy-20240329")

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "cancel_requested")
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
        client = FakePaperExecutorBrokerClient(
            open_orders=(_paper_order_snapshot(client_order_id="other-open-order"),)
        )
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

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.submitted_orders, [])
        self.assertFalse(payload["submitted"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("open_order_exists", payload["preflight"]["reasons"])
        self.assertEqual(payload["open_orders"][0]["client_order_id"], "other-open-order")
        self.assertIsNone(payload["order_result"])

    def test_paper_cli_signal_order_blocks_existing_position_for_symbol(self) -> None:
        client = FakePaperExecutorBrokerClient(
            positions=(PaperPosition(symbol="SPY", quantity=1.0, market_value=500.0),)
        )
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

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.submitted_orders, [])
        self.assertFalse(payload["submitted"])
        self.assertFalse(payload["preflight"]["allowed"])
        self.assertIn("position_exists", payload["preflight"]["reasons"])
        self.assertEqual(payload["positions"][0]["symbol"], "SPY")
        self.assertIsNone(payload["order_result"])

    def test_paper_cli_signal_order_uses_executor_client_for_real_submit(self) -> None:
        client = FakePaperExecutorBrokerClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(
                    feature_names=("momentum_20",),
                    intercept=0.0,
                    coefficients=(5.0,),
                ),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["preflight"]["allowed"])
        self.assertTrue(payload["submitted"])
        self.assertEqual(len(client.submitted_orders), 1)
        self.assertEqual(client.submitted_orders[0].position_intent, "open")
        self.assertEqual(
            payload["executor_receipt"],
            {
                "request_id": "d" * 32,
                "operation": "submit_order",
                "outcome": "completed",
                **_paper_executor_target_payload(),
            },
        )

    def test_paper_cli_signal_order_blocks_when_executor_cannot_open(self) -> None:
        client = FakePaperExecutorBrokerClient(opening_orders_allowed=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            save_model(
                LogisticBaselineModel(
                    feature_names=("momentum_20",),
                    intercept=0.0,
                    coefficients=(5.0,),
                ),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertTrue(payload["preflight"]["allowed"])
        self.assertFalse(payload["submitted"])
        self.assertEqual(payload["order_result"]["reasons"], ["opening_orders_disabled"])
        self.assertEqual(client.submitted_orders, [])

    def test_paper_cli_signal_submit_outcome_unknown_is_not_retried(self) -> None:
        request_id = "a" * 32

        class OutcomeUnknownSubmitClient(FakePaperExecutorBrokerClient):
            def __init__(self) -> None:
                super().__init__()
                self.call_order: list[str] = []
                self.submit_attempts = 0

            def read_account(self) -> PaperAccount:
                self.call_order.append("read_account")
                return super().read_account()

            def submit_order(self, order: PaperOrder) -> PaperOrderResult:
                del order
                self.call_order.append("submit_order")
                self.submit_attempts += 1
                raise PaperExecutorOutcomeUnknownError(
                    "executor response timed out",
                    request_id=request_id,
                    operation="submit_order",
                    phase="receive",
                    target=_paper_executor_target(),
                )

            def submit_order_with_receipt(
                self,
                order: PaperOrder,
            ) -> PaperExecutorMutationReceipt:
                self.submit_order(order)
                raise AssertionError("outcome-unknown submit unexpectedly returned")

        client = OutcomeUnknownSubmitClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "model.json"
            features_path = root / "features.csv"
            output = root / "signal_order.json"
            ledger = root / "paper_ledger.jsonl"
            stderr = io.StringIO()
            save_model(
                LogisticBaselineModel(
                    feature_names=("momentum_20",),
                    intercept=0.0,
                    coefficients=(5.0,),
                ),
                str(model_path),
            )
            write_records(
                [{"timestamp": "2024-03-29", "symbol": "SPY", "momentum_20": "0.20"}],
                features_path,
            )

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
                contextlib.redirect_stderr(stderr),
            ):
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
                        "--ledger-output",
                        str(ledger),
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))
            event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(exit_code, 2)
        self.assertEqual(client.submit_attempts, 1)
        self.assertEqual(client.call_order, ["read_account", "submit_order"])
        self.assertFalse(payload["submitted"])
        self.assertIsNone(payload["order_result"])
        self.assertEqual(
            payload["outcome_unknown"],
            {
                "request_id": request_id,
                "operation": "submit_order",
                "phase": "receive",
                "retry_allowed": False,
                **_paper_executor_target_payload(),
            },
        )
        self.assertEqual(event["event_type"], "paper_signal_order")
        self.assertEqual(event["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(event["exit_code"], 2)
        self.assertEqual(event["reasons"], ["command_outcome_unknown"])
        self.assertIn("reconcile before retry", stderr.getvalue())

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

    def test_paper_cli_list_orders_real_uses_executor_client_only(self) -> None:
        client = FakePaperExecutorBrokerClient(open_orders=(_paper_order_snapshot(),))
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "orders.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
                mock.patch(
                    "trading_ai.cli.AlpacaPaperBroker",
                    side_effect=AssertionError("real-paper must not construct a caller-owned broker"),
                ) as direct_broker_constructor,
            ):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--list-orders",
                        "--order-status",
                        "all",
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            direct_broker_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.listed_statuses, ["all"])
        self.assertEqual(payload["order_status"], "all")
        self.assertEqual(payload["orders"][0]["order_id"], "broker-order-1")

    def test_paper_cli_real_read_account_and_positions_uses_executor_client_only(self) -> None:
        client = FakePaperExecutorBrokerClient(
            positions=(PaperPosition(symbol="SPY", quantity=2.0, market_value=1_000.0),)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "status.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_market_data_client",
                    side_effect=AssertionError("caller must not build paper market data"),
                ) as market_data_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_crypto_market_data_client",
                    side_effect=AssertionError("caller must not build crypto market data"),
                ) as crypto_market_data_constructor,
            ):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--read-account",
                        "--read-positions",
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            market_data_constructor.assert_not_called()
            crypto_market_data_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["account"]["account_id"], "paper-account")
        self.assertEqual(payload["positions"][0]["symbol"], "SPY")

    def test_paper_cli_real_noop_reports_executor_health(self) -> None:
        client = FakePaperExecutorBrokerClient()
        with (
            mock.patch(
                "trading_ai.cli.PaperExecutorBrokerClient",
                return_value=client,
            ) as executor_constructor,
            mock.patch(
                "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                side_effect=AssertionError("direct broker client must not be built"),
            ) as direct_constructor,
        ):
            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--real-paper",
                    "--confirm-paper",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.health_calls, 1)
        executor_constructor.assert_called_once_with()
        direct_constructor.assert_not_called()

    def test_paper_cli_get_order_by_client_order_id_writes_normalized_order(self) -> None:
        client = FakePaperExecutorBrokerClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "order.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order"]["order_id"], "broker-order-1")
        self.assertEqual(payload["order"]["client_order_id"], "signal-spy-20240329")
        self.assertEqual(payload["order"]["status"], "accepted")

    def test_paper_cli_get_order_by_order_id_uses_executor_client_only(self) -> None:
        client = FakePaperExecutorBrokerClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "order.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--get-order",
                        "--order-id",
                        "broker-order-1",
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order"]["order_id"], "broker-order-1")

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
        client = FakePaperExecutorBrokerClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cancel.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.cancelled, [("signal-spy-20240329", None)])
        self.assertTrue(payload["cancel_result"]["accepted"])
        self.assertEqual(payload["resolved_order"]["order_id"], "broker-order-1")
        self.assertEqual(
            payload["executor_receipt"],
            {
                "request_id": "e" * 32,
                "operation": "cancel_order",
                "outcome": "completed",
                **_paper_executor_target_payload(),
            },
        )

    def test_paper_cli_cancel_order_by_order_id_uses_executor_client_only(self) -> None:
        client = FakePaperExecutorBrokerClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cancel.json"

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--cancel-order",
                        "--order-id",
                        "broker-order-1",
                        "--confirm-cancel",
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(client.cancelled, [(None, "broker-order-1")])
        self.assertTrue(payload["cancel_result"]["accepted"])
        self.assertEqual(payload["resolved_order"]["order_id"], "broker-order-1")
        self.assertEqual(
            payload["executor_receipt"],
            {
                "request_id": "e" * 32,
                "operation": "cancel_order",
                "outcome": "completed",
                **_paper_executor_target_payload(),
            },
        )

    def test_paper_cli_cancel_outcome_unknown_is_not_retried(self) -> None:
        request_id = "b" * 32

        class OutcomeUnknownCancelClient(FakePaperExecutorBrokerClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancel_attempts = 0

            def cancel_order(
                self,
                client_order_id: str | None = None,
                *,
                order_id: str | None = None,
            ) -> PaperOrderResult:
                del client_order_id, order_id
                self.cancel_attempts += 1
                raise PaperExecutorOutcomeUnknownError(
                    "executor response timed out",
                    request_id=request_id,
                    operation="cancel_order",
                    phase="receive",
                    target=_paper_executor_target(),
                )

            def cancel_order_with_receipt(
                self,
                client_order_id: str | None = None,
                *,
                order_id: str | None = None,
            ) -> PaperExecutorMutationReceipt:
                self.cancel_order(
                    client_order_id,
                    order_id=order_id,
                )
                raise AssertionError("outcome-unknown cancel unexpectedly returned")

        client = OutcomeUnknownCancelClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "cancel.json"
            ledger = root / "paper_ledger.jsonl"
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
                contextlib.redirect_stderr(stderr),
            ):
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
                        "--ledger-output",
                        str(ledger),
                        "--output",
                        str(output),
                    ]
                )
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))
            event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(exit_code, 2)
        self.assertEqual(client.cancel_attempts, 1)
        self.assertIsNone(payload["cancel_result"])
        self.assertEqual(payload["resolved_order"]["order_id"], "broker-order-1")
        self.assertEqual(
            payload["outcome_unknown"],
            {
                "request_id": request_id,
                "operation": "cancel_order",
                "phase": "receive",
                "retry_allowed": False,
                **_paper_executor_target_payload(),
            },
        )
        self.assertEqual(event["event_type"], "paper_cancel_order")
        self.assertEqual(event["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(event["exit_code"], 2)
        self.assertEqual(event["reasons"], ["command_outcome_unknown"])
        self.assertIn("reconcile before retry", stderr.getvalue())

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

            client = FakePaperExecutorBrokerClient()
            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    return_value=client,
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
            ):
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
            executor_constructor.assert_called_once_with()
            direct_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("not_filled_yet", payload["reconciliation"]["differences"])
        self.assertEqual(payload["expected_order"]["client_order_id"], "signal-spy-20240329")


if __name__ == "__main__":
    unittest.main()
