import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.cli import build_parser, main

DATASET_HASH = "d" * 64
SOURCE_SHA256 = "e" * 64
REGISTRY_RUN_ID_TAG = "trading_ai.registry_run_id"


class MlflowRegistrySyncTests(unittest.TestCase):
    def test_parser_defaults_for_sync_registry_mlflow(self) -> None:
        args = build_parser().parse_args(["sync-registry-mlflow"])

        self.assertEqual(args.registry_dir, "reports/registry")
        self.assertEqual(args.tracking_uri, "reports/mlruns")
        self.assertEqual(args.experiment_name, "approved-data-evaluations")
        self.assertIsNone(args.run_id)

    def test_parser_defaults_for_register_registry_mlflow_model(self) -> None:
        args = build_parser().parse_args(["register-registry-mlflow-model", "--run-id", "registry-run-1"])

        self.assertEqual(args.run_id, "registry-run-1")
        self.assertEqual(args.registry_dir, "reports/registry")
        self.assertEqual(args.tracking_uri, "reports/mlruns")
        self.assertEqual(args.experiment_name, "approved-data-evaluations")
        self.assertEqual(args.registered_model_name, "approved-data-logistic-baseline")
        self.assertEqual(args.alias, "paper-candidate")

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(["register-registry-mlflow-model"])

    def test_parser_defaults_for_review_mlflow_paper_candidate(self) -> None:
        args = build_parser().parse_args(["review-mlflow-paper-candidate"])

        self.assertEqual(args.registry_dir, "reports/registry")
        self.assertEqual(args.tracking_uri, "reports/mlruns")
        self.assertEqual(args.registered_model_name, "approved-data-logistic-baseline")
        self.assertEqual(args.alias, "paper-candidate")
        self.assertEqual(args.features, "data/processed/features.csv")
        self.assertEqual(args.config, "configs/universe.yml")
        self.assertEqual(args.output, "reports/tmp/mlflow_paper_candidate_review/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/mlflow_paper_candidate_review/latest.md")

    def test_missing_mlflow_returns_two_without_breaking_cli_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            stderr = io.StringIO()

            with mock.patch.dict(sys.modules, {"mlflow": None}), contextlib.redirect_stderr(stderr):
                exit_code = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])

        self.assertEqual(exit_code, 2)
        self.assertIn("MLflow is not installed", stderr.getvalue())
        self.assertIn(".[monitoring]", stderr.getvalue())

    def test_sync_approved_run_logs_params_metrics_tags_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, metrics={"sortino": 0.77, "trade_count": 8})
            registry_dir = root / "registry"
            tracking_uri = root / "mlruns"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            state = FakeMlflowState()
            stdout = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                mock.patch(
                    "trading_ai.cli.build_alpaca_paper_client",
                    side_effect=AssertionError("alpaca client should not be built"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "sync-registry-mlflow",
                        "--registry-dir",
                        str(registry_dir),
                        "--tracking-uri",
                        str(tracking_uri),
                    ]
                )

            mlflow_run = one_run(state)

        self.assertEqual(exit_code, 0)
        self.assertIn("read=1 created=1 updated=0 skipped=0", stdout.getvalue())
        self.assertIn(f"tracking URI: {tracking_uri}", stdout.getvalue())
        self.assertEqual(state.client_tracking_uris, [str(tracking_uri)])
        self.assertEqual(state.experiments_by_name["approved-data-evaluations"].experiment_id, "1")
        self.assertEqual(mlflow_run.data.tags["trading_ai.source"], "local_registry")
        self.assertEqual(mlflow_run.data.tags["trading_ai.status"], "APPROVED")
        self.assertEqual(mlflow_run.data.tags["trading_ai.dataset_id"], "core_etfs")
        self.assertEqual(mlflow_run.data.tags["trading_ai.frequency"], "1d")
        self.assertEqual(mlflow_run.data.tags["trading_ai.as_of_date"], "2026-06-16")
        self.assertIn(REGISTRY_RUN_ID_TAG, mlflow_run.data.tags)
        self.assertEqual(mlflow_run.data.params["dataset_id"], "core_etfs")
        self.assertEqual(mlflow_run.data.params["frequency"], "1d")
        self.assertEqual(mlflow_run.data.params["as_of_date"], "2026-06-16")
        self.assertEqual(mlflow_run.data.params["dataset_hash"], DATASET_HASH)
        self.assertEqual(mlflow_run.data.params["source_sha256"], SOURCE_SHA256)
        self.assertEqual(mlflow_run.data.params["start"], "2025-01-01")
        self.assertEqual(mlflow_run.data.params["end"], "2026-06-16")
        self.assertEqual(mlflow_run.data.params["row_count"], "123")
        self.assertEqual(mlflow_run.data.params["symbols"], '["SPY"]')
        self.assertEqual(mlflow_run.data.params["eligible_for_paper_challenger"], "true")
        self.assertEqual(mlflow_run.data.metrics["accuracy"], 0.61)
        self.assertEqual(mlflow_run.data.metrics["sharpe"], 1.25)
        self.assertEqual(mlflow_run.data.metrics["cagr"], 0.14)
        self.assertEqual(mlflow_run.data.metrics["max_drawdown"], -0.08)
        self.assertEqual(mlflow_run.data.metrics["sortino"], 0.77)
        self.assertEqual(mlflow_run.data.metrics["trade_count"], 8.0)
        self.assertEqual({artifact.path for artifact in mlflow_run.artifacts}, {"evaluation"})
        self.assertIn("evaluation_summary.json", {artifact.name for artifact in mlflow_run.artifacts})
        self.assertIn("data_quality.json", {artifact.name for artifact in mlflow_run.artifacts})

    def test_rejected_and_blocked_sync_without_required_model_metrics(self) -> None:
        cases = (
            ("REJECTED", False, True),
            ("BLOCKED", False, False),
        )
        for status, eligible, extra_artifacts in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                evaluation_dir = write_evaluation_package(
                    root,
                    status=status,
                    eligible=eligible,
                    extra_artifacts=extra_artifacts,
                )
                registry_dir = root / "registry"
                self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
                state = FakeMlflowState()

                with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                    exit_code = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])

                mlflow_run = one_run(state)
                self.assertEqual(exit_code, 0)
                self.assertEqual(mlflow_run.data.tags["trading_ai.status"], status)
                if status == "BLOCKED":
                    self.assertEqual(mlflow_run.data.metrics, {})

    def test_double_sync_reuses_run_by_registry_run_id_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            state = FakeMlflowState()
            first_stdout = io.StringIO()
            second_stdout = io.StringIO()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                with contextlib.redirect_stdout(first_stdout):
                    first_exit = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])
                with contextlib.redirect_stdout(second_stdout):
                    second_exit = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(len(state.runs), 1)
        self.assertIn("read=1 created=1 updated=0 skipped=0", first_stdout.getvalue())
        self.assertIn("read=1 created=0 updated=1 skipped=0", second_stdout.getvalue())

    def test_run_id_syncs_only_requested_registry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry_dir = root / "registry"
            first_dir = write_evaluation_package(root, as_of_date="2026-06-16")
            second_dir = write_evaluation_package(root, as_of_date="2026-06-17")
            self.assertEqual(register_package(first_dir, registry_dir), 0)
            self.assertEqual(register_package(second_dir, registry_dir), 0)
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            selected_run_id = index["runs"][1]["run_id"]
            skipped_run_id = index["runs"][0]["run_id"]
            state = FakeMlflowState()
            stdout = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "sync-registry-mlflow",
                        "--registry-dir",
                        str(registry_dir),
                        "--run-id",
                        selected_run_id,
                    ]
                )

            mlflow_run = one_run(state)

        self.assertEqual(exit_code, 0)
        self.assertIn("read=1 created=1 updated=0 skipped=1", stdout.getvalue())
        self.assertEqual(mlflow_run.data.tags[REGISTRY_RUN_ID_TAG], selected_run_id)
        self.assertNotEqual(mlflow_run.data.tags[REGISTRY_RUN_ID_TAG], skipped_run_id)

    def test_missing_index_invalid_run_json_or_missing_artifact_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(["sync-registry-mlflow", "--registry-dir", str(Path(temp_dir) / "missing")])
            self.assertEqual(exit_code, 2)
            self.assertIn("registry index not found", stderr.getvalue())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
            Path(index["runs"][0]["run_path"]).write_text("{bad json", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])

            self.assertEqual(exit_code, 2)
            self.assertIn("invalid registry run JSON", stderr.getvalue())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            (evaluation_dir / "data_quality.json").unlink()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(["sync-registry-mlflow", "--registry-dir", str(registry_dir)])

            self.assertEqual(exit_code, 2)
            self.assertIn("declared artifact not found", stderr.getvalue())

    def test_register_registry_mlflow_model_missing_mlflow_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            stderr = io.StringIO()

            with mock.patch.dict(sys.modules, {"mlflow": None}), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "register-registry-mlflow-model",
                        "--registry-dir",
                        str(registry_dir),
                        "--run-id",
                        registry_run_id,
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("MLflow is not installed", stderr.getvalue())

    def test_register_registry_mlflow_model_creates_pyfunc_version_tags_and_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            tracking_uri = root / "mlruns"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()
            stdout = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "register-registry-mlflow-model",
                        "--registry-dir",
                        str(registry_dir),
                        "--tracking-uri",
                        str(tracking_uri),
                        "--run-id",
                        registry_run_id,
                    ]
                )

            mlflow_run = one_run(state)
            version = one_model_version(state, "approved-data-logistic-baseline")
            logged_model = state.logged_models[0]
            predictions = logged_model.python_model.predict(None, [{"momentum_2": 0.75}])

        self.assertEqual(exit_code, 0)
        self.assertIn("version=1 alias=paper-candidate created", stdout.getvalue())
        self.assertEqual(state.client_tracking_uris, [str(tracking_uri), str(tracking_uri)])
        self.assertEqual(mlflow_run.data.tags[REGISTRY_RUN_ID_TAG], registry_run_id)
        self.assertEqual(logged_model.run_id, mlflow_run.info.run_id)
        self.assertEqual(logged_model.kwargs["name"], "model")
        self.assertEqual(logged_model.model_uri, f"runs:/{mlflow_run.info.run_id}/model")
        self.assertEqual(version.source, logged_model.model_uri)
        self.assertEqual(version.run_id, mlflow_run.info.run_id)
        self.assertEqual(version.tags[REGISTRY_RUN_ID_TAG], registry_run_id)
        self.assertEqual(version.tags["trading_ai.source"], "local_registry")
        self.assertEqual(version.tags["trading_ai.status"], "APPROVED")
        self.assertEqual(version.tags["trading_ai.dataset_id"], "core_etfs")
        self.assertEqual(version.tags["trading_ai.frequency"], "1d")
        self.assertEqual(version.tags["trading_ai.as_of_date"], "2026-06-16")
        self.assertEqual(version.tags["trading_ai.eligible_for_paper_challenger"], "true")
        self.assertEqual(state.aliases[("approved-data-logistic-baseline", "paper-candidate")], "1")
        self.assertEqual(len(predictions), 1)
        self.assertGreater(predictions[0]["probability"], 0.5)
        self.assertEqual(predictions[0]["prediction"], 1)

    def test_register_registry_mlflow_model_reuses_existing_version_by_registry_run_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()
            first_stdout = io.StringIO()
            second_stdout = io.StringIO()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                with contextlib.redirect_stdout(first_stdout):
                    first_exit = main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    )
                with contextlib.redirect_stdout(second_stdout):
                    second_exit = main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    )

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(len(state.model_versions_by_name["approved-data-logistic-baseline"]), 1)
        self.assertEqual(len(state.logged_models), 1)
        self.assertIn("created", first_stdout.getvalue())
        self.assertIn("reused", second_stdout.getvalue())

    def test_register_registry_mlflow_model_rejects_non_paper_candidates(self) -> None:
        cases = (
            ("REJECTED", False, "not APPROVED"),
            ("BLOCKED", False, "not APPROVED"),
            ("APPROVED", False, "not eligible"),
        )
        for status, eligible, expected_error in cases:
            with self.subTest(status=status, eligible=eligible), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                evaluation_dir = write_evaluation_package(
                    root,
                    status=status,
                    eligible=eligible,
                    include_model_run=True,
                )
                registry_dir = root / "registry"
                self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
                registry_run_id = first_registry_run_id(registry_dir)
                state = FakeMlflowState()
                stderr = io.StringIO()

                with (
                    mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    )

                self.assertEqual(exit_code, 2)
                self.assertIn(expected_error, stderr.getvalue())
                self.assertEqual(state.model_versions_by_name, {})

    def test_register_registry_mlflow_model_rejects_missing_invalid_or_unsupported_model_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "register-registry-mlflow-model",
                        "--registry-dir",
                        str(registry_dir),
                        "--run-id",
                        registry_run_id,
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("missing model_run artifact", stderr.getvalue())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            (evaluation_dir / "model_run.json").write_text("{bad json", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "register-registry-mlflow-model",
                        "--registry-dir",
                        str(registry_dir),
                        "--run-id",
                        registry_run_id,
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("invalid model run JSON", stderr.getvalue())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True, model_type="random-forest")
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "register-registry-mlflow-model",
                        "--registry-dir",
                        str(registry_dir),
                        "--run-id",
                        registry_run_id,
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("unsupported model_type", stderr.getvalue())

    def test_review_mlflow_paper_candidate_missing_mlflow_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            stderr = io.StringIO()

            with mock.patch.dict(sys.modules, {"mlflow": None}), contextlib.redirect_stderr(stderr):
                exit_code = main(["review-mlflow-paper-candidate", "--registry-dir", str(registry_dir)])

        self.assertEqual(exit_code, 2)
        self.assertIn("MLflow is not installed", stderr.getvalue())

    def test_review_mlflow_paper_candidate_valid_alias_writes_reports_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            tracking_uri = root / "mlruns"
            features = write_features_file(root)
            output = root / "review.json"
            markdown_output = root / "review.md"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                self.assertEqual(
                    main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--tracking-uri",
                            str(tracking_uri),
                            "--run-id",
                            registry_run_id,
                        ]
                    ),
                    0,
                )
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "review-mlflow-paper-candidate",
                            "--registry-dir",
                            str(registry_dir),
                            "--tracking-uri",
                            str(tracking_uri),
                            "--features",
                            str(features),
                            "--config",
                            str(write_universe_config(root)),
                            "--output",
                            str(output),
                            "--markdown-output",
                            str(markdown_output),
                        ]
                    )

            report = json.loads(output.read_text(encoding="utf-8"))
            markdown = markdown_output.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("review passed", stdout.getvalue())
        self.assertEqual(report["status"], "PASSED")
        self.assertEqual(report["registered_model_name"], "approved-data-logistic-baseline")
        self.assertEqual(report["alias"], "paper-candidate")
        self.assertEqual(report["model_version"], "1")
        self.assertEqual(report["model_uri"], "models:/approved-data-logistic-baseline@paper-candidate")
        self.assertEqual(report["registry_run_id"], registry_run_id)
        self.assertEqual(report["local_registry_status"], "APPROVED")
        self.assertTrue(report["eligible_for_paper_challenger"])
        self.assertEqual(report["dataset_id"], "core_etfs")
        self.assertEqual(report["frequency"], "1d")
        self.assertEqual(report["as_of_date"], "2026-06-16")
        self.assertEqual(report["feature_names"], ["momentum_2"])
        self.assertEqual(len(report["prediction_sample"]), 1)
        self.assertGreater(report["prediction_sample"][0]["probability"], 0.5)
        self.assertEqual(report["prediction_sample"][0]["prediction"], 1)
        self.assertEqual(report["failures"], [])
        self.assertIn("# MLflow Paper Candidate Review", markdown)
        self.assertIn("Status: PASSED", markdown)

    def test_review_mlflow_paper_candidate_falls_back_to_version_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            features = write_features_file(root)
            output = root / "review.json"
            markdown_output = root / "review.md"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                self.assertEqual(
                    main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    ),
                    0,
                )
                state.alias_model_uri_supported = False
                exit_code = main(
                    [
                        "review-mlflow-paper-candidate",
                        "--registry-dir",
                        str(registry_dir),
                        "--features",
                        str(features),
                        "--config",
                        str(write_universe_config(root)),
                        "--output",
                        str(output),
                        "--markdown-output",
                        str(markdown_output),
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["model_uri"], "models:/approved-data-logistic-baseline/1")
        self.assertEqual(state.loaded_model_uris, ["models:/approved-data-logistic-baseline/1"])
        self.assertEqual(
            report["warnings"],
            ["alias model URI failed; loaded version URI instead: models:/approved-data-logistic-baseline/1"],
        )

    def test_review_mlflow_paper_candidate_missing_alias_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = FakeMlflowState()
            stderr = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "review-mlflow-paper-candidate",
                        "--registry-dir",
                        str(root / "registry"),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("MLflow model alias not found", stderr.getvalue())

    def test_review_mlflow_paper_candidate_missing_registry_run_tag_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            features = write_features_file(root)
            output = root / "review.json"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                self.assertEqual(
                    main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    ),
                    0,
                )
                one_model_version(state, "approved-data-logistic-baseline").tags.pop(REGISTRY_RUN_ID_TAG)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "review-mlflow-paper-candidate",
                            "--registry-dir",
                            str(registry_dir),
                            "--features",
                            str(features),
                            "--config",
                            str(write_universe_config(root)),
                            "--output",
                            str(output),
                        ]
                    )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("missing tag", stderr.getvalue())
        self.assertEqual(report["status"], "FAILED")
        self.assertIn(REGISTRY_RUN_ID_TAG, report["failures"][0])

    def test_review_mlflow_paper_candidate_missing_registry_run_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry_dir = root / "registry"
            registry_dir.mkdir()
            (registry_dir / "index.json").write_text('{"runs": []}', encoding="utf-8")
            state = FakeMlflowState()
            seed_model_version(state, registry_run_id="missing-run")
            stderr = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "review-mlflow-paper-candidate",
                        "--registry-dir",
                        str(registry_dir),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("registry run not found in index", stderr.getvalue())

    def test_review_mlflow_paper_candidate_local_rejected_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(
                root,
                status="REJECTED",
                eligible=False,
                include_model_run=True,
            )
            registry_dir = root / "registry"
            output = root / "review.json"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()
            seed_model_version(state, registry_run_id=registry_run_id)
            stderr = io.StringIO()

            with (
                mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "review-mlflow-paper-candidate",
                        "--registry-dir",
                        str(registry_dir),
                        "--features",
                        str(write_features_file(root)),
                        "--config",
                        str(write_universe_config(root)),
                        "--output",
                        str(output),
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("local registry run is not APPROVED", stderr.getvalue())
        self.assertEqual(report["local_registry_status"], "REJECTED")

    def test_review_mlflow_paper_candidate_invalid_pyfunc_output_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            output = root / "review.json"
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                self.assertEqual(
                    main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    ),
                    0,
                )
                state.logged_models[0].python_model = InvalidPredictionModel()
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "review-mlflow-paper-candidate",
                            "--registry-dir",
                            str(registry_dir),
                            "--features",
                            str(write_features_file(root)),
                            "--config",
                            str(write_universe_config(root)),
                            "--output",
                            str(output),
                        ]
                    )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("probability is outside", stderr.getvalue())
        self.assertEqual(report["status"], "FAILED")

    def test_review_mlflow_paper_candidate_missing_feature_column_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evaluation_dir = write_evaluation_package(root, include_model_run=True)
            registry_dir = root / "registry"
            output = root / "review.json"
            features = write_features_file(root, include_required_feature=False)
            self.assertEqual(register_package(evaluation_dir, registry_dir), 0)
            registry_run_id = first_registry_run_id(registry_dir)
            state = FakeMlflowState()

            with mock.patch.dict(sys.modules, {"mlflow": fake_mlflow_module(state)}):
                self.assertEqual(
                    main(
                        [
                            "register-registry-mlflow-model",
                            "--registry-dir",
                            str(registry_dir),
                            "--run-id",
                            registry_run_id,
                        ]
                    ),
                    0,
                )
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "review-mlflow-paper-candidate",
                            "--registry-dir",
                            str(registry_dir),
                            "--features",
                            str(features),
                            "--config",
                            str(write_universe_config(root)),
                            "--output",
                            str(output),
                        ]
                    )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("missing required column", stderr.getvalue())
        self.assertEqual(report["failures"], ["feature source missing required column(s): momentum_2"])


