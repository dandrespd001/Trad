import unittest

from trading_ai.backtest.engine import run_signal_policy_backtest
from trading_ai.models.baseline import LogisticBaselineModel


def _model(*, intercept: float) -> LogisticBaselineModel:
    # coefficient 0 => probability depends only on the intercept (deterministic action).
    return LogisticBaselineModel(feature_names=("f",), intercept=intercept, coefficients=(0.0,))


def _records(symbol: str, closes: list[float]) -> list[dict[str, object]]:
    return [
        {"symbol": symbol, "timestamp": f"2026-01-{i + 1:02d}", "f": 1.0, "close": close}
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
        records: list[dict[str, object]] = []
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


if __name__ == "__main__":
    unittest.main()
