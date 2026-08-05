import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from trading_ai.execution.account_supervisor import AccountLeaseBusyError
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder, _is_transient_error
from trading_ai.execution.order_journal import DurableOrderJournal
from trading_ai.risk.policy import RiskLimits


class _TransientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _NotFoundError(RuntimeError):
    status_code = 404


class FakeMarketDataClient:
    def __init__(self, *, price: float = 1.0) -> None:
        self.price = price

    def get_stock_latest_trade(self, request: object) -> dict[str, Any]:
        class Trade:
            price = self.price

        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: Trade()}


class SubmitClient:
    def __init__(
        self,
        *,
        submit_error: BaseException | None = None,
        existing_order: dict[str, Any] | None = None,
        accept_before_error: bool = False,
        lookup_error: BaseException | None = None,
    ) -> None:
        self.submit_calls = 0
        self.lookup_calls = 0
        self.submit_error = submit_error
        self.existing_order = existing_order
        self.accept_before_error = accept_before_error
        self.lookup_error = lookup_error

    def submit_order(self, **kwargs: object) -> dict[str, Any]:
        self.submit_calls += 1
        response = {"id": "broker-order", "status": "accepted", **kwargs}
        if self.submit_error is not None:
            if self.accept_before_error:
                self.existing_order = response
            raise self.submit_error
        self.existing_order = response
        return response

    def get_order_by_client_id(self, client_order_id: str) -> object:
        self.lookup_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        if self.existing_order is not None:
            return self.existing_order
        raise _NotFoundError("order not found")


def _broker(client: object, journal_path: Path | None) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        client=client,
        allowlist=("SPY",),
        risk_limits=RiskLimits(),
        dry_run=False,
        max_retries=2,
        retry_base_delay=0.0,
        today=lambda: date(2024, 4, 1),
        market_data=FakeMarketDataClient(price=1.0),
        order_journal_path=journal_path,
    )


def _order(
    *,
    client_order_id: str = "o-1",
    notional: float = 1.0,
    reference_price: float = 1.0,
) -> PaperOrder:
    return PaperOrder(
        symbol="SPY",
        side="buy",
        notional=notional,
        client_order_id=client_order_id,
        reference_price=reference_price,
        position_intent="open",
    )


class TransientClassificationTests(unittest.TestCase):
    def test_timeouts_and_status_codes_are_transient(self) -> None:
        self.assertTrue(_is_transient_error(TimeoutError("x")))
        self.assertTrue(_is_transient_error(ConnectionError("x")))
        self.assertTrue(_is_transient_error(_TransientError("boom", status_code=429)))
        self.assertTrue(_is_transient_error(_TransientError("boom", status_code=503)))
        self.assertTrue(_is_transient_error(RuntimeError("Rate limit exceeded")))

    def test_client_errors_are_not_transient(self) -> None:
        self.assertFalse(_is_transient_error(_TransientError("bad request", status_code=400)))
        self.assertFalse(_is_transient_error(ValueError("invalid symbol")))

    def test_market_data_preflight_does_not_multiply_transport_retries(self) -> None:
        class TimeoutMarketData:
            def __init__(self) -> None:
                self.calls = 0

            def get_stock_latest_trade(self, _request: object) -> object:
                self.calls += 1
                raise TimeoutError("inert quote timeout")

        market_data = TimeoutMarketData()
        broker = AlpacaPaperBroker(
            client=SubmitClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            max_retries=2,
            market_data=market_data,
        )

        self.assertIsNone(broker.latest_trade_price("SPY"))
        self.assertEqual(market_data.calls, 1)