class FakeMlflowState:
    def __init__(self) -> None:
        self.experiments_by_name: dict[str, types.SimpleNamespace] = {}
        self.runs: dict[str, types.SimpleNamespace] = {}
        self.registered_models: dict[str, types.SimpleNamespace] = {}
        self.model_versions_by_name: dict[str, list[types.SimpleNamespace]] = {}
        self.aliases: dict[tuple[str, str], str] = {}
        self.logged_models: list[types.SimpleNamespace] = []
        self.next_experiment_id = 1
        self.next_run_id = 1
        self.client_tracking_uris: list[str | None] = []
        self.global_tracking_uri: str | None = None
        self.active_run_id: str | None = None
        self.alias_model_uri_supported = True
        self.loaded_model_uris: list[str] = []

    def set_tracking_uri(self, tracking_uri: str) -> None:
        self.global_tracking_uri = tracking_uri


class FakeMlflowClient:
    def __init__(self, state: FakeMlflowState, tracking_uri: str | None = None) -> None:
        self.state = state
        self.state.client_tracking_uris.append(tracking_uri)

    def get_experiment_by_name(self, name: str) -> types.SimpleNamespace | None:
        return self.state.experiments_by_name.get(name)

    def create_experiment(self, name: str) -> str:
        experiment_id = str(self.state.next_experiment_id)
        self.state.next_experiment_id += 1
        experiment = types.SimpleNamespace(experiment_id=experiment_id, name=name)
        self.state.experiments_by_name[name] = experiment
        return experiment_id

    def search_runs(
        self,
        *,
        experiment_ids: list[str],
        filter_string: str,
        max_results: int,
    ) -> list[types.SimpleNamespace]:
        del max_results
        match = re.search(r"= '((?:\\'|[^'])*)'", filter_string)
        target = match.group(1).replace("\\'", "'").replace("\\\\", "\\") if match else ""
        experiment_id_set = {str(experiment_id) for experiment_id in experiment_ids}
        return [
            run
            for run in self.state.runs.values()
            if str(run.info.experiment_id) in experiment_id_set and run.data.tags.get(REGISTRY_RUN_ID_TAG) == target
        ]

    def create_run(
        self,
        *,
        experiment_id: str,
        tags: dict[str, str],
        run_name: str | None = None,
    ) -> types.SimpleNamespace:
        run_id = f"mlflow-run-{self.state.next_run_id}"
        self.state.next_run_id += 1
        resolved_tags = dict(tags)
        if run_name is not None:
            resolved_tags["mlflow.runName"] = run_name
        run = types.SimpleNamespace(
            info=types.SimpleNamespace(run_id=run_id, experiment_id=str(experiment_id)),
            data=types.SimpleNamespace(params={}, metrics={}, tags=resolved_tags),
            artifacts=[],
        )
        self.state.runs[run_id] = run
        return run

    def set_tag(self, run_id: str, key: str, value: str) -> None:
        self.state.runs[run_id].data.tags[key] = value

    def log_param(self, run_id: str, key: str, value: str) -> None:
        self.state.runs[run_id].data.params[key] = value

    def log_metric(self, run_id: str, key: str, value: float) -> None:
        self.state.runs[run_id].data.metrics[key] = value

    def log_artifact(self, run_id: str, local_path: str, artifact_path: str) -> None:
        self.state.runs[run_id].artifacts.append(types.SimpleNamespace(path=artifact_path, name=Path(local_path).name))

    def get_registered_model(self, name: str) -> types.SimpleNamespace | None:
        return self.state.registered_models.get(name)

    def create_registered_model(self, name: str) -> types.SimpleNamespace:
        model = types.SimpleNamespace(name=name)
        self.state.registered_models[name] = model
        self.state.model_versions_by_name.setdefault(name, [])
        return model

    def search_model_versions(self, filter_string: str | None = None) -> list[types.SimpleNamespace]:
        match = re.search(r"name\s*=\s*'((?:\\'|[^'])*)'", filter_string or "")
        target = match.group(1).replace("\\'", "'").replace("\\\\", "\\") if match else None
        versions: list[types.SimpleNamespace] = []
        for name, model_versions in self.state.model_versions_by_name.items():
            if target is None or name == target:
                versions.extend(model_versions)
        return versions

    def create_model_version(
        self,
        *,
        name: str,
        source: str,
        run_id: str,
        tags: dict[str, str] | None = None,
    ) -> types.SimpleNamespace:
        self.state.model_versions_by_name.setdefault(name, [])
        version = types.SimpleNamespace(
            name=name,
            version=str(len(self.state.model_versions_by_name[name]) + 1),
            source=source,
            run_id=run_id,
            tags=dict(tags or {}),
        )
        self.state.model_versions_by_name[name].append(version)
        return version

    def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
        model_version = self._model_version(name, version)
        model_version.tags[key] = value

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        self.state.aliases[(name, alias)] = str(version)

    def get_model_version_by_alias(self, name: str, alias: str) -> types.SimpleNamespace:
        version = self.state.aliases[(name, alias)]
        return self._model_version(name, version)

    def _model_version(self, name: str, version: str) -> types.SimpleNamespace:
        for model_version in self.state.model_versions_by_name.get(name, []):
            if str(model_version.version) == str(version):
                return model_version
        raise KeyError((name, version))


