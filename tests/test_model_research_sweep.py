import json
import tempfile
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from trading_ai.cli import main
from trading_ai.data.manifest import build_dataset_manifest


def write_universe(path: Path, symbols: tuple[str, ...]) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            universe:
              symbols: [{", ".join(symbols)}]
            """
        ),
        encoding="utf-8",
    )
    return path


def write_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              live_trading_allowed: false
            """
        ),
        encoding="utf-8",
    )
    return path


def directional_records(*, days: int = 220) -> list[dict[str, object]]:
    return _records_from_returns(_block_returns(days, block_size=11))


def one_way_records(*, days: int = 180) -> list[dict[str, object]]:
    return _records_from_returns([0.006 for _ in range(days)])


def _block_returns(days: int, *, block_size: int) -> list[float]:
    values: list[float] = []
    sign = 1.0
    while len(values) < days:
        values.extend([0.012 * sign for _ in range(block_size)])
        sign *= -1.0
    return values[:days]


def _records_from_returns(returns: list[float]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current = date(2024, 1, 2)
    close = 100.0
    for index, daily_return in enumerate(returns):
        while current.weekday() >= 5:
            current += timedelta(days=1)
        close = max(1.0, close * (1.0 + daily_return))
        rows.append(
            {
                "timestamp": current.isoformat(),
                "symbol": "SPY",
                "open": round(close * 0.998, 4),
                "high": round(close * 1.006, 4),
                "low": round(close * 0.994, 4),
                "close": round(close, 4),
                "volume": 1_000_000 + index * 250,
            }
        )
        current += timedelta(days=1)
    return rows


def write_approved_package(root: Path, *, records: list[dict[str, object]]) -> Path:
    approved_dir = root / "approved" / "core_etfs" / "1d"
    approved_dir.mkdir(parents=True)
    dataset_path = approved_dir / "ohlcv.parquet"
    manifest_path = approved_dir / "manifest.json"
    catalog_path = approved_dir / "catalog_entry.json"
    dataset_path.write_bytes(b"PAR1 fake approved parquet for model research tests")
    manifest = build_dataset_manifest(records, source=str(dataset_path))
    manifest.update(
        {
            "dataset_id": "core_etfs",
            "frequency": "1d",
            "source_sha256": "a" * 64,
            "provider": "manual_csv",
            "provider_kind": "manual",
            "license_note": "approved local fixture",
            "as_of_date": "2026-06-18",
        }
    )
    catalog_entry = {
        "schema_version": 1,
        "dataset_id": "core_etfs",
        "frequency": "1d",
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "dataset_hash": manifest["dataset_hash"],
        "symbols": manifest["symbols"],
        "row_count": manifest["row_count"],
        "start": manifest["start"],
        "end": manifest["end"],
        "as_of_date": "2026-06-18",
        "provider": "manual_csv",
        "provider_kind": "manual",
        "network_allowed": False,
        "license_note": "approved local fixture",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog_entry, indent=2, sort_keys=True), encoding="utf-8")
    return approved_dir


