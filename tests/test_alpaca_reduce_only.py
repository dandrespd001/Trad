import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder
from trading_ai.risk.policy import RiskLimits


class _NotFoundError(RuntimeError):
    status_code = 404


class ReduceOnlyClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, object]],
        open_orders: list[dict[str, object]] | None = None,
    ) -> None:
        self.positions = positions
        self.open_orders = open_orders or []
        self.submit_calls: list[dict[str, object]] = []

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, object]:
        raise _NotFoundError(f"order not found: {client_order_id}")

    def list_positions(self) -> list[dict[str, object]]:
        return self.positions

    def get_orders(self, filter: object | None = None) -> list[dict[str, object]]:
        del filter
        return self.open_orders

    def submit_order(self, **kwargs: object) -> dict[str, object]:
        self.submit_calls.append(dict(kwargs))
        return {"id": "broker-close-1", "status": "accepted", **kwargs}


class FixedMarketDataClient:
    def get_stock_latest_trade(self, request: object) -> dict[str, object]:
        symbol = getattr(request, "symbol_or_symbols", "SPY")
        if isinstance(symbol, list):
            symbol = symbol[0]

        class Trade:
            price = 100.0

        return {str(symbol): Trade()}


def _position(symbol: str, quantity: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "qty": str(quantity),
        "market_value": str(quantity * 100),
        "avg_entry_price": "100",
        "current_price": "100",
    }


def _open_order(*, symbol: str, side: str, quantity: float) -> dict[str, object]:
    return {
        "id": "working-close",
        "client_order_id": "working-close",
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "status": "accepted",
        "qty": str(quantity),
        "notional": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2026-07-14T14:30:00Z",
        "created_at": "2026-07-14T14:30:00Z",
        "updated_at": "2026-07-14T14:30:00Z",
        "expires_at": "",
    }