def fake_mlflow_module(state: FakeMlflowState) -> types.SimpleNamespace:
    def build_client(tracking_uri: str | None = None) -> FakeMlflowClient:
        return FakeMlflowClient(state, tracking_uri=tracking_uri)

    def start_run(run_id: str) -> FakeActiveRun:
        return FakeActiveRun(state, run_id)

    def log_model(**kwargs: object) -> types.SimpleNamespace:
        artifact_name = kwargs.get("name") or kwargs.get("artifact_path")
        if not isinstance(artifact_name, str):
            raise TypeError("model artifact name is required")
        if state.active_run_id is None:
            raise RuntimeError("no active run")
        logged = types.SimpleNamespace(
            run_id=state.active_run_id,
            artifact_path=artifact_name,
            python_model=kwargs["python_model"],
            kwargs=dict(kwargs),
            model_uri=f"runs:/{state.active_run_id}/{artifact_name}",
        )
        state.logged_models.append(logged)
        return types.SimpleNamespace(model_uri=logged.model_uri)

    def load_model(model_uri: str) -> FakeLoadedPyfuncModel:
        if not isinstance(model_uri, str) or not model_uri.startswith("models:/"):
            raise RuntimeError(f"unsupported model URI: {model_uri}")
        name, version_number = resolve_model_uri(state, model_uri)
        version = model_version_by_number(state, name, version_number)
        for logged_model in state.logged_models:
            if logged_model.model_uri == version.source:
                state.loaded_model_uris.append(model_uri)
                return FakeLoadedPyfuncModel(logged_model.python_model)
        raise RuntimeError(f"logged model source not found: {version.source}")

    return types.SimpleNamespace(
        tracking=types.SimpleNamespace(MlflowClient=build_client),
        set_tracking_uri=state.set_tracking_uri,
        start_run=start_run,
        pyfunc=types.SimpleNamespace(PythonModel=FakePythonModel, log_model=log_model, load_model=load_model),
    )


