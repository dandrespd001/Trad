import unittest
from typing import Any

from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder, _is_transient_error
from trading_ai.risk.policy import RiskLimits


class _TransientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeMarketDataClient:
    """Returns a fixed latest-trade price, matching the order's reference price."""

    def __init__(self, *, price: float = 1.0) -> None:
        self.price = price

    def get_stock_latest_trade(self, request: object) -> dict[str, Any]:
        class Trade:
            price = self.price

        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]
        return {symbol: Trade()}


class FlakySubmitClient:
    """Fails the submit call a fixed number of times before succeeding."""

    def __init__(self, *, failures: int, existing_order: object | None = None) -> None:
        self.submit_calls = 0
        self.lookup_calls = 0
        self._failures = failures
        self._existing_order = existing_order

    def submit_order(self, **kwargs: object) -> dict[str, Any]:
        self.submit_calls += 1
        if self.submit_calls <= self._failures:
            raise TimeoutError("request timed out")
        return {"id": "broker-order", "status": "accepted", **kwargs}

    def get_order_by_client_id(self, client_order_id: str) -> object:
        self.lookup_calls += 1
        if self._existing_order is not None:
            return self._existing_order
        raise ValueError("order not found")


def _broker(client: object) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        client=client,
        allowlist=("SPY",),
        risk_limits=RiskLimits(),
        dry_run=False,
        max_retries=3,
        retry_base_delay=0.0,
        market_data=FakeMarketDataClient(price=1.0),
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


class RetryIdempotencyTests(unittest.TestCase):
    def test_submit_retries_transient_then_succeeds(self) -> None:
        client = FlakySubmitClient(failures=2)
        order = PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=1.0)
        result = _broker(client).submit_order(order)
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(client.submit_calls, 3)  # 2 failures + 1 success

    def test_submit_idempotency_avoids_duplicate_when_order_already_exists(self) -> None:
        existing = {"id": "broker-order", "client_order_id": "o-1", "status": "accepted"}
        client = FlakySubmitClient(failures=99, existing_order=existing)
        order = PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-1", reference_price=1.0)
        result = _broker(client).submit_order(order)
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, "submitted")
        # Submit attempted once; the idempotency lookup resolved the existing order.
        self.assertEqual(client.submit_calls, 1)
        self.assertEqual(client.lookup_calls, 1)
        self.assertEqual(result.broker_response, existing)

    def test_non_transient_error_propagates(self) -> None:
        class HardFailClient:
            def submit_order(self, **kwargs: object) -> dict[str, Any]:
                raise ValueError("invalid request")

            def get_order_by_client_id(self, client_order_id: str) -> object:
                raise ValueError("order not found")

        with self.assertRaises(ValueError):
            _broker(HardFailClient()).submit_order(
                PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-2", reference_price=1.0)
            )

    def test_retries_exhausted_raises(self) -> None:
        client = FlakySubmitClient(failures=99)
        broker = AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            max_retries=2,
            retry_base_delay=0.0,
            market_data=FakeMarketDataClient(price=1.0),
        )
        with self.assertRaises(TimeoutError):
            broker.submit_order(
                PaperOrder(symbol="SPY", side="buy", notional=1.0, client_order_id="o-3", reference_price=1.0)
            )
        self.assertEqual(client.submit_calls, 3)  # initial + 2 retries


if __name__ == "__main__":
    unittest.main()
