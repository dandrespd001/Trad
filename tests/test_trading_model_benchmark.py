import json
import math
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
    _available_features,
    _candidate_spec,
    _evaluate_candidate,
    _load_costs,
    _missing_required_features,
    _score,
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

    def test_evaluate_candidate_adds_sortino_and_directional_bias_to_metrics(self) -> None:
        """Sprint G2: the row must carry finite Sortino (downside-deviation
        convention) and directional_bias on the same daily_returns series
        used for sharpe, so the report can expand visibility without moving
        the ranking."""

        records = benchmark_feature_records(days=12)

        def fake_run_signal_policy_backtest(feature_records, model, **kwargs):
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

        metrics = row["metrics"]
        self.assertIn("sortino", metrics)
        self.assertIn("directional_bias", metrics)
        # Both must be finite floats; the fixture's daily_returns of (0.01, 0.002)
        # has no downside, so sortino lands on the documented 0.0 (fail-closed).
        self.assertTrue(math.isfinite(metrics["sortino"]))
        self.assertTrue(math.isfinite(metrics["directional_bias"]))
        # directional_bias for [0.01, 0.002] is (2 - 0) / 2 = 1.0.
        self.assertEqual(metrics["directional_bias"], 1.0)

    def test_evaluate_candidate_score_unchanged_by_new_metrics(self) -> None:
        """Non-regression: adding sortino + directional_bias must NOT change
        the candidate's score, which depends only on sharpe/calmar/max_drawdown
        /estimated_costs/turnover. The score must equal the pre-Sprint-G2
        reference value for the existing fake_backtest_result fixture."""

        records = benchmark_feature_records(days=12)
        # fake_backtest_result()'s metrics: sharpe=1.25, cagr=0.13, max_drawdown=0.10,
        # estimated_costs=0.03, turnover=150.0 -> calmar = cagr / max_drawdown = 1.3
        # (calmar is computed inside _evaluate_candidate from cagr+max_drawdown).
        pre_sprint_metrics = {
            "sharpe": 1.25,
            "calmar": 0.13 / 0.10,
            "max_drawdown": 0.10,
            "estimated_costs": 0.03,
            "turnover": 150.0,
        }
        # Score formula in trading_model_benchmark._score (pre-Sprint-G2 and
        # post-Sprint-G2 are identical -- new metrics aren't part of the sum).
        expected_score = (
            1.25
            + 0.5 * (0.13 / 0.10)
            - 0.10
            - 0.03
            - 0.001 * 150.0
        )

        def fake_run_signal_policy_backtest(feature_records, model, **kwargs):
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
        # Pre-Sprint-G2 reference score for the same fixture (must match).
        self.assertAlmostEqual(row["score"], expected_score, places=12)
        # Sanity: the score computed by the module's _score() on the actual
        # row metrics (which now also include sortino + directional_bias)
        # must match the reference exactly, proving the new metrics are
        # NOT feeding the score.
        self.assertAlmostEqual(
            row["score"],
            _score({**pre_sprint_metrics, "sortino": 999.0, "directional_bias": 999.0}),
            places=12,
        )

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