class FakeActiveRun:
    def __init__(self, state: FakeMlflowState, run_id: str) -> None:
        self.state = state
        self.run_id = run_id
        self.previous_run_id: str | None = None

    def __enter__(self) -> "FakeActiveRun":
        self.previous_run_id = self.state.active_run_id
        self.state.active_run_id = self.run_id
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.state.active_run_id = self.previous_run_id


class FakePythonModel:
    pass


class FakeLoadedPyfuncModel:
    def __init__(self, python_model: Any) -> None:
        self.python_model = python_model

    def predict(self, model_input: object) -> object:
        return self.python_model.predict(None, model_input)


class InvalidPredictionModel:
    def predict(self, context: object, model_input: object) -> list[dict[str, Any]]:
        del context, model_input
        return [{"probability": 1.2, "prediction": 2}]


def one_run(state: FakeMlflowState) -> types.SimpleNamespace:
    if len(state.runs) != 1:
        raise AssertionError(f"expected exactly one MLflow run, got {len(state.runs)}")
    return next(iter(state.runs.values()))


def one_model_version(state: FakeMlflowState, name: str) -> types.SimpleNamespace:
    versions = state.model_versions_by_name.get(name, [])
    if len(versions) != 1:
        raise AssertionError(f"expected exactly one model version for {name}, got {len(versions)}")
    return versions[0]