class AlpacaLocalReduceOnlyTests(unittest.TestCase):
    def _broker(
        self,
        client: ReduceOnlyClient,
        root: str,
        *,
        market_data: object | None = None,
    ) -> AlpacaPaperBroker:
        return AlpacaPaperBroker(
            client=client,
            allowlist=("SPY",),
            risk_limits=RiskLimits(),
            dry_run=False,
            today=lambda: date(2026, 7, 14),
            market_data=market_data,
            order_journal_path=Path(root) / "orders.sqlite3",
        )

    def test_sell_without_long_is_blocked_before_submit(self) -> None:
        client = ReduceOnlyClient(positions=[])
        with tempfile.TemporaryDirectory() as tmp:
            result = self._broker(client, tmp).submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=1,
                    client_order_id="close-spy-1",
                    position_intent="close",
                )
            )

        self.assertFalse(result.accepted)
        self.assertIn("reducing_position_missing", result.reasons)
        self.assertEqual(client.submit_calls, [])

    def test_sell_larger_than_long_position_is_blocked(self) -> None:
        client = ReduceOnlyClient(positions=[_position("SPY", 1.0)])
        with tempfile.TemporaryDirectory() as tmp:
            result = self._broker(client, tmp).submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=2,
                    client_order_id="close-spy-2",
                    position_intent="close",
                )
            )

        self.assertFalse(result.accepted)
        self.assertIn("reducing_quantity_exceeds_position", result.reasons)
        self.assertEqual(client.submit_calls, [])

    def test_pending_close_quantity_is_reserved(self) -> None:
        client = ReduceOnlyClient(
            positions=[_position("SPY", 3.0)],
            open_orders=[_open_order(symbol="SPY", side="sell", quantity=2.0)],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = self._broker(client, tmp).submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=2,
                    client_order_id="reduce-spy-2",
                    position_intent="reduce",
                )
            )

        self.assertFalse(result.accepted)
        self.assertIn("reducing_quantity_exceeds_position", result.reasons)
        self.assertEqual(client.submit_calls, [])

    def test_unreconciled_prior_close_blocks_a_new_close_id_on_stale_position(self) -> None:
        client = ReduceOnlyClient(positions=[_position("SPY", 1.0)])
        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(client, tmp)
            first = broker.submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=1,
                    client_order_id="close-spy-first-day",
                    position_intent="close",
                )
            )
            second = broker.submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=1,
                    client_order_id="close-spy-second-day",
                    position_intent="close",
                )
            )

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(
            second.reasons,
            ("unreconciled_reducing_order_exists",),
        )
        self.assertEqual(len(client.submit_calls), 1)

    def test_pending_close_with_unknown_quantity_blocks_reduction(self) -> None:
        working = _open_order(symbol="SPY", side="sell", quantity=1.0)
        working["qty"] = None
        working["notional"] = "100"
        client = ReduceOnlyClient(
            positions=[_position("SPY", 3.0)],
            open_orders=[working],
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = self._broker(client, tmp).submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=1,
                    client_order_id="reduce-spy-unknown-working-qty",
                    position_intent="reduce",
                )
            )

        self.assertFalse(result.accepted)
        self.assertIn("reducing_open_order_quantity_unknown", result.reasons)
        self.assertEqual(client.submit_calls, [])

    def test_pending_close_with_non_finite_quantity_snapshot_blocks_reduction(self) -> None:
        for field in ("qty", "filled_qty"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                working = _open_order(symbol="SPY", side="sell", quantity=1.0)
                working[field] = "NaN"
                client = ReduceOnlyClient(
                    positions=[_position("SPY", 3.0)],
                    open_orders=[working],
                )

                result = self._broker(client, tmp).submit_order(
                    PaperOrder(
                        symbol="SPY",
                        side="sell",
                        quantity=1,
                        client_order_id=f"reduce-spy-non-finite-{field}",
                        position_intent="reduce",
                    )
                )

                self.assertFalse(result.accepted)
                self.assertIn("reducing_snapshot_unavailable", result.reasons)
                self.assertEqual(client.submit_calls, [])

    def test_buy_to_cover_short_is_allowed_without_crossing_zero(self) -> None:
        client = ReduceOnlyClient(positions=[_position("SPY", -2.0)])
        with tempfile.TemporaryDirectory() as tmp:
            result = self._broker(client, tmp).submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="buy",
                    quantity=2,
                    client_order_id="close-short-spy",
                    position_intent="close",
                    daily_pnl_pct=-99,
                    current_drawdown_pct=99,
                )
            )

        self.assertTrue(result.accepted)
        self.assertEqual(len(client.submit_calls), 1)
        self.assertEqual(client.submit_calls[0]["side"], "buy")

    def test_existing_non_allowlisted_exposure_can_close_but_cannot_open(self) -> None:
        client = ReduceOnlyClient(positions=[_position("TSLA", 2.0)])
        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(client, tmp, market_data=FixedMarketDataClient())
            close_result = broker.submit_order(
                PaperOrder(
                    symbol="TSLA",
                    side="sell",
                    quantity=2,
                    client_order_id="emergency-close-tsla",
                    position_intent="close",
                )
            )
            open_result = broker.submit_order(
                PaperOrder(
                    symbol="TSLA",
                    side="buy",
                    quantity=1,
                    client_order_id="forbidden-open-tsla",
                    reference_price=100.0,
                    position_intent="open",
                )
            )

        self.assertTrue(close_result.accepted)
        self.assertFalse(open_result.accepted)
        self.assertIn("symbol_not_allowlisted", open_result.reasons)
        self.assertEqual(len(client.submit_calls), 1)
        self.assertEqual(client.submit_calls[0]["symbol"], "TSLA")
        self.assertEqual(client.submit_calls[0]["side"], "sell")

    def test_kill_switch_still_allows_locally_bounded_close(self) -> None:
        client = ReduceOnlyClient(positions=[_position("SPY", 1.0)])
        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(client, tmp)
            broker.activate_kill_switch("drawdown")
            result = broker.submit_order(
                PaperOrder(
                    symbol="SPY",
                    side="sell",
                    quantity=1,
                    client_order_id="kill-switch-close-spy",
                    position_intent="close",
                    daily_pnl_pct=-99,
                    current_drawdown_pct=99,
                )
            )

        self.assertTrue(result.accepted)
        self.assertEqual(len(client.submit_calls), 1)


if __name__ == "__main__":
    unittest.main()
