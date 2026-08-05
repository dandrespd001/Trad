import unittest
from typing import Any

from trading_ai.backtest.engine import BacktestConfig, run_signal_policy_backtest
from trading_ai.models.baseline import LogisticBaselineModel


def _model(*, intercept: float) -> LogisticBaselineModel:
    # coefficient 0 => probability depends only on the intercept (deterministic action).
    return LogisticBaselineModel(feature_names=("f",), intercept=intercept, coefficients=(0.0,))


def _records(
    symbol: str,
    closes: list[float],
    *,
    opens: list[float] | None = None,
    features: list[float] | None = None,
) -> list[dict[str, Any]]:
    execution_opens = opens if opens is not None else [closes[0], *closes[:-1]]
    feature_values = features if features is not None else [1.0] * len(closes)
    if len(execution_opens) != len(closes) or len(feature_values) != len(closes):
        raise ValueError("opens, closes, and features must have equal length")
    return [
        {
            "symbol": symbol,
            "timestamp": f"2026-01-{i + 1:02d}",
            "f": feature_values[i],
            "open": execution_opens[i],
            "close": close,
        }
        for i, close in enumerate(closes)
    ]


class SignalPolicyBacktestTests(unittest.TestCase):
    def test_always_buy_single_symbol_compounds_returns(self) -> None:
        records = _records("SPY", [100.0, 110.0, 121.0, 133.1])
        result = run_signal_policy_backtest(records, _model(intercept=10.0))
        self.assertEqual(result.metrics["trade_count"], 1.0)  # one entry, then held
        self.assertEqual(result.metrics["average_exposure"], 1.0)
        self.assertGreater(result.metrics["cumulative_return"], 0.25)
        self.assertGreater(result.metrics["sharpe"], 0.0)
        self.assertEqual(result.metadata["strategy"], "signal_policy_single_name")

    def test_no_buy_signal_stays_in_cash(self) -> None:
        records = _records("SPY", [100.0, 110.0, 121.0])
        result = run_signal_policy_backtest(records, _model(intercept=-10.0))
        self.assertEqual(result.metrics["trade_count"], 0.0)
        self.assertEqual(result.metrics["average_exposure"], 0.0)
        self.assertEqual(result.metrics["cumulative_return"], 0.0)

    def test_too_many_buys_blocks_selection(self) -> None:
        records: list[dict[str, Any]] = []
        for symbol in ("SPY", "QQQ", "IWM", "TLT"):
            records.extend(_records(symbol, [100.0, 110.0, 121.0]))
        # 4 simultaneous buys but max_buy_signals=3 -> no position taken.
        result = run_signal_policy_backtest(records, _model(intercept=10.0), max_buy_signals=3)
        self.assertEqual(result.metrics["average_exposure"], 0.0)
        self.assertEqual(result.metrics["trade_count"], 0.0)

    def test_margin_filter_blocks_low_conviction(self) -> None:
        records = _records("SPY", [100.0, 110.0, 121.0])
        # probability ~0.5 (intercept 0) -> margin below 0.2 -> no buy.
        result = run_signal_policy_backtest(records, _model(intercept=0.0), min_signal_margin=0.2)
        self.assertEqual(result.metrics["average_exposure"], 0.0)

    def test_rotation_between_symbols_incurs_turnover(self) -> None:
        # SPY rises early then flattens; QQQ rises later. With always-buy the policy
        # holds the alphabetically-max symbol (SPY) consistently; turnover stays low.
        records = _records("SPY", [100.0, 110.0, 121.0]) + _records("QQQ", [100.0, 90.0, 80.0])
        result = run_signal_policy_backtest(records, _model(intercept=10.0))
        self.assertEqual(result.metrics["average_exposure"], 1.0)
        self.assertGreaterEqual(result.metrics["turnover"], 1.0)

    def test_new_signal_executes_at_next_open_without_capturing_prior_gap(self) -> None:
        model = LogisticBaselineModel(
            feature_names=("f",),
            intercept=-5.0,
            coefficients=(10.0,),
        )
        records = _records(
            "SPY",
            [100.0, 110.0, 202.0],
            opens=[100.0, 110.0, 200.0],
            features=[0.0, 1.0, 1.0],
        )

        result = run_signal_policy_backtest(
            records,
            model,
            config=BacktestConfig(cost_bps=0.0, slippage_bps=0.0),
        )

        self.assertEqual(result.daily_returns[0], 0.0)
        self.assertAlmostEqual(result.daily_returns[1], 202.0 / 200.0 - 1.0, places=12)

    def test_existing_signal_position_owns_the_next_gap(self) -> None:
        records = _records(
            "SPY",
            [100.0, 110.0, 55.0],
            opens=[100.0, 100.0, 55.0],
        )

        result = run_signal_policy_backtest(
            records,
            _model(intercept=10.0),
            config=BacktestConfig(cost_bps=0.0, slippage_bps=0.0),
        )

        self.assertAlmostEqual(result.daily_returns[-1], -0.5, places=12)

    def test_signal_policy_transaction_cost_is_charged_once(self) -> None:
        model = LogisticBaselineModel(
            feature_names=("f",),
            intercept=-5.0,
            coefficients=(10.0,),
        )
        records = _records(
            "SPY",
            [100.0, 110.0, 202.0],
            opens=[100.0, 110.0, 200.0],
            features=[0.0, 1.0, 1.0],
        )

        result = run_signal_policy_backtest(
            records,
            model,
            config=BacktestConfig(cost_bps=25.0, slippage_bps=0.0),
        )

        expected_return = (1.0 - 0.0025) * (202.0 / 200.0) - 1.0
        self.assertAlmostEqual(result.daily_returns[-1], expected_return, places=12)
        self.assertAlmostEqual(result.metrics["estimated_costs"], 0.0025, places=12)

    def test_rotation_keeps_old_gap_and_new_intraday_return_separate(self) -> None:
        model = LogisticBaselineModel(
            feature_names=("f",),
            intercept=-5.0,
            coefficients=(10.0,),
        )
        records = _records(
            "SPY",
            [100.0, 100.0, 50.0],
            opens=[100.0, 100.0, 50.0],
            features=[1.0, 0.0, 0.0],
        )
        records.extend(
            _records(
                "QQQ",
                [100.0, 100.0, 220.0],
                opens=[100.0, 100.0, 200.0],
                features=[0.0, 1.0, 1.0],
            )
        )

        result = run_signal_policy_backtest(
            records,
            model,
            config=BacktestConfig(cost_bps=0.0, slippage_bps=0.0),
        )

        # SPY, the old book, owns the -50% gap. QQQ, selected at the open,
        # owns only its +10% intraday move: (1 - .5) * (1 + .1) - 1.
        self.assertAlmostEqual(result.daily_returns[-1], -0.45, places=12)
        self.assertAlmostEqual(result.metrics["turnover"], 3.0, places=12)


if __name__ == "__main__":
    unittest.main()
