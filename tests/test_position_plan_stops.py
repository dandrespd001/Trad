import unittest
from typing import Any, cast

from trading_ai.execution.alpaca_paper import PaperPosition
from trading_ai.execution.paper_position_plan import build_position_plan, close_actions, hold_actions
from trading_ai.features.engineering import build_features


def _buy_signal(symbol: str, *, atr: float | None) -> dict[str, Any]:
    return {"symbol": symbol, "action": "buy", "probability": 0.7, "threshold": 0.5, "atr": atr}


class ProtectiveExitTests(unittest.TestCase):
    def _plan(self, position: PaperPosition, *, signal: dict[str, Any], **mults: float) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            build_position_plan(
                signals=[signal],
                selected_signal=signal,
                positions=[position],
                signal_quality={"allowed": True},
                paper_notional_usd=1.0,
                stop_loss_atr_mult=mults.get("stop_loss_atr_mult", 0.0),
                take_profit_atr_mult=mults.get("take_profit_atr_mult", 0.0),
                trailing_atr_mult=mults.get("trailing_atr_mult", 0.0),
            ),
        )

    def test_stop_loss_triggers_close(self) -> None:
        # entry 100, ATR 5, stop at 2*ATR => 90. Price 89 breaches.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=89.0, avg_entry_price=100.0, current_price=89.0
        )
        plan = self._plan(position, signal=_buy_signal("SPY", atr=5.0), stop_loss_atr_mult=2.0)
        closes = close_actions(plan)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "stop_loss")

    def test_take_profit_triggers_close(self) -> None:
        # entry 100, ATR 5, tp at 4*ATR => 120. Price 121 breaches.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=121.0, avg_entry_price=100.0, current_price=121.0
        )
        plan = self._plan(position, signal=_buy_signal("SPY", atr=5.0), take_profit_atr_mult=4.0)
        closes = close_actions(plan)
        self.assertEqual(closes[0]["reason"], "take_profit")

    def test_protective_exit_overrides_active_buy_signal(self) -> None:
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=85.0, avg_entry_price=100.0, current_price=85.0
        )
        plan = self._plan(position, signal=_buy_signal("SPY", atr=5.0), stop_loss_atr_mult=2.0)
        # Despite a buy signal, the stop wins.
        self.assertEqual(close_actions(plan)[0]["reason"], "stop_loss")
        self.assertEqual(hold_actions(plan), [])

    def test_within_bounds_holds(self) -> None:
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=101.0, avg_entry_price=100.0, current_price=101.0
        )
        plan = self._plan(
            position,
            signal=_buy_signal("SPY", atr=5.0),
            stop_loss_atr_mult=2.0,
            take_profit_atr_mult=4.0,
            trailing_atr_mult=3.0,
        )
        self.assertEqual(close_actions(plan), [])
        self.assertEqual(hold_actions(plan)[0]["reason"], "position_matches_buy_signal")

    def test_trailing_stop_uses_persisted_high(self) -> None:
        # Price ran up to 130 previously; ATR 5, trailing 3*ATR => stop at 115.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=114.0, avg_entry_price=100.0, current_price=114.0
        )
        plan = build_position_plan(
            signals=[_buy_signal("SPY", atr=5.0)],
            selected_signal=_buy_signal("SPY", atr=5.0),
            positions=[position],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
            trailing_atr_mult=3.0,
            trailing_high_by_symbol={"SPY": 130.0},
        )
        self.assertEqual(close_actions(plan)[0]["reason"], "trailing_stop")

    def test_missing_atr_disables_protective_exit(self) -> None:
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=80.0, avg_entry_price=100.0, current_price=80.0
        )
        plan = self._plan(position, signal=_buy_signal("SPY", atr=None), stop_loss_atr_mult=2.0)
        # No ATR -> no protective exit; buy signal -> HOLD.
        self.assertEqual(close_actions(plan), [])
        self.assertEqual(hold_actions(plan)[0]["reason"], "position_matches_buy_signal")

    def test_trailing_highs_reported_in_summary(self) -> None:
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=110.0, avg_entry_price=100.0, current_price=110.0
        )
        plan = cast(
            dict[str, Any],
            build_position_plan(
                signals=[_buy_signal("SPY", atr=5.0)],
                selected_signal=_buy_signal("SPY", atr=5.0),
                positions=[position],
                signal_quality={"allowed": True},
                paper_notional_usd=1.0,
                trailing_high_by_symbol={"SPY": 105.0},
            ),
        )
        self.assertEqual(plan["summary"]["trailing_highs"], {"SPY": 110.0})


class AtrFeatureTests(unittest.TestCase):
    def test_atr_and_true_range_computed(self) -> None:
        records = []
        for index in range(20):
            base = 100.0 + index
            records.append(
                {
                    "symbol": "SPY",
                    "timestamp": f"2026-01-{index + 1:02d}",
                    "open": base,
                    "high": base + 2.0,
                    "low": base - 2.0,
                    "close": base + 1.0,
                    "volume": 1000.0,
                }
            )
        featured = build_features(records)
        last = cast(dict[str, Any], featured[-1])
        self.assertIn("true_range", last)
        self.assertIsNotNone(last["true_range"])
        self.assertIn("atr_14", last)
        self.assertIsNotNone(last["atr_14"])
        self.assertGreater(float(last["atr_14"]), 0.0)


if __name__ == "__main__":
    unittest.main()