class BrokerFirstIdempotencyTests(unittest.TestCase):
    def test_successful_submit_is_preceded_by_definitive_broker_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient()
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(client.lookup_calls, 1)
        self.assertEqual(client.submit_calls, 1)

    def test_ambiguous_submit_is_never_retried_when_lookup_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(submit_error=TimeoutError("request timed out"))
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "submit_unresolved")
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(client.lookup_calls, 2)

    def test_busy_error_after_dispatch_claim_remains_ambiguous_and_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            client = SubmitClient(submit_error=AccountLeaseBusyError("raised after dispatch"))
            broker = _broker(client, path)

            first = broker.submit_order(_order())
            second = broker.submit_order(_order())

        self.assertEqual(first.status, "submit_unresolved")
        self.assertEqual(second.status, "submit_unresolved")
        self.assertEqual(client.submit_calls, 1)

    def test_timeout_recovers_existing_order_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(
                submit_error=TimeoutError("request timed out"),
                accept_before_error=True,
            )
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "recovered_accepted")
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(client.lookup_calls, 2)

    def test_restart_adopts_broker_order_without_new_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            first_client = SubmitClient()
            first = _broker(first_client, path).submit_order(_order())
            recovery_client = SubmitClient(existing_order=first.broker_response)
            recovered = _broker(recovery_client, path).submit_order(_order())

        self.assertTrue(first.accepted)
        self.assertTrue(recovered.accepted)
        self.assertEqual(recovered.status, "recovered_accepted")
        self.assertEqual(recovery_client.submit_calls, 0)
        self.assertEqual(recovery_client.lookup_calls, 1)

    def test_same_client_id_with_different_intent_is_blocked_before_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            self.assertTrue(_broker(SubmitClient(), path).submit_order(_order(notional=1.0)).accepted)
            second_client = SubmitClient()
            result = _broker(second_client, path).submit_order(_order(notional=2.0))

        self.assertFalse(result.accepted)
        self.assertIn("client_order_id_intent_mismatch", result.reasons)
        self.assertEqual(second_client.lookup_calls, 0)
        self.assertEqual(second_client.submit_calls, 0)

    def test_same_client_id_with_different_reference_price_is_blocked_before_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.sqlite3"
            self.assertTrue(
                _broker(SubmitClient(), path).submit_order(_order(reference_price=1.0)).accepted
            )
            second_client = SubmitClient()
            result = _broker(second_client, path).submit_order(
                _order(reference_price=1.01)
            )

        self.assertFalse(result.accepted)
        self.assertIn("client_order_id_intent_mismatch", result.reasons)
        self.assertEqual(second_client.lookup_calls, 0)
        self.assertEqual(second_client.submit_calls, 0)

    def test_lookup_unavailable_before_submit_is_durably_deferred_without_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(lookup_error=TimeoutError("lookup unavailable"))
            path = Path(tmp) / "orders.sqlite3"
            result = _broker(client, path).submit_order(_order())
            record = DurableOrderJournal(path).get("o-1")
            event = DurableOrderJournal(path).events("o-1")[-1]

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "submit_deferred")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state.value, "intent_recorded")
        self.assertEqual(event.event_type, "submit_not_dispatched")
        self.assertEqual(client.submit_calls, 0)

    def test_explicit_422_submit_rejection_is_terminal_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(submit_error=_TransientError("invalid request", status_code=422))
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(client.submit_calls, 1)

    def test_local_parse_error_after_acceptance_is_recovered_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(
                submit_error=ValueError("response parse failed"),
                accept_before_error=True,
            )
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "recovered_accepted")
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(client.lookup_calls, 2)

    def test_broker_first_recovery_rejects_different_time_in_force(self) -> None:
        existing = {
            "id": "broker-order",
            "client_order_id": "o-1",
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "notional": 1.0,
            "qty": None,
            "status": "accepted",
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(existing_order=existing)
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertIn("broker_order_intent_mismatch", result.reasons)
        self.assertEqual(client.submit_calls, 0)

    def test_broker_first_recovery_rejects_different_limit_price(self) -> None:
        order = PaperOrder(
            symbol="SPY",
            side="buy",
            quantity=1,
            client_order_id="limit-1",
            reference_price=1.0,
            order_type="limit",
            limit_price=1.0,
            position_intent="open",
        )
        existing = {
            "id": "broker-order",
            "client_order_id": "limit-1",
            "symbol": "SPY",
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "notional": None,
            "qty": 1.0,
            "limit_price": 1.5,
            "status": "accepted",
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(existing_order=existing)
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("broker_order_intent_mismatch", result.reasons)
        self.assertEqual(client.submit_calls, 0)

    def test_unknown_status_after_submit_is_unresolved(self) -> None:
        class UnknownStatusClient(SubmitClient):
            def submit_order(self, **kwargs: object) -> dict[str, Any]:
                self.submit_calls += 1
                self.existing_order = {"id": "broker-order", "status": "future_state", **kwargs}
                return self.existing_order

        with tempfile.TemporaryDirectory() as tmp:
            client = UnknownStatusClient()
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "submit_unresolved")
        self.assertIn("broker_order_status_unknown", result.reasons)
        self.assertEqual(client.submit_calls, 1)

    def test_unknown_status_during_broker_first_recovery_never_posts(self) -> None:
        existing = {
            "id": "broker-order",
            "client_order_id": "o-1",
            "symbol": "SPY",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "notional": 1.0,
            "qty": None,
            "status": "future_state",
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = SubmitClient(existing_order=existing)
            result = _broker(client, Path(tmp) / "orders.sqlite3").submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "submit_unresolved")
        self.assertIn("broker_order_status_unknown", result.reasons)
        self.assertEqual(client.submit_calls, 0)

    def test_real_submit_requires_durable_journal(self) -> None:
        client = SubmitClient()
        result = _broker(client, None).submit_order(_order())

        self.assertFalse(result.accepted)
        self.assertIn("durable_order_journal_unavailable", result.reasons)
        self.assertEqual(client.lookup_calls, 0)
        self.assertEqual(client.submit_calls, 0)


if __name__ == "__main__":
    unittest.main()