def model_version_by_number(state: FakeMlflowState, name: str, version: str) -> types.SimpleNamespace:
    for model_version in state.model_versions_by_name.get(name, []):
        if str(model_version.version) == str(version):
            return model_version
    raise KeyError((name, version))


def resolve_model_uri(state: FakeMlflowState, model_uri: str) -> tuple[str, str]:
    rest = model_uri[len("models:/") :]
    if "@" in rest:
        if not state.alias_model_uri_supported:
            raise RuntimeError("alias model URI unsupported")
        name, alias = rest.rsplit("@", 1)
        return name, state.aliases[(name, alias)]
    name, version = rest.rsplit("/", 1)
    return name, version


def seed_model_version(
    state: FakeMlflowState,
    *,
    registry_run_id: str,
    status: str = "APPROVED",
    eligible: str = "true",
    dataset_id: str = "core_etfs",
    frequency: str = "1d",
    as_of_date: str = "2026-06-16",
    registered_model_name: str = "approved-data-logistic-baseline",
    alias: str = "paper-candidate",
) -> types.SimpleNamespace:
    state.registered_models[registered_model_name] = types.SimpleNamespace(name=registered_model_name)
    version = types.SimpleNamespace(
        name=registered_model_name,
        version="1",
        source="runs:/missing/model",
        run_id="missing-run",
        tags={
            "trading_ai.source": "local_registry",
            REGISTRY_RUN_ID_TAG: registry_run_id,
            "trading_ai.status": status,
            "trading_ai.dataset_id": dataset_id,
            "trading_ai.frequency": frequency,
            "trading_ai.as_of_date": as_of_date,
            "trading_ai.eligible_for_paper_challenger": eligible,
        },
    )
    state.model_versions_by_name[registered_model_name] = [version]
    state.aliases[(registered_model_name, alias)] = "1"
    return version


