import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_model_research_sweep import directional_records
from trading_ai.cli import build_parser, main
from trading_ai.data.io import write_records
from trading_ai.evaluation.indicator_activation import (
    EXTENDED_FEATURE_CONFIG_FIELDS,
    RECOMMENDATION_BASELINE,
    RECOMMENDATION_EXTENDED,
    STATUS_BLOCKED,
    STATUS_OK,
    _extended_config_mismatch_reason,
    compute_activation_hash,
    feature_config_from_activation,
    load_indicator_activation,
    run_indicator_activation_report,
)
from trading_ai.execution.paper_common import read_json_artifact
from trading_ai.features.engineering import FeatureConfig

_STUB_TARGET = "trading_ai.evaluation.indicator_activation._best_candidate_score"


def _write_dataset(root: Path, *, days: int = 60) -> Path:
    dataset = root / "dataset.csv"
    write_records(directional_records(days=days), dataset)
    return dataset


class IndicatorActivationRecommendationTests(unittest.TestCase):
    """Unit tests for the recommendation logic with the benchmark stubbed out.

    Running the real trading-model benchmark on every branch (margin
    cleared / not cleared / tied / erroring) would mean training several
    candidate models per case just to exercise pure comparison arithmetic.
    Stubbing ``_best_candidate_score`` isolates that arithmetic; a separate
    small integration test below runs the real benchmark end-to-end.
    """

    def test_recommends_extended_when_margin_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                    min_relative_margin=0.05,
                )

        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_EXTENDED)
        self.assertEqual(result.payload["feature_config"], dict(EXTENDED_FEATURE_CONFIG_FIELDS))
        self.assertEqual(result.payload["baseline_score"], 1.0)
        self.assertEqual(result.payload["extended_score"], 1.2)
        self.assertAlmostEqual(result.payload["relative_margin_observed"], 0.2)

    def test_recommends_baseline_when_margin_is_not_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.02, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                    min_relative_margin=0.05,
                )

        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_BASELINE)
        self.assertEqual(result.payload["feature_config"], {})

    def test_recommends_baseline_on_exact_tie(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(0.75, "baseline_id"), (0.75, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                    min_relative_margin=0.05,
                )

        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_BASELINE)

    def test_margin_boundary_is_inclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.05, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                    min_relative_margin=0.05,
                )

        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_EXTENDED)

    def test_missing_valid_candidate_on_one_side_falls_back_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (None, None)]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                )

        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_BASELINE)
        self.assertIn("extended_candidate_missing", result.payload["blockers"])

    def test_benchmark_error_blocks_and_falls_back_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "out"
            missing_dataset = root / "does_not_exist.csv"

            result = run_indicator_activation_report(
                as_of_date="2026-07-06",
                dataset=missing_dataset,
                output_dir=output_dir,
            )

        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_BASELINE)
        self.assertEqual(result.payload["feature_config"], {})
        self.assertTrue(any(blocker.startswith("benchmark_error:") for blocker in result.payload["blockers"]))

    def test_invalid_dataset_blocks_and_falls_back_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.csv"
            # Missing the required "volume" column.
            write_records(
                [
                    {
                        "timestamp": "2026-01-01",
                        "symbol": "SPY",
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                    }
                ],
                dataset,
            )
            output_dir = root / "out"

            result = run_indicator_activation_report(
                as_of_date="2026-07-06",
                dataset=dataset,
                output_dir=output_dir,
            )

        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(result.payload["recommendation"], RECOMMENDATION_BASELINE)
        self.assertTrue(any(blocker.startswith("dataset_invalid:") for blocker in result.payload["blockers"]))

    def test_artifact_hash_is_reverifiable_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                )

            on_disk = read_json_artifact(result.output_path)

        self.assertEqual(on_disk["artifact_hash"], compute_activation_hash(on_disk))
        # A change to the generated_at field alone must not change the hash.
        mutated = dict(on_disk)
        mutated["generated_at"] = "2099-01-01T00:00:00+00:00"
        self.assertEqual(compute_activation_hash(mutated), on_disk["artifact_hash"])
        # A change to any decision field must change the hash.
        tampered = dict(on_disk)
        tampered["recommendation"] = "extended" if on_disk["recommendation"] == "baseline" else "baseline"
        self.assertNotEqual(compute_activation_hash(tampered), on_disk["artifact_hash"])