class MissingRequiredFeaturesSkippedTests(unittest.TestCase):
    """Sprint E3: a candidate that declares required feature columns must be
    reported as SKIPPED (not ERROR) when the dataset lacks those columns --
    same family as the optional ML dependency_missing SKIPPED state. Genuine
    training/prediction exceptions must still surface as ERROR.
    """

    def test_missing_required_features_emits_skipped_with_explicit_reason(self) -> None:
        raw_records = directional_records(days=80)
        # Sanity: the directional_records fixture does not produce extended
        # indicators, so the candidate's required list is fully unmet.
        available = set(_available_features(raw_records))
        self.assertNotIn("rsi_14", available)
        self.assertNotIn("macd_hist", available)
        self.assertNotIn("bb_pct_b", available)

        row = _evaluate_candidate(
            {
                "candidate_id": "logreg_extended_technical",
                "family": "logistic",
                "model_type": "logistic-baseline",
                "baseline_role": "challenger",
                "features": [],
            },
            feature_records=raw_records,
            signal_model=Path("unused.json"),
            threshold=0.5,
            min_signal_margin=0.05,
            max_buy_signals=3,
            backtest_config=BacktestConfig(),
            embargo=1,
        )

        self.assertEqual(row["status"], "SKIPPED")
        self.assertFalse(row["dependency_missing"])
        self.assertEqual(row["score"], float("-inf"))
        self.assertEqual(row["metrics"], {})
        reasons = " ".join(row["reason_codes"])
        self.assertIn("missing_required_features", reasons)
        self.assertIn("rsi_14", reasons)
        # The full required set must be surfaced in the reason so the
        # operator can see exactly which columns are absent.
        self.assertIn("macd_hist", reasons)
        self.assertIn("bb_pct_b", reasons)

    def test_present_required_features_are_not_skipped_for_missing_reason(self) -> None:
        """When the dataset carries the extended indicators the candidate must
        NOT carry a missing_required_features reason, even if training later
        fails for an unrelated reason (e.g. the stubbed exception below)."""

        enriched_records = _enriched_records_with_extended(days=80)

        # Force the inner training step to raise so we can assert the row is
        # NOT routed through the missing_required_features branch.
        with mock.patch(
            "trading_ai.evaluation.trading_model_benchmark.train_logistic_baseline",
            side_effect=RuntimeError("forced training failure for test"),
        ):
            row = _evaluate_candidate(
                {
                    "candidate_id": "logreg_extended_technical",
                    "family": "logistic",
                    "model_type": "logistic-baseline",
                    "baseline_role": "challenger",
                    "features": ["rsi_14", "macd_hist", "bb_pct_b"],
                },
                feature_records=enriched_records,
                signal_model=Path("unused.json"),
                threshold=0.5,
                min_signal_margin=0.05,
                max_buy_signals=3,
                backtest_config=BacktestConfig(),
                embargo=1,
            )

        reasons = " ".join(row["reason_codes"])
        self.assertNotIn("missing_required_features", reasons)

    def test_real_training_error_remains_error_when_required_features_present(self) -> None:
        """A genuine exception during training/prediction with all required
        features present must still be classified as ERROR -- the
        missing_required_features path is NOT a blanket catcher."""

        enriched_records = _enriched_records_with_extended(days=80)

        with mock.patch(
            "trading_ai.evaluation.trading_model_benchmark.train_logistic_baseline",
            side_effect=RuntimeError("real training failure"),
        ):
            row = _evaluate_candidate(
                {
                    "candidate_id": "logreg_extended_technical",
                    "family": "logistic",
                    "model_type": "logistic-baseline",
                    "baseline_role": "challenger",
                    "features": ["rsi_14", "macd_hist", "bb_pct_b"],
                },
                feature_records=enriched_records,
                signal_model=Path("unused.json"),
                threshold=0.5,
                min_signal_margin=0.05,
                max_buy_signals=3,
                backtest_config=BacktestConfig(),
                embargo=1,
            )

        self.assertEqual(row["status"], "ERROR")
        self.assertFalse(row["dependency_missing"])
        self.assertTrue(
            any(code.startswith("evaluation_error:") for code in row["reason_codes"]),
            f"expected evaluation_error:* reason, got {row['reason_codes']!r}",
        )

    def test_missing_required_features_helper_lists_only_declared_requirements(self) -> None:
        """The helper must surface only the declared-requirements map; an
        unknown candidate_id (no declared requirements) returns an empty
        tuple so the caller falls through to the legacy try/except path."""

        raw_records = directional_records(days=40)
        # Declared candidate with missing columns -> the required set is reported.
        extended_missing = _missing_required_features(
            {"candidate_id": "logreg_extended_technical", "features": []},
            raw_records,
        )
        self.assertEqual(extended_missing, ("rsi_14", "macd_hist", "bb_pct_b"))

        # Undeclared candidate -> empty tuple regardless of dataset.
        undeclared = _missing_required_features(
            {"candidate_id": "logreg_current_features", "features": ["momentum_20"]},
            raw_records,
        )
        self.assertEqual(undeclared, ())

        # Declared candidate with all columns present -> empty tuple.
        enriched = _enriched_records_with_extended(days=40)
        satisfied = _missing_required_features(
            {"candidate_id": "logreg_extended_technical", "features": []},
            enriched,
        )
        self.assertEqual(satisfied, ())


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


def _enriched_records_with_extended(*, days: int) -> list[dict[str, object]]:
    """Mirror ``directional_records`` shape and append the extended
    technical-indicator columns (``rsi_14``/``macd_hist``/``bb_pct_b``) so a
    candidate that declares them as required sees them as available."""

    base = directional_records(days=days)
    enriched: list[dict[str, object]] = []
    for index, row in enumerate(base):
        copy = dict(row)
        copy["rsi_14"] = 50.0 + (index % 7)
        copy["macd_hist"] = float(index % 5) - 2.0
        copy["bb_pct_b"] = 0.1 * ((index % 11) - 5)
        enriched.append(copy)
    return enriched


if __name__ == "__main__":
    unittest.main()
