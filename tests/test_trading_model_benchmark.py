import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_model_research_sweep import (
    directional_records,
    fake_backtest_result,
    write_approved_package,
    write_trading_first_risk,
    write_universe,
)
from trading_ai.backtest.engine import BacktestConfig
from trading_ai.cli import main
from trading_ai.evaluation.trading_model_benchmark import (
    _candidate_spec,
    _evaluate_candidate,
    _load_costs,
    build_benchmark_candidates,
)


class TradingModelBenchmarkTests(unittest.TestCase):
    def test_candidate_plan_includes_champion_extended_and_optional_ml_dependency_states(self) -> None:
        candidates = build_benchmark_candidates(
            (
                "momentum_20",
                "rsi_14",
                "macd_hist",
                "bb_pct_b",
                "ai_sentiment_1d",
                "ai_risk_1d",
                "ai_confidence_1d",
                "forecast_return_1d",
                "forecast_confidence",
            )
        )
        ids = {candidate["candidate_id"]: candidate for candidate in candidates}

        self.assertIn("champion_latest_model", ids)
        self.assertIn("logreg_extended_technical", ids)
        self.assertIn("logreg_ai_features", ids)
        self.assertIn("logreg_forecast_challenger", ids)
        self.assertIn("sklearn_random_forest", ids)
        self.assertIn("lightgbm_classifier", ids)
        self.assertIn("xgboost_classifier", ids)
        self.assertEqual(ids["champion_latest_model"]["baseline_role"], "champion")
        self.assertEqual(ids["logreg_extended_technical"]["features"], ["rsi_14", "macd_hist", "bb_pct_b"])
        self.assertEqual(
            ids["logreg_ai_features"]["features"],
            ["momentum_20", "ai_sentiment_1d", "ai_risk_1d", "ai_confidence_1d"],
        )
        self.assertEqual(
            ids["logreg_forecast_challenger"]["features"],
            ["momentum_20", "forecast_return_1d", "forecast_confidence"],
        )

    def test_trading_model_benchmark_applies_signal_margin_once(self) -> None:
        records = directional_records(days=80)
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        calls = []

        def fake_evaluate(candidate, **kwargs):
            calls.append(kwargs)
            status = "ERROR" if candidate["candidate_id"] == "champion_latest_model" else "OK"
            return {
                **candidate,
                "status": status,
                "dependency_missing": False,
                "metrics": fake_backtest_result().metrics if status == "OK" else {},
                "score": 1.0 if status == "OK" else float("-inf"),
                "reason_codes": [] if status == "OK" else ["test_skip_champion"],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_trading_first_risk(root / "risk.yml")
            output_dir = root / "benchmark"

            with (
                mock.patch("trading_ai.evaluation.trading_model_benchmark.read_records", return_value=records),
                mock.patch(
                    "trading_ai.evaluation.trading_model_benchmark._evaluate_candidate",
                    side_effect=fake_evaluate,
                ),
            ):
                exit_code = main(
                    [
                        "trading-model-benchmark",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2024-01-02",
                        "--to",
                        "2026-06-18",
                        "--as-of-date",
                        "2026-06-18",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)
        self.assertTrue(calls)
        self.assertTrue(all(call["threshold"] == 0.5 for call in calls))
        self.assertTrue(all(call["min_signal_margin"] == 0.05 for call in calls))

    def test_trading_model_benchmark_merges_ai_features_by_symbol_and_timestamp(self) -> None:
        records = directional_records(days=80)
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        calls: list[dict[str, object]] = []

        def fake_read_records(path):
            if Path(path) == ai_features:
                return [
                    {
                        "timestamp": records[-1]["timestamp"],
                        "symbol": "SPY",
                        "ai_sentiment_1d": 0.42,
                        "ai_risk_1d": 0.15,
                        "ai_confidence_1d": 0.91,
                        "forecast_return_1d": 0.025,
                        "forecast_confidence": 0.67,
                    }
                ]
            return records

        def fake_evaluate(candidate, **kwargs):
            calls.append({"candidate": candidate, "feature_records": kwargs["feature_records"]})
            status = "OK" if candidate["candidate_id"] == "logreg_ai_features" else "ERROR"
            return {
                **candidate,
                "status": status,
                "dependency_missing": False,
                "metrics": fake_backtest_result().metrics if status == "OK" else {},
                "score": 1.0 if status == "OK" else float("-inf"),
                "reason_codes": [] if status == "OK" else ["test_skip_candidate"],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_trading_first_risk(root / "risk.yml")
            output_dir = root / "benchmark"
            ai_features = root / "ai_features.csv"
            ai_features.write_text(
                "timestamp,symbol,ai_sentiment_1d,ai_risk_1d,ai_confidence_1d,forecast_return_1d,forecast_confidence\n"
                f"{records[-1]['timestamp']},SPY,0.42,0.15,0.91,0.025,0.67\n",
                encoding="utf-8",
            )

            with (
                mock.patch(
                    "trading_ai.evaluation.trading_model_benchmark.read_records",
                    side_effect=fake_read_records,
                ),
                mock.patch(
                    "trading_ai.evaluation.trading_model_benchmark._evaluate_candidate",
                    side_effect=fake_evaluate,
                ),
            ):
                exit_code = main(
                    [
                        "trading-model-benchmark",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2024-01-02",
                        "--to",
                        "2026-06-18",
                        "--as-of-date",
                        "2026-06-18",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--ai-features",
                        str(ai_features),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-18"
            ranking = json.loads((run_dir / "ranking.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)
        self.assertTrue(any(call["candidate"]["candidate_id"] == "logreg_ai_features" for call in calls))
        ai_call = next(call for call in calls if call["candidate"]["candidate_id"] == "logreg_ai_features")
        merged_rows = ai_call["feature_records"]
        self.assertTrue(any(row.get("ai_sentiment_1d") == 0.42 for row in merged_rows))
        self.assertEqual(ranking["feature_sources"]["ai_features"][0]["path"], str(ai_features))
        self.assertRegex(ranking["feature_sources"]["ai_features"][0]["dataset_hash"], r"^[0-9a-f]{64}$")
        self.assertFalse(ranking["safety"]["orders_submitted"])

    def test_evaluate_candidate_backtests_only_held_out_records(self) -> None:
        records = benchmark_feature_records(days=12)
        captured: dict[str, object] = {}

        def fake_run_signal_policy_backtest(feature_records, model, **kwargs):
            captured["timestamps"] = [str(row["timestamp"]) for row in feature_records]
            captured["kwargs"] = kwargs
            return fake_backtest_result()

        with mock.patch(
            "trading_ai.evaluation.trading_model_benchmark.run_signal_policy_backtest",
            side_effect=fake_run_signal_policy_backtest,
        ):
            row = _evaluate_candidate(
                {
                    "candidate_id": "logreg_current_features",
                    "family": "logistic",
                    "model_type": "logistic-baseline",
                    "baseline_role": "challenger",
                    "features": ["momentum_20"],
                },
                feature_records=records,
                signal_model=Path("unused.json"),
                threshold=0.5,
                min_signal_margin=0.05,
                max_buy_signals=3,
                backtest_config=BacktestConfig(),
                embargo=1,
            )

        self.assertEqual(row["status"], "OK")
        self.assertEqual(captured["timestamps"][0], records[8]["timestamp"])
        self.assertNotIn(records[0]["timestamp"], captured["timestamps"])

    def test_candidate_spec_prefers_loaded_feature_names_for_champion(self) -> None:
        spec = _candidate_spec(
            {
                "candidate_id": "champion_latest_model",
                "family": "champion",
                "model_type": "logistic-baseline",
                "features": [],
                "feature_names": ["momentum_20"],
                "rank": 1,
                "metrics": {"sharpe": 1.0},
            },
            metadata={"dataset_hash": "dataset", "source_sha256": "source"},
            as_of_date="2026-06-18",
            embargo=1,
        )

        self.assertEqual(spec["feature_names"], ["momentum_20"])

    def test_load_costs_uses_backtest_defaults_when_costs_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            risk = write_trading_first_risk(Path(temp_dir) / "risk.yml")

            cost_bps, slippage_bps = _load_costs(risk)

        default = BacktestConfig()
        self.assertEqual(cost_bps, default.cost_bps)
        self.assertEqual(slippage_bps, default.slippage_bps)

    def test_trading_model_benchmark_writes_risk_adjusted_ranking_without_latest_model_mutation(self) -> None:
        records = directional_records(days=260)
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_trading_first_risk(root / "risk.yml")
            output_dir = root / "benchmark"

            with mock.patch("trading_ai.evaluation.trading_model_benchmark.read_records", return_value=records):
                exit_code = main(
                    [
                        "trading-model-benchmark",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2024-01-02",
                        "--to",
                        "2026-06-18",
                        "--as-of-date",
                        "2026-06-18",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-18"
            ranking = json.loads((run_dir / "ranking.json").read_text(encoding="utf-8"))
            candidate_spec = json.loads((run_dir / "candidate_spec.json").read_text(encoding="utf-8"))

        self.assertIn(exit_code, {0, 1})
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)
        self.assertEqual(ranking["objective"], "risk_adjusted_return")
        self.assertEqual(ranking["authority"]["llm_authority"], "none")
        self.assertFalse(ranking["safety"]["orders_submitted"])
        self.assertIn("champion_latest_model", [item["candidate_id"] for item in ranking["candidates"]])
        self.assertTrue({"sharpe", "calmar", "max_drawdown", "estimated_costs", "turnover"} <= set(ranking["rank_by"]))
        self.assertIn("preprocessing", candidate_spec)
        self.assertIn("training_config", candidate_spec)
        self.assertEqual(candidate_spec["preprocessing"]["type"], "none")
        self.assertIn("test_fraction", candidate_spec["training_config"])
        self.assertFalse(candidate_spec["safety"]["futures_forex_execution"])
        self.assertEqual(candidate_spec["safety"]["llm_order_authority"], "none")
        self.assertEqual(candidate_spec["safety"]["paper_only"], True)
        self.assertFalse(candidate_spec["authority"]["mutates_latest_model"])


def benchmark_feature_records(*, days: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(days):
        rows.append(
            {
                "timestamp": f"2026-01-{index + 1:02d}",
                "symbol": "SPY",
                "close": 100.0 + index,
                "momentum_20": float(index % 3) - 1.0,
            }
        )
    return rows


if __name__ == "__main__":
    unittest.main()