class IndicatorActivationRealBenchmarkIntegrationTest(unittest.TestCase):
    """Small end-to-end run against the real trading-model benchmark (no stubs).

    Kept intentionally small (60 synthetic days, no optional ML deps
    installed in this environment) so it stays well under 5 seconds.
    """

    def test_real_benchmark_run_produces_a_well_formed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root, days=90)
            output_dir = root / "out"

            result = run_indicator_activation_report(
                as_of_date="2026-07-06",
                dataset=dataset,
                output_dir=output_dir,
            )

            self.assertIn(result.status, {STATUS_OK, STATUS_BLOCKED})
            self.assertIn(result.exit_code, {0, 1})
            self.assertIn(result.payload["recommendation"], {RECOMMENDATION_EXTENDED, RECOMMENDATION_BASELINE})
            self.assertFalse(result.payload["safety"]["orders_submitted"])
            self.assertFalse(result.payload["safety"]["mutates_latest_model"])
            self.assertTrue(result.output_path.exists())
            self.assertEqual(result.payload["artifact_hash"], compute_activation_hash(result.payload))


class LoadIndicatorActivationTests(unittest.TestCase):
    def test_missing_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            activation = load_indicator_activation(as_of_date="2026-07-06", output_dir=Path(temp_dir) / "nope")

        self.assertEqual(activation["recommendation"], RECOMMENDATION_BASELINE)
        self.assertEqual(activation["feature_config"], {})
        self.assertTrue(activation["fail_closed"])
        self.assertEqual(activation["reason"], "artifact_missing")

    def test_corrupt_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"
            run_dir = output_dir / "2026-07-06"
            run_dir.mkdir(parents=True)
            (run_dir / "activation.json").write_text("{not json", encoding="utf-8")

            activation = load_indicator_activation(as_of_date="2026-07-06", output_dir=output_dir)

        self.assertTrue(activation["fail_closed"])
        self.assertEqual(activation["reason"], "artifact_corrupt")

    def test_tampered_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                result = run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=output_dir,
                )

            payload = json.loads(result.output_path.read_text(encoding="utf-8"))
            payload["recommendation"] = "baseline"  # tamper without recomputing artifact_hash
            result.output_path.write_text(json.dumps(payload), encoding="utf-8")

            activation = load_indicator_activation(as_of_date="2026-07-06", output_dir=output_dir)

        self.assertTrue(activation["fail_closed"])
        self.assertEqual(activation["reason"], "artifact_hash_mismatch")

    def test_stale_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                run_indicator_activation_report(
                    as_of_date="2020-01-01",
                    dataset=dataset,
                    output_dir=output_dir,
                )

            activation = load_indicator_activation(
                as_of_date="2026-07-06", output_dir=output_dir, max_age_days=7
            )

        self.assertTrue(activation["fail_closed"])
        self.assertEqual(activation["reason"], "artifact_stale")

    def test_fresh_extended_artifact_loads_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root)
            output_dir = root / "out"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                run_indicator_activation_report(
                    as_of_date="2026-07-05",
                    dataset=dataset,
                    output_dir=output_dir,
                )

            activation = load_indicator_activation(
                as_of_date="2026-07-06", output_dir=output_dir, max_age_days=7
            )

        self.assertFalse(activation["fail_closed"])
        self.assertEqual(activation["recommendation"], RECOMMENDATION_EXTENDED)
        self.assertEqual(activation["feature_config"], dict(EXTENDED_FEATURE_CONFIG_FIELDS))


