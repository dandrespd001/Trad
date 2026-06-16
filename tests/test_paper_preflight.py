import unittest
from datetime import date

from trading_ai.execution.alpaca_paper import (
    PaperOrderSnapshot,
    PaperPosition,
    evaluate_paper_preflight,
)
from trading_ai.models.signals import ModelSignal


def _buy_signal(timestamp: str = "2024-03-29", symbol: str = "SPY") -> ModelSignal:
    return ModelSignal(
        timestamp=timestamp,
        symbol=symbol,
        probability=0.75,
        threshold=0.5,
        action="buy",
    )


def _open_order(
    *,
    symbol: str = "SPY",
    client_order_id: str = "other-order",
) -> PaperOrderSnapshot:
    return PaperOrderSnapshot(
        order_id="broker-order-1",
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        order_type="market",
        time_in_force="day",
        status="accepted",
        notional=1.0,
        quantity=None,
        filled_quantity=0.0,
        filled_avg_price=None,
        submitted_at="2024-03-29T14:30:00Z",
        created_at="2024-03-29T14:30:00Z",
        updated_at="2024-03-29T14:30:00Z",
        expires_at="2024-04-01T20:00:00Z",
    )


class PaperPreflightTests(unittest.TestCase):
    def test_allows_fresh_buy_signal_without_orders_or_positions(self) -> None:
        decision = evaluate_paper_preflight(
            signal=_buy_signal(),
            client_order_id="signal-spy-20240329",
            open_orders=(),
            positions=(),
            as_of_date=date(2024, 4, 1),
            max_feature_age_days=5,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.checked_at, "2024-04-01")
        self.assertEqual(decision.max_feature_age_days, 5)

    def test_blocks_stale_features(self) -> None:
        decision = evaluate_paper_preflight(
            signal=_buy_signal(timestamp="2024-03-29"),
            client_order_id="signal-spy-20240329",
            open_orders=(),
            positions=(),
            as_of_date=date(2026, 6, 16),
            max_feature_age_days=5,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("stale_features", decision.reasons)

    def test_blocks_open_order_for_same_symbol(self) -> None:
        decision = evaluate_paper_preflight(
            signal=_buy_signal(symbol="SPY"),
            client_order_id="signal-spy-20240329",
            open_orders=(_open_order(symbol="SPY", client_order_id="other-order"),),
            positions=(),
            as_of_date=date(2024, 4, 1),
            max_feature_age_days=5,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("open_order_exists", decision.reasons)

    def test_blocks_duplicate_client_order_id(self) -> None:
        decision = evaluate_paper_preflight(
            signal=_buy_signal(symbol="SPY"),
            client_order_id="signal-spy-20240329",
            open_orders=(_open_order(symbol="QQQ", client_order_id="signal-spy-20240329"),),
            positions=(),
            as_of_date=date(2024, 4, 1),
            max_feature_age_days=5,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("duplicate_client_order_id", decision.reasons)

    def test_blocks_existing_position_for_same_symbol(self) -> None:
        decision = evaluate_paper_preflight(
            signal=_buy_signal(symbol="SPY"),
            client_order_id="signal-spy-20240329",
            open_orders=(),
            positions=(PaperPosition(symbol="SPY", quantity=1.0, market_value=500.0),),
            as_of_date=date(2024, 4, 1),
            max_feature_age_days=5,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("position_exists", decision.reasons)

    def test_blocks_when_no_buy_signal_is_selected(self) -> None:
        decision = evaluate_paper_preflight(
            signal=None,
            client_order_id=None,
            open_orders=(),
            positions=(),
            as_of_date=date(2024, 4, 1),
            max_feature_age_days=5,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ("no_buy_signal",))


if __name__ == "__main__":
    unittest.main()
