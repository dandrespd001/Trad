import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import main
from trading_ai.evaluation.trading_model_benchmark import build_benchmark_candidates

from tests.test_model_research_sweep import (
    directional_records,
    write_approved_package,
    write_trading_first_risk,
    write_universe,
)


class TradingModelBenchmarkTests(unittest.TestCase):
    def test_candidate_plan_includes_champion_extended_and_optional_ml_dependency_states(self) -> None:
        candidates = build_benchmark_candidates(("momentum_20", "rsi_14", "macd_hist", "bb_pct_b"))
        ids = {candidate["candidate_id"]: candidate for candidate in candidates}

        self.assertIn("champion_latest_model", ids)
        self.assertIn("logreg_extended_technical", ids)
        self.assertIn("sklearn_random_forest", ids)
        self.assertIn("lightgbm_classifier", ids)
        self.assertIn("xgboost_classifier", ids)
        self.assertEqual(ids["champion_latest_model"]["baseline_role"], "champion")
        self.assertEqual(ids["logreg_extended_technical"]["features"], ["rsi_14", "macd_hist", "bb_pct_b"])

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


if __name__ == "__main__":
    unittest.main()
