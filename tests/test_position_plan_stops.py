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

    def test_hold_action_includes_dynamic_protective_levels(self) -> None:
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=112.0, avg_entry_price=100.0, current_price=112.0
        )
        plan = self._plan(
            position,
            signal=_buy_signal("SPY", atr=5.0),
            stop_loss_atr_mult=2.0,
            take_profit_atr_mult=4.0,
            trailing_atr_mult=3.0,
        )

        levels = hold_actions(plan)[0]["protective_levels"]

        self.assertEqual(levels["avg_entry_price"], 100.0)
        self.assertEqual(levels["current_price"], 112.0)
        self.assertEqual(levels["atr"], 5.0)
        self.assertEqual(levels["stop_loss_price"], 90.0)
        self.assertEqual(levels["take_profit_price"], 120.0)
        self.assertEqual(levels["trailing_high"], 112.0)
        self.assertEqual(levels["trailing_stop_price"], 97.0)

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

    def test_breakeven_disarmed_when_high_below_trigger(self) -> None:
        # entry 100, ATR 5, trigger 2*ATR => needs high >= 110. High-water is only 108.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=108.0, avg_entry_price=100.0, current_price=108.0
        )
        plan = cast(
            dict[str, Any],
            build_position_plan(
                signals=[_buy_signal("SPY", atr=5.0)],
                selected_signal=_buy_signal("SPY", atr=5.0),
                positions=[position],
                signal_quality={"allowed": True},
                paper_notional_usd=1.0,
                stop_loss_atr_mult=2.0,
                breakeven_trigger_atr_mult=2.0,
                breakeven_buffer_atr_mult=0.5,
            ),
        )
        levels = hold_actions(plan)[0]["protective_levels"]
        self.assertIsNone(levels["breakeven_stop_price"])
        self.assertEqual(levels["effective_stop_price"], levels["stop_loss_price"])
        self.assertEqual(close_actions(plan), [])

    def test_breakeven_arms_from_persisted_high_water_even_if_price_pulled_back(self) -> None:
        # High-water mark (persisted) reached 111 => trigger 2*ATR=10 satisfied (>=110).
        # Current price has since pulled back to 96, below entry.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=96.0, avg_entry_price=100.0, current_price=96.0
        )
        plan = build_position_plan(
            signals=[_buy_signal("SPY", atr=5.0)],
            selected_signal=_buy_signal("SPY", atr=5.0),
            positions=[position],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
            breakeven_trigger_atr_mult=2.0,
            breakeven_buffer_atr_mult=0.5,
            trailing_high_by_symbol={"SPY": 111.0},
        )
        closes = close_actions(cast(dict[str, Any], plan))
        # breakeven_stop_price = entry + 0.5*5 = 102.5; price 96 <= 102.5 => breach.
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "breakeven_stop")
        self.assertEqual(closes[0]["protective_levels"]["breakeven_stop_price"], 102.5)

    def test_effective_stop_price_is_monotonic_across_runs_with_rising_high(self) -> None:
        # Simulates two successive runs where the persisted trailing_high grows
        # (as it would via risk_state.trailing_stops). effective_stop_price must
        # never decrease across runs -- it is a ratchet.
        def plan_for(*, current_price: float, persisted_high: float) -> dict[str, Any]:
            position = PaperPosition(
                symbol="SPY",
                quantity=1.0,
                market_value=current_price,
                avg_entry_price=100.0,
                current_price=current_price,
            )
            return cast(
                dict[str, Any],
                build_position_plan(
                    signals=[_buy_signal("SPY", atr=5.0)],
                    selected_signal=_buy_signal("SPY", atr=5.0),
                    positions=[position],
                    signal_quality={"allowed": True},
                    paper_notional_usd=1.0,
                    trailing_atr_mult=1.0,
                    breakeven_trigger_atr_mult=2.0,
                    breakeven_buffer_atr_mult=0.5,
                    trailing_high_by_symbol={"SPY": persisted_high},
                ),
            )

        # Run 1: high reaches 111 (arms breakeven at 102.5; trailing_stop = 106).
        first = plan_for(current_price=111.0, persisted_high=100.0)
        first_level = hold_actions(first)[0]["protective_levels"]["effective_stop_price"]
        self.assertEqual(first_level, 106.0)

        # Run 2: persisted high from run 1 carried forward, new high reaches 118.
        second = plan_for(current_price=118.0, persisted_high=111.0)
        second_level = hold_actions(second)[0]["protective_levels"]["effective_stop_price"]
        self.assertEqual(second_level, 113.0)

        self.assertGreaterEqual(second_level, first_level)

    def test_max_level_rule_trailing_above_breakeven_wins(self) -> None:
        # entry 100, ATR 5. trailing_atr_mult 1.0 => trailing_stop = high - 5.
        # breakeven trigger 2*ATR=10 (high>=110), buffer 0.5 => breakeven_stop = 102.5.
        # With high 120: trailing_stop = 115 (> 102.5) => trailing wins.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=114.0, avg_entry_price=100.0, current_price=114.0
        )
        plan = build_position_plan(
            signals=[_buy_signal("SPY", atr=5.0)],
            selected_signal=_buy_signal("SPY", atr=5.0),
            positions=[position],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
            trailing_atr_mult=1.0,
            breakeven_trigger_atr_mult=2.0,
            breakeven_buffer_atr_mult=0.5,
            trailing_high_by_symbol={"SPY": 120.0},
        )
        closes = close_actions(cast(dict[str, Any], plan))
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "trailing_stop")

    def test_max_level_rule_breakeven_above_trailing_wins(self) -> None:
        # entry 100, ATR 5. trailing_atr_mult 4.0 => trailing_stop = high - 20.
        # breakeven trigger 2*ATR=10 (high>=110), buffer 0.5 => breakeven_stop = 102.5.
        # With high 120: trailing_stop = 100 (< 102.5) => breakeven wins.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=102.0, avg_entry_price=100.0, current_price=102.0
        )
        plan = build_position_plan(
            signals=[_buy_signal("SPY", atr=5.0)],
            selected_signal=_buy_signal("SPY", atr=5.0),
            positions=[position],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
            trailing_atr_mult=4.0,
            breakeven_trigger_atr_mult=2.0,
            breakeven_buffer_atr_mult=0.5,
            trailing_high_by_symbol={"SPY": 120.0},
        )
        closes = close_actions(cast(dict[str, Any], plan))
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "breakeven_stop")

    def test_max_level_rule_exact_tie_prefers_breakeven(self) -> None:
        # entry 100, ATR 5, high 120.
        # trailing_atr_mult 3.5 => trailing_stop = 120 - 17.5 = 102.5.
        # breakeven trigger 2*ATR=10, buffer 0.5 => breakeven_stop = 102.5 (tie).
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=102.0, avg_entry_price=100.0, current_price=102.0
        )
        plan = build_position_plan(
            signals=[_buy_signal("SPY", atr=5.0)],
            selected_signal=_buy_signal("SPY", atr=5.0),
            positions=[position],
            signal_quality={"allowed": True},
            paper_notional_usd=1.0,
            trailing_atr_mult=3.5,
            breakeven_trigger_atr_mult=2.0,
            breakeven_buffer_atr_mult=0.5,
            trailing_high_by_symbol={"SPY": 120.0},
        )
        closes = close_actions(cast(dict[str, Any], plan))
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "breakeven_stop")

    def test_effective_stop_price_max_with_none_subsets(self) -> None:
        # Only trailing active (stop_loss and breakeven both off) => effective == trailing.
        position = PaperPosition(
            symbol="SPY", quantity=1.0, market_value=112.0, avg_entry_price=100.0, current_price=112.0
        )
        plan = cast(
            dict[str, Any],
            build_position_plan(
                signals=[_buy_signal("SPY", atr=5.0)],
                selected_signal=_buy_signal("SPY", atr=5.0),
                positions=[position],
                signal_quality={"allowed": True},
                paper_notional_usd=1.0,
                trailing_atr_mult=3.0,
            ),
        )
        levels = hold_actions(plan)[0]["protective_levels"]
        self.assertIsNone(levels["stop_loss_price"])
        self.assertIsNone(levels["breakeven_stop_price"])
        self.assertEqual(levels["effective_stop_price"], levels["trailing_stop_price"])

        # Nothing active => effective is None.
        plan_none = cast(
            dict[str, Any],
            build_position_plan(
                signals=[_buy_signal("SPY", atr=5.0)],
                selected_signal=_buy_signal("SPY", atr=5.0),
                positions=[position],
                signal_quality={"allowed": True},
                paper_notional_usd=1.0,
            ),
        )
        levels_none = hold_actions(plan_none)[0]["protective_levels"]
        self.assertIsNone(levels_none["effective_stop_price"])

    def test_breakeven_trigger_with_zero_buffer_arms_at_entry(self) -> None:
        # trigger > 0 but buffer == 0.0 => breakeven stop at entry exactly.
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
                breakeven_trigger_atr_mult=2.0,
                breakeven_buffer_atr_mult=0.0,
            ),
        )
        levels = hold_actions(plan)[0]["protective_levels"]
        self.assertEqual(levels["breakeven_stop_price"], 100.0)
        self.assertEqual(levels["effective_stop_price"], 100.0)

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
