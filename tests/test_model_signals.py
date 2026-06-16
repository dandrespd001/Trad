import unittest

from trading_ai.models.baseline import LogisticBaselineModel
from trading_ai.models.signals import generate_model_signals, latest_valid_feature_rows


class ModelSignalTests(unittest.TestCase):
    def test_latest_valid_feature_rows_uses_latest_row_with_required_features(self) -> None:
        rows = [
            {"timestamp": "2024-01-01", "symbol": "SPY", "momentum_20": "0.10"},
            {"timestamp": "2024-01-02", "symbol": "SPY", "momentum_20": ""},
            {"timestamp": "2024-01-03", "symbol": "QQQ", "momentum_20": "-0.20"},
            {"timestamp": "2024-01-04", "symbol": "TSLA", "momentum_20": "0.30"},
        ]

        latest = latest_valid_feature_rows(rows, feature_names=("momentum_20",), allowlist=("SPY", "QQQ"))

        self.assertEqual(set(latest), {"SPY", "QQQ"})
        self.assertEqual(latest["SPY"]["timestamp"], "2024-01-01")
        self.assertEqual(latest["QQQ"]["timestamp"], "2024-01-03")

    def test_generate_model_signals_maps_probabilities_to_buy_or_hold(self) -> None:
        model = LogisticBaselineModel(
            feature_names=("momentum_20",),
            intercept=0.0,
            coefficients=(5.0,),
        )
        rows = [
            {"timestamp": "2024-01-01", "symbol": "SPY", "momentum_20": "-0.20"},
            {"timestamp": "2024-01-02", "symbol": "SPY", "momentum_20": "0.20"},
            {"timestamp": "2024-01-02", "symbol": "QQQ", "momentum_20": "-0.20"},
        ]

        signals = generate_model_signals(rows, model=model, allowlist=("SPY", "QQQ"), threshold=0.5)

        by_symbol = {signal.symbol: signal for signal in signals}
        self.assertEqual(by_symbol["SPY"].action, "buy")
        self.assertGreaterEqual(by_symbol["SPY"].probability, 0.5)
        self.assertEqual(by_symbol["QQQ"].action, "hold")
        self.assertLess(by_symbol["QQQ"].probability, 0.5)


if __name__ == "__main__":
    unittest.main()