class FeatureConfigFromActivationTests(unittest.TestCase):
    def test_extended_canonical_config_yields_extended_feature_config(self) -> None:
        payload = {"recommendation": RECOMMENDATION_EXTENDED, "feature_config": dict(EXTENDED_FEATURE_CONFIG_FIELDS)}

        config = feature_config_from_activation(payload)

        self.assertEqual(config, FeatureConfig(**EXTENDED_FEATURE_CONFIG_FIELDS))

    def test_tampered_field_falls_back_to_baseline(self) -> None:
        tampered = dict(EXTENDED_FEATURE_CONFIG_FIELDS)
        tampered["rsi_window"] = 2
        payload = {"recommendation": RECOMMENDATION_EXTENDED, "feature_config": tampered}

        self.assertEqual(_extended_config_mismatch_reason(tampered), "tampered_feature_config")
        config = feature_config_from_activation(payload)

        self.assertEqual(config, FeatureConfig())

    def test_missing_field_falls_back_to_baseline(self) -> None:
        partial = dict(EXTENDED_FEATURE_CONFIG_FIELDS)
        del partial["bb_window"]
        payload = {"recommendation": RECOMMENDATION_EXTENDED, "feature_config": partial}

        config = feature_config_from_activation(payload)

        self.assertEqual(config, FeatureConfig())

    def test_baseline_recommendation_yields_baseline_feature_config(self) -> None:
        payload = {"recommendation": RECOMMENDATION_BASELINE, "feature_config": {}}

        config = feature_config_from_activation(payload)

        self.assertEqual(config, FeatureConfig())

    def test_missing_recommendation_key_fails_closed_to_baseline(self) -> None:
        self.assertEqual(feature_config_from_activation({}), FeatureConfig())


class BuildFeaturesConsumptionPointTests(unittest.TestCase):
    def test_without_flag_features_have_no_extended_indicator_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root, days=40)
            output = root / "features.csv"

            exit_code = main(["build-features", "--dataset", str(dataset), "--output", str(output)])

            rows = list(_read_csv(output))

        self.assertEqual(exit_code, 0)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("rsi_14", row)
            self.assertNotIn("macd_hist", row)
            self.assertNotIn("bb_pct_b", row)

    def test_with_flag_and_fresh_extended_artifact_features_include_extended_indicators(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root, days=40)
            activation_dir = root / "activation"
            output = root / "features.csv"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                run_indicator_activation_report(
                    as_of_date="2026-07-06",
                    dataset=dataset,
                    output_dir=activation_dir,
                )

            exit_code = main(
                [
                    "build-features",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output),
                    "--indicator-activation-dir",
                    str(activation_dir),
                    "--as-of-date",
                    "2026-07-06",
                ]
            )

            rows = list(_read_csv(output))
            manifest = json.loads((root / "features.indicator_activation.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(any(row.get("rsi_14") not in (None, "") for row in rows))
        self.assertTrue(any(row.get("macd_hist") not in (None, "") for row in rows))
        self.assertTrue(any(row.get("bb_pct_b") not in (None, "") for row in rows))
        self.assertEqual(manifest["recommendation_used"], RECOMMENDATION_EXTENDED)
        self.assertFalse(manifest["fail_closed"])

    def test_with_flag_and_stale_artifact_falls_back_to_baseline_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = _write_dataset(root, days=40)
            activation_dir = root / "activation"
            output = root / "features.csv"

            with mock.patch(_STUB_TARGET, side_effect=[(1.0, "baseline_id"), (1.2, "extended_id")]):
                run_indicator_activation_report(
                    as_of_date="2020-01-01",
                    dataset=dataset,
                    output_dir=activation_dir,
                )

            exit_code = main(
                [
                    "build-features",
                    "--dataset",
                    str(dataset),
                    "--output",
                    str(output),
                    "--indicator-activation-dir",
                    str(activation_dir),
                    "--as-of-date",
                    "2026-07-06",
                ]
            )

            rows = list(_read_csv(output))
            manifest = json.loads((root / "features.indicator_activation.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        for row in rows:
            self.assertNotIn("rsi_14", row)
        self.assertTrue(manifest["fail_closed"])
        self.assertEqual(manifest["reason"], "artifact_stale")
        self.assertEqual(manifest["recommendation_used"], RECOMMENDATION_BASELINE)


class IndicatorActivationCliParserTests(unittest.TestCase):
    def test_parser_registers_indicator_activation_without_submit_or_confirm_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "indicator-activation",
                "--as-of-date",
                "2026-07-06",
                "--dataset",
                "data.csv",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-07-06")
        self.assertEqual(args.dataset, "data.csv")

        subparser = next(
            action.choices["indicator-activation"]
            for action in parser._subparsers._group_actions  # noqa: SLF001
            if hasattr(action, "choices") and "indicator-activation" in action.choices
        )
        option_strings = {option for action in subparser._actions for option in action.option_strings}  # noqa: SLF001
        for forbidden in ("--confirm", "--submit", "--execute-live", "--confirm-real"):
            self.assertNotIn(forbidden, option_strings)


def _read_csv(path: Path) -> list[dict[str, str]]:
    import csv

    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
