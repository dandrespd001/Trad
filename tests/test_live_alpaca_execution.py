import unittest

from trading_ai.execution.alpaca_paper import AlpacaPaperBroker
from trading_ai.execution.live_alpaca import AlpacaLiveBroker, LiveOrder, _build_market_order_request
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
            projected_gross_exposure=0.50,
            daily_pnl_pct=0.0,
            current_drawdown_pct=0.0,
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

    def test_buy_order_requires_reference_and_live_price(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeLiveClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
        )

        missing_reference = broker.validate_order(
            LiveOrder(symbol="SPY", side="buy", client_order_id="live-4", notional=1.0, live_price=100.0)
        )
        missing_live = broker.validate_order(
            LiveOrder(symbol="SPY", side="buy", client_order_id="live-5", notional=1.0, reference_price=100.0)
        )

        self.assertFalse(missing_reference.accepted)
        self.assertIn("missing_reference_price", missing_reference.reasons)
        self.assertFalse(missing_live.accepted)
        self.assertIn("missing_live_price", missing_live.reasons)

    def test_buy_order_rejects_price_deviation_above_limit(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeLiveClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
        )
        order = LiveOrder(
            symbol="SPY",
            side="buy",
            client_order_id="live-6",
            notional=1.0,
            reference_price=100.0,
            live_price=106.0,
            max_price_deviation_pct=0.05,
        )

        result = broker.validate_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("price_sanity_failed", result.reasons)

    def test_validate_order_rejects_both_notional_and_quantity(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeLiveClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
        )
        order = LiveOrder(
            symbol="SPY",
            side="buy",
            client_order_id="live-7",
            notional=1.0,
            quantity=0.01,
            reference_price=100.0,
            live_price=100.0,
        )

        result = broker.validate_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("both_notional_and_quantity_set", result.reasons)

    def test_market_order_request_rejects_ambiguous_sizing(self) -> None:
        order = LiveOrder(symbol="SPY", side="sell", client_order_id="live-8", notional=1.0, quantity=0.01)

        with self.assertRaisesRegex(ValueError, "both_notional_and_quantity_set"):
            _build_market_order_request(order)

    def test_submit_enabled_remains_disabled_pending_p0_controls(self) -> None:
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
        order = LiveOrder(
            symbol="SPY",
            side="buy",
            client_order_id="live-canary-2026-06-16-spy",
            notional=1.0,
            reference_price=100.0,
            live_price=100.01,
            estimated_position_weight=0.01,
            projected_gross_exposure=0.01,
            daily_pnl_pct=0.0,
            current_drawdown_pct=0.0,
        )

        result = broker.submit_order(order)

        self.assertFalse(result.accepted)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.status, "rejected")
        self.assertIn("live_submit_disabled_pending_p0_controls", result.reasons)
        self.assertIn("live_risk_context_unverified", result.reasons)
        self.assertEqual(client.submitted, [])
        self.assertIsNone(result.broker_response)

    def test_nonfinite_or_boolean_order_fields_fail_closed(self) -> None:
        broker = AlpacaLiveBroker(
            client=FakeSubmitClient(),
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
            submit_enabled=True,
        )
        base: dict[str, object] = {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "live-adversarial",
            "notional": 1.0,
            "reference_price": 100.0,
            "live_price": 100.0,
            "max_price_deviation_pct": 0.05,
            "estimated_position_weight": 0.01,
            "projected_gross_exposure": 0.01,
            "daily_pnl_pct": 0.0,
            "current_drawdown_pct": 0.0,
        }
        cases = (
            ("notional", float("nan"), "invalid_notional"),
            ("quantity", float("inf"), "both_notional_and_quantity_set"),
            ("reference_price", float("nan"), "invalid_reference_price"),
            ("live_price", float("inf"), "invalid_live_price"),
            ("max_price_deviation_pct", float("nan"), "invalid_max_price_deviation_pct"),
            ("estimated_position_weight", float("nan"), "live_risk_context_invalid"),
            ("projected_gross_exposure", float("inf"), "live_risk_context_invalid"),
            ("daily_pnl_pct", True, "live_risk_context_invalid"),
            ("current_drawdown_pct", float("nan"), "live_risk_context_invalid"),
        )
        for field, value, expected_reason in cases:
            with self.subTest(field=field, value=value):
                payload = dict(base)
                payload[field] = value
                result = broker.submit_order(LiveOrder(**payload))  # type: ignore[arg-type]
                self.assertFalse(result.accepted)
                self.assertIn(expected_reason, result.reasons)

    def test_huge_notional_cannot_rely_on_caller_supplied_zero_exposure(self) -> None:
        client = FakeSubmitClient()
        broker = AlpacaLiveBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(live_trading_allowed=True),
            submit_enabled=True,
        )
        order = LiveOrder(
            symbol="SPY",
            side="buy",
            client_order_id="live-huge",
            notional=1_000_000_000.0,
            reference_price=100.0,
            live_price=100.0,
            estimated_position_weight=0.0,
            projected_gross_exposure=0.0,
            daily_pnl_pct=0.0,
            current_drawdown_pct=0.0,
        )

        result = broker.submit_order(order)

        self.assertFalse(result.accepted)
        self.assertIn("live_risk_context_unverified", result.reasons)
        self.assertIn("live_submit_disabled_pending_p0_controls", result.reasons)
        self.assertEqual(client.submitted, [])


if __name__ == "__main__":
    unittest.main()