class ModelResearchSweepTests(unittest.TestCase):
    def test_model_research_sweep_writes_ranked_candidates_without_broker(self) -> None:
        records = directional_records()
        latest_model_before = Path("models/latest_model.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            output_dir = root / "research"

            with mock.patch("trading_ai.evaluation.model_research.read_records", return_value=records), mock.patch(
                "trading_ai.cli.build_alpaca_paper_client",
                side_effect=AssertionError("alpaca client should not be built"),
            ):
                exit_code = main(
                    [
                        "model-research-sweep",
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
            report = json.loads((run_dir / "sweep_report.json").read_text(encoding="utf-8"))
            candidate_specs = json.loads((run_dir / "candidate_specs.json").read_text(encoding="utf-8"))
            best_spec = json.loads((run_dir / "best_candidate_spec.json").read_text(encoding="utf-8"))
            deployment_model = json.loads((run_dir / "deployment_model.json").read_text(encoding="utf-8"))
            markdown = (run_dir / "sweep_report.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "CANDIDATE_READY")
        self.assertGreaterEqual(len(candidate_specs["candidates"]), 2)
        self.assertEqual(candidate_specs["candidates"][0]["candidate_id"], best_spec["candidate_id"])
        self.assertEqual(best_spec["model_type"], "logistic-baseline")
        self.assertEqual(best_spec["authority"]["mutates_latest_model"], False)
        self.assertEqual(best_spec["authority"]["orders_submitted"], False)
        self.assertEqual(best_spec["authority"]["broker_client_built"], False)
        self.assertEqual(best_spec["authority"]["credentials_read"], False)
        self.assertEqual(best_spec["safety"]["paper_only"], True)
        self.assertEqual(deployment_model["feature_names"], best_spec["feature_names"])
        self.assertIn("CANDIDATE_READY", markdown)
        self.assertEqual(Path("models/latest_model.json").read_text(encoding="utf-8"), latest_model_before)

    def test_model_research_sweep_marks_no_candidate_when_lift_is_not_robust(self) -> None:
        records = one_way_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            output_dir = root / "research"

            with mock.patch("trading_ai.evaluation.model_research.read_records", return_value=records):
                exit_code = main(
                    [
                        "model-research-sweep",
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
            report = json.loads((run_dir / "sweep_report.json").read_text(encoding="utf-8"))
            candidate_specs = json.loads((run_dir / "candidate_specs.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "NO_CANDIDATE_READY")
        self.assertFalse(report["ready_for_paper_demo"])
        self.assertGreaterEqual(len(candidate_specs["candidates"]), 1)
        self.assertFalse((run_dir / "best_candidate_spec.json").exists())
        self.assertFalse((run_dir / "deployment_model.json").exists())

    def test_model_research_sweep_filters_records_to_requested_window(self) -> None:
        records = directional_records(days=90)
        captured_records: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            output_dir = root / "research"

            def capture_build_features(
                input_records: list[dict[str, object]],
                config: object,
            ) -> list[dict[str, object]]:
                del config
                captured_records.extend(input_records)
                return []

            with mock.patch("trading_ai.evaluation.model_research.read_records", return_value=records), mock.patch(
                "trading_ai.evaluation.model_research.build_features",
                side_effect=capture_build_features,
            ), mock.patch("trading_ai.evaluation.model_research._evaluate_candidate_specs", return_value=[]):
                exit_code = main(
                    [
                        "model-research-sweep",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2024-02-01",
                        "--to",
                        "2024-02-29",
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
            report = json.loads((run_dir / "sweep_report.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertTrue(captured_records)
        self.assertEqual(
            [row["timestamp"] for row in captured_records],
            [row["timestamp"] for row in records if "2024-02-01" <= str(row["timestamp"]) <= "2024-02-29"],
        )
        self.assertEqual(report["approved_dataset"]["window_start"], "2024-02-01")
        self.assertEqual(report["approved_dataset"]["window_end"], "2024-02-29")
        self.assertEqual(report["approved_dataset"]["window_row_count"], len(captured_records))

    def test_standardized_logistic_exports_raw_feature_coefficients(self) -> None:
        from trading_ai.evaluation.model_research import CandidateTrainingSpec, train_candidate_model

        feature_records = [
            {"timestamp": "2024-01-01", "symbol": "SPY", "close": 100.0, "return_1d": -2.0},
            {"timestamp": "2024-01-02", "symbol": "SPY", "close": 99.0, "return_1d": -1.0},
            {"timestamp": "2024-01-03", "symbol": "SPY", "close": 101.0, "return_1d": 1.0},
            {"timestamp": "2024-01-04", "symbol": "SPY", "close": 103.0, "return_1d": 2.0},
            {"timestamp": "2024-01-05", "symbol": "SPY", "close": 104.0, "return_1d": 1.5},
        ]
        spec = CandidateTrainingSpec(
            candidate_id="standardized-return",
            feature_names=("return_1d",),
            preprocessing={"type": "standardize"},
            training_config={"learning_rate": 0.3, "epochs": 80, "l2": 0.0, "test_fraction": 0.4},
        )

        result = train_candidate_model(feature_records=feature_records, spec=spec)
        model = result.model

        self.assertEqual(model.feature_names, ("return_1d",))
        self.assertEqual(result.preprocessing["type"], "standardize")
        self.assertNotEqual(model.coefficients[0], 0.0)
        self.assertAlmostEqual(
            model.predict_probability((1.0,)),
            result.standardized_model.predict_probability(result.transform_raw_features((1.0,))),
            places=10,
        )


if __name__ == "__main__":
    unittest.main()
