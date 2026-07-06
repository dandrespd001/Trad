import unittest

from trading_ai.models.baseline import LogisticBaselineModel
from trading_ai.models.signals import SignalPolicyConfig, generate_model_signals, latest_valid_feature_rows


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

    def test_generate_model_signals_emits_open_long_with_governed_metadata(self) -> None:
        model = LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(8.0,))
        rows = [
            {
                "timestamp": "2026-06-30",
                "symbol": "SPY",
                "momentum_20": "0.2",
                "atr_14": "2.0",
                "close": "101.0",
            }
        ]

        signals = generate_model_signals(
            rows,
            model=model,
            allowlist=("SPY",),
            threshold=0.55,
            policy=SignalPolicyConfig(model_id="candidate-a", max_positions=1, max_gross_exposure=0.3),
        )

        signal = signals[0]
        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.policy_action, "open_long")
        self.assertEqual(signal.model_id, "candidate-a")
        self.assertGreaterEqual(signal.open_score, 0.55)
        self.assertIn("probability_above_open_threshold", signal.reason_codes)
        self.assertTrue(signal.safety["paper_only"])
        self.assertFalse(signal.safety["orders_submitted"])
        self.assertEqual(signal.safety["llm_authority"], "none")
        self.assertEqual(signal.risk_inputs["max_positions"], 1)

    def test_generate_model_signals_emits_close_long_for_low_probability_and_atr_exits(self) -> None:
        model = LogisticBaselineModel(feature_names=("momentum_20",), intercept=0.0, coefficients=(8.0,))
        rows = [
            {
                "timestamp": "2026-06-30",
                "symbol": "LOWP",
                "momentum_20": "-0.2",
                "atr_14": "2.0",
                "close": "100.0",
            },
            {
                "timestamp": "2026-06-30",
                "symbol": "STOP",
                "momentum_20": "0.2",
                "atr_14": "2.0",
                "close": "95.0",
            },
            {
                "timestamp": "2026-06-30",
                "symbol": "TAKE",
                "momentum_20": "0.2",
                "atr_14": "2.0",
                "close": "109.0",
            },
            {
                "timestamp": "2026-06-30",
                "symbol": "TRAIL",
                "momentum_20": "0.2",
                "atr_14": "2.0",
                "close": "103.0",
            },
        ]
        positions = {
            "LOWP": {"entry_price": 100.0, "highest_price": 106.0},
            "STOP": {"entry_price": 100.0, "highest_price": 104.0},
            "TAKE": {"entry_price": 100.0, "highest_price": 109.0},
            "TRAIL": {"entry_price": 100.0, "highest_price": 110.0},
        }

        signals = generate_model_signals(
            rows,
            model=model,
            allowlist=("LOWP", "STOP", "TAKE", "TRAIL"),
            threshold=0.55,
            policy=SignalPolicyConfig(
                model_id="candidate-a",
                close_threshold=0.45,
                stop_loss_atr_mult=2.0,
                take_profit_atr_mult=4.0,
                trailing_atr_mult=3.0,
                current_positions=positions,
            ),
        )

        by_symbol = {signal.symbol: signal for signal in signals}
        self.assertEqual(by_symbol["LOWP"].action, "hold")
        self.assertEqual(by_symbol["LOWP"].policy_action, "close_long")
        self.assertIn("probability_below_close_threshold", by_symbol["LOWP"].reason_codes)
        self.assertEqual(by_symbol["STOP"].policy_action, "close_long")
        self.assertIn("stop_loss_atr", by_symbol["STOP"].reason_codes)
        self.assertEqual(by_symbol["TAKE"].policy_action, "close_long")
        self.assertIn("take_profit_atr", by_symbol["TAKE"].reason_codes)
        self.assertEqual(by_symbol["TRAIL"].policy_action, "close_long")
        self.assertIn("trailing_atr", by_symbol["TRAIL"].reason_codes)


if __name__ == "__main__":
    unittest.main()
