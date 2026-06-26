import unittest

from trading_ai.execution.alpaca_paper import AlpacaPaperBroker
from trading_ai.execution.live_alpaca import AlpacaLiveBroker, LiveOrder
from trading_ai.risk.policy import RiskLimits


class FakeLiveClient:
    def __init__(self) -> None:
        self.submit_calls = 0

    def submit_order(self, *args: object, **kwargs: object) -> object:
        self.submit_calls += 1
        raise AssertionError("live submit should not be called before go-live")


class FakeSubmitClient:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit_order(self, order_request: object) -> object:
        self.submitted.append(order_request)
        return {"id": "live-order-1", "status": "accepted"}


class AlpacaLiveExecutionTests(unittest.TestCase):
    def test_live_broker_does_not_subclass_paper_broker(self) -> None:
        self.assertFalse(issubclass(AlpacaLiveBroker, AlpacaPaperBroker))

    def test_submit_order_is_blocked_by_default_without_calling_client(self) -> None:
        client = FakeLiveClient()
        broker = AlpacaLiveBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=False),
        )
        order = LiveOrder(symbol="SPY", side="buy", client_order_id="live-1", notional=1.0)

        result = broker.submit_order(order)

        self.assertFalse(result.accepted)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.status, "rejected")
        self.assertIn("live_submit_not_enabled", result.reasons)
        self.assertEqual(client.submit_calls, 0)

    def test_validate_order_uses_live_risk_semantics_without_submit(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeLiveClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=False, max_single_position=0.10),
        )
        order = LiveOrder(
            symbol="SPY",
            side="buy",
            client_order_id="live-2",
            notional=1.0,
            estimated_position_weight=0.20,
        )

        result = broker.validate_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("single_position_limit", result.reasons)
        self.assertIn("live_trading_not_allowed_by_risk_config", result.reasons)

    def test_non_allowlisted_symbol_is_rejected_before_any_live_submit_path(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeLiveClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=False),
        )
        order = LiveOrder(symbol="TSLA", side="buy", client_order_id="live-3", notional=1.0)

        result = broker.submit_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("symbol_not_allowlisted", result.reasons)
        self.assertIn("live_submit_not_enabled", result.reasons)

    def test_submit_enabled_uses_live_client_once_with_idempotent_notional_order(self) -> None:
        client = FakeSubmitClient()
        broker = AlpacaLiveBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
            submit_enabled=True,
            order_request_factory=lambda order: {
                "symbol": order.symbol,
                "side": order.side,
                "client_order_id": order.client_order_id,
                "notional": order.notional,
            },
        )
        order = LiveOrder(symbol="SPY", side="buy", client_order_id="live-canary-2026-06-16-spy", notional=1.0)

        result = broker.submit_order(order)

        self.assertTrue(result.accepted)
        self.assertFalse(result.dry_run)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(client.submitted, [{"symbol": "SPY", "side": "buy", "client_order_id": order.client_order_id, "notional": 1.0}])
        self.assertEqual(result.broker_response, {"id": "live-order-1", "status": "accepted"})


if __name__ == "__main__":
    unittest.main()