def first_registry_run_id(registry_dir: Path) -> str:
    index = json.loads((registry_dir / "index.json").read_text(encoding="utf-8"))
    return str(index["runs"][0]["run_id"])


def register_package(evaluation_dir: Path, registry_dir: Path) -> int:
    return main(
        [
            "register-evaluation",
            "--evaluation-dir",
            str(evaluation_dir),
            "--registry-dir",
            str(registry_dir),
        ]
    )


def write_evaluation_package(
    root: Path,
    *,
    as_of_date: str = "2026-06-16",
    status: str = "APPROVED",
    eligible: bool = True,
    metrics: dict[str, float] | None = None,
    extra_artifacts: bool = True,
    include_model_run: bool = False,
    model_type: str = "logistic-baseline",
) -> Path:
    evaluation_dir = root / "reports" / "tmp" / "approved_eval" / "core_etfs" / "1d" / as_of_date
    evaluation_dir.mkdir(parents=True)
    data_quality_path = evaluation_dir / "data_quality.json"
    data_quality_path.write_text(
        json.dumps(
            {
                "status": "APPROVED" if status != "BLOCKED" else "BLOCKED",
                "passed": status != "BLOCKED",
                "reasons": ["blocked"] if status == "BLOCKED" else [],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    artifacts = {
        "data_quality": {
            "path": str(data_quality_path),
            "sha256": file_sha256(data_quality_path),
        },
    }
    if extra_artifacts:
        for name in ("backtest", "model_eval", "promotion_decision"):
            path = evaluation_dir / f"{name}.json"
            path.write_text(json.dumps({"artifact": name}, indent=2, sort_keys=True), encoding="utf-8")
            artifacts[name] = {
                "path": str(path),
                "sha256": file_sha256(path),
            }
    if include_model_run:
        path = evaluation_dir / "model_run.json"
        path.write_text(json.dumps(model_run_payload(model_type), indent=2, sort_keys=True), encoding="utf-8")
        artifacts["model_run"] = {
            "path": str(path),
            "sha256": file_sha256(path),
        }

    base_metrics = {
        "accuracy": 0.61,
        "sharpe": 1.25,
        "cagr": 0.14,
        "max_drawdown": -0.08,
    }
    if metrics:
        base_metrics.update(metrics)
    summary = {
        "schema_version": 1,
        "status": status,
        "eligible_for_paper_challenger": eligible,
        "reasons": ["not_eligible"] if status == "REJECTED" else [],
        "approved_dataset": {
            "dataset_id": "core_etfs",
            "frequency": "1d",
            "dataset_hash": DATASET_HASH,
            "source_sha256": SOURCE_SHA256,
            "start": "2025-01-01",
            "end": as_of_date,
            "symbols": ["SPY"],
            "row_count": 123,
        },
        "metrics": base_metrics if status != "BLOCKED" else {},
        "artifacts": artifacts,
    }
    (evaluation_dir / "evaluation_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return evaluation_dir


def model_run_payload(model_type: str = "logistic-baseline") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_type": model_type,
        "model": {
            "feature_names": ["momentum_2"],
            "intercept": 0.0,
            "coefficients": [2.0],
        },
        "feature_names": ["momentum_2"],
        "metrics": {
            "test": {
                "accuracy": 0.61,
                "sample_count": 50.0,
            },
        },
    }


def write_universe_config(root: Path) -> Path:
    path = root / "universe.yml"
    path.write_text("universe:\n  symbols:\n    - SPY\n", encoding="utf-8")
    return path


def write_features_file(root: Path, *, include_required_feature: bool = True) -> Path:
    path = root / "features.csv"
    header = "timestamp,symbol"
    row = "2026-06-16,SPY"
    if include_required_feature:
        header += ",momentum_2"
        row += ",0.75"
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
