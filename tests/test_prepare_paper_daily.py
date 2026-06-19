import contextlib
import io
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

from trading_ai.cli import build_parser, main
from trading_ai.data.io import PARQUET_DEPENDENCY_MESSAGE, ParquetDependencyError
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.evaluation.approved_data import ApprovedEvaluationResult
from trading_ai.evaluation.registry import EvaluationRegistrationResult
from trading_ai.execution.paper_daily import PaperDailyOperationalError, PaperDailyResult


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


def write_signal_model(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "model_type": "logistic-baseline",
                "feature_names": ["momentum_20", "realized_volatility_20"],
                "coefficients": [0.0, 0.0],
                "intercept": 0.0,
                "classes": [0, 1],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def write_candidate_spec(path: Path, *, dataset_hash: str = "b" * 64) -> Path:
    path.write_text(
        json.dumps(
            {
                "candidate_id": "candidate-return-1d",
                "model_type": "logistic-baseline",
                "feature_names": ["return_1d"],
                "preprocessing": {"type": "none"},
                "training_config": {
                    "learning_rate": 0.2,
                    "epochs": 80,
                    "l2": 0.001,
                    "test_fraction": 0.25,
                },
                "dataset_hash": dataset_hash,
                "source_sha256": "a" * 64,
                "as_of_date": "2026-06-16",
                "authority": {
                    "mutates_latest_model": False,
                    "orders_submitted": False,
                    "broker_client_built": False,
                    "credentials_read": False,
                },
                "safety": {
                    "paper_only": True,
                    "live_trading_allowed": False,
                    "futures_forex_execution": False,
                    "llm_order_authority": "none",
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def daily_records() -> list[dict[str, object]]:
    return generate_sample_ohlcv(symbols=("SPY",), start="2025-01-01", end="2026-06-16")


def write_approved_package(root: Path, *, records: list[dict[str, object]]) -> Path:
    approved_dir = root / "approved" / "core_etfs" / "1d"
    approved_dir.mkdir(parents=True)
    dataset_path = approved_dir / "ohlcv.parquet"
    manifest_path = approved_dir / "manifest.json"
    catalog_path = approved_dir / "catalog_entry.json"
    dataset_path.write_bytes(b"PAR1 fake approved parquet for prepare-paper-daily tests")
    manifest = build_dataset_manifest(records, source=str(dataset_path))
    manifest.update(
        {
            "dataset_id": "core_etfs",
            "frequency": "1d",
            "source_sha256": "a" * 64,
            "provider": "manual_csv",
            "provider_kind": "manual",
            "license_note": "approved local fixture",
            "as_of_date": "2026-06-16",
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
        "as_of_date": "2026-06-16",
        "provider": "manual_csv",
        "provider_kind": "manual",
        "network_allowed": False,
        "license_note": "approved local fixture",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog_entry, indent=2, sort_keys=True), encoding="utf-8")
    return approved_dir


def write_successful_evaluation(run_dir: Path) -> ApprovedEvaluationResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "evaluation_summary.json"
    summary_markdown_path = run_dir / "evaluation_summary.md"
    data_quality_path = run_dir / "data_quality.json"
    backtest_path = run_dir / "backtest.json"
    promotion_path = run_dir / "promotion_decision.json"
    backtest_path.write_text(json.dumps({"metrics": {"sharpe": 1.0}}), encoding="utf-8")
    promotion_path.write_text(json.dumps({"approved": True, "reasons": []}), encoding="utf-8")
    data_quality_path.write_text(json.dumps({"passed": True, "reasons": []}), encoding="utf-8")
    summary_path.write_text(
        json.dumps(
            {
                "status": "APPROVED",
                "reasons": [],
                "artifacts": {
                    "backtest": {"path": str(backtest_path)},
                    "promotion_decision": {"path": str(promotion_path)},
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    summary_markdown_path.write_text("# Approved\n", encoding="utf-8")
    return ApprovedEvaluationResult(
        exit_code=0,
        status="APPROVED",
        output_dir=run_dir,
        summary_path=summary_path,
        summary_markdown_path=summary_markdown_path,
        data_quality_path=data_quality_path,
        promotion_decision_path=promotion_path,
    )


def write_registry_result(registry_dir: Path) -> EvaluationRegistrationResult:
    run_path = registry_dir / "runs" / "approved-core_etfs-1d-2026-06-16.json"
    index_path = registry_dir / "index.json"
    markdown_path = registry_dir / "index.md"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text("{}", encoding="utf-8")
    index_path.write_text("[]", encoding="utf-8")
    markdown_path.write_text("# Registry\n", encoding="utf-8")
    return EvaluationRegistrationResult(
        run_id="approved-core_etfs-1d-2026-06-16",
        status="APPROVED",
        registry_dir=registry_dir,
        run_path=run_path,
        index_path=index_path,
        markdown_path=markdown_path,
    )


class PreparePaperDailyTests(unittest.TestCase):
    def test_parser_defaults_and_mutually_exclusive_inputs(self) -> None:
        args = build_parser().parse_args(
            [
                "prepare-paper-daily",
                "--source",
                "/tmp/approved.csv",
                "--dataset-id",
                "core_etfs",
                "--frequency",
                "1d",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-16",
                "--as-of-date",
                "2026-06-16",
                "--license-note",
                "manual approval",
            ]
        )

        self.assertEqual(args.provider, "manual_csv")
        self.assertEqual(args.config, "configs/universe.yml")
        self.assertEqual(args.risk, "configs/risk.yml")
        self.assertEqual(args.output_dir, "reports/tmp/paper_daily_prepare")
        self.assertEqual(args.registry_dir, "reports/registry")
        self.assertIsNone(args.candidate_spec)
        self.assertFalse(args.run_offline_smoke)

        smoke_args = build_parser().parse_args(
            [
                "prepare-paper-daily",
                "--approved-dir",
                "/tmp/approved/core_etfs/1d",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-16",
                "--as-of-date",
                "2026-06-16",
                "--run-offline-smoke",
            ]
        )
        self.assertTrue(smoke_args.run_offline_smoke)

        source_smoke_args = build_parser().parse_args(
            [
                "prepare-paper-daily",
                "--source",
                "/tmp/source.csv",
                "--dataset-id",
                "core_etfs",
                "--frequency",
                "1d",
                "--from",
                "2026-03-01",
                "--to",
                "2026-06-16",
                "--as-of-date",
                "2026-06-16",
                "--license-note",
                "manual approval",
                "--run-offline-smoke",
            ]
        )
        self.assertTrue(source_smoke_args.run_offline_smoke)

        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "prepare-paper-daily",
                    "--source",
                    "/tmp/source.csv",
                    "--approved-dir",
                    "/tmp/approved",
                    "--from",
                    "2026-03-01",
                    "--to",
                    "2026-06-16",
                    "--as-of-date",
                    "2026-06-16",
                ]
            )

    def test_approved_package_generates_readiness_config_and_registry_run(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "latest_model.json")
            reference_features = root / "reference_features.csv"
            reference_features.write_text("timestamp,symbol,momentum_20,realized_volatility_20\n", encoding="utf-8")
            output_dir = root / "prepare"
            registry_dir = root / "registry"

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--reference-features",
                        str(reference_features),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                        "--min-accuracy-lift",
                        "-1.0",
                        "--min-test-samples",
                        "1",
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))
            markdown = (run_dir / "readiness.md").read_text(encoding="utf-8")
            generated_config = yaml.safe_load((run_dir / "paper_daily.generated.yml").read_text(encoding="utf-8"))
            paper_daily = generated_config["paper_daily"]
            registry_run_exists = Path(readiness["registry"]["run_path"]).exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(readiness["ready_for_paper_daily"])
        self.assertEqual(readiness["status"], "READY")
        self.assertEqual(readiness["registry"]["registered"], True)
        self.assertTrue(registry_run_exists)
        self.assertEqual(paper_daily["source_csv"], str(approved_dir / "ohlcv.parquet"))
        self.assertEqual(paper_daily["from"], "2026-03-01")
        self.assertEqual(paper_daily["to"], "2026-06-16")
        self.assertEqual(paper_daily["as_of_date"], "2026-06-16")
        self.assertEqual(readiness["inputs"]["signal_model"], str(signal_model))
        self.assertEqual(readiness["inputs"]["reference_features"], str(reference_features))
        self.assertEqual(paper_daily["signal_model"], str(signal_model))
        self.assertEqual(paper_daily["reference_features"], str(reference_features))
        self.assertEqual(paper_daily["backtest_report"], str(run_dir / "backtest.json"))
        self.assertEqual(paper_daily["promotion_report"], str(run_dir / "promotion_decision.json"))
        self.assertEqual(paper_daily["sessions_root"], str(run_dir / "paper_daily" / "sessions"))
        self.assertEqual(
            paper_daily["session_dir"],
            str(run_dir / "paper_daily" / "sessions" / "daily" / "{as_of_date}"),
        )
        self.assertEqual(paper_daily["output"], str(run_dir / "paper_daily" / "daily.json"))
        self.assertEqual(paper_daily["monitor_output"], str(run_dir / "paper_daily" / "monitor.json"))
        self.assertNotIn("confirm_paper", paper_daily)
        self.assertFalse(readiness["offline_smoke"]["requested"])
        self.assertFalse(readiness["offline_smoke"]["ran"])
        self.assertIn("paper-daily --config", readiness["recommended_commands"]["offline_review"])
        self.assertIn("paper-daily-from-readiness", readiness["recommended_commands"]["broker_confirmed"])
        self.assertIn("--confirm-readiness", readiness["recommended_commands"]["broker_confirmed"])
        self.assertIn("Broker confirmed", markdown)

    def test_prepare_paper_daily_uses_candidate_spec_only_when_approved(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "deployment_model.json")
            candidate_spec = write_candidate_spec(root / "best_candidate_spec.json")
            output_dir = root / "prepare"
            registry_dir = root / "registry"
            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            eval_result = write_successful_evaluation(run_dir)
            registry_result = write_registry_result(registry_dir)

            with mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.evaluate_approved_data",
                return_value=eval_result,
            ) as eval_mock, mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.register_evaluation",
                return_value=registry_result,
            ):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--candidate-spec",
                        str(candidate_spec),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                    ]
                )

            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))
            generated_config = yaml.safe_load((run_dir / "paper_daily.generated.yml").read_text(encoding="utf-8"))
            eval_kwargs = eval_mock.call_args.kwargs

        self.assertEqual(exit_code, 0)
        self.assertEqual(eval_kwargs["candidate_spec"], str(candidate_spec))
        self.assertEqual(readiness["inputs"]["candidate_spec"], str(candidate_spec))
        self.assertEqual(readiness["inputs"]["signal_model"], str(signal_model))
        self.assertEqual(generated_config["paper_daily"]["signal_model"], str(signal_model))
        self.assertEqual(readiness["status"], "READY")

    def test_approved_package_with_offline_smoke_runs_paper_daily_artifacts(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "latest_model.json")
            output_dir = root / "prepare"
            registry_dir = root / "registry"

            with mock.patch(
                "trading_ai.evaluation.approved_data.read_records",
                return_value=records,
            ), mock.patch(
                "trading_ai.data.market_data.read_records",
                return_value=records,
            ), mock.patch(
                "trading_ai.execution.paper_execute_session.build_alpaca_paper_client",
                side_effect=AssertionError("submit client should not be built"),
            ), mock.patch(
                "trading_ai.execution.paper_close_session.build_alpaca_paper_client",
                side_effect=AssertionError("close client should not be built"),
            ), mock.patch(
                "trading_ai.execution.alpaca_connection.load_alpaca_paper_credentials",
                side_effect=AssertionError("credentials should not be read"),
            ), mock.patch(
                "trading_ai.execution.paper_monitor.send_paper_monitor_telegram",
                side_effect=AssertionError("telegram should not be sent"),
            ), mock.patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "SECRET_TOKEN", "TELEGRAM_CHAT_ID": "123456"},
            ):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                        "--min-accuracy-lift",
                        "-1.0",
                        "--min-test-samples",
                        "1",
                        "--run-offline-smoke",
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))
            markdown = (run_dir / "readiness.md").read_text(encoding="utf-8")
            smoke = readiness["offline_smoke"]
            artifacts = smoke["artifacts"]
            daily_payload = json.loads(Path(artifacts["daily_json"]).read_text(encoding="utf-8"))
            session_exists = Path(artifacts["session_json"]).exists()
            observability_exists = Path(artifacts["observability_json"]).exists()
            monitor_exists = Path(artifacts["monitor_json"]).exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(readiness["ready_for_paper_daily"])
        self.assertEqual(readiness["status"], "READY")
        self.assertTrue(smoke["requested"])
        self.assertTrue(smoke["ran"])
        self.assertEqual(smoke["exit_code"], 0)
        self.assertEqual(smoke["confirmations"]["confirm_paper"], False)
        self.assertEqual(smoke["telegram"]["send_telegram"], False)
        self.assertFalse(daily_payload["confirmations"]["confirm_paper"])
        self.assertFalse(daily_payload["confirmations"]["confirm_auto_close"])
        self.assertFalse(daily_payload["confirmations"]["confirm_auto_submit"])
        self.assertIsNone(daily_payload["final_monitor"]["telegram"])
        self.assertNotIn("SECRET_TOKEN", json.dumps(daily_payload))
        self.assertTrue(session_exists)
        self.assertTrue(observability_exists)
        self.assertTrue(monitor_exists)
        self.assertIn("Offline Smoke", markdown)

    def test_offline_smoke_blocked_marks_prepare_blocked_after_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=daily_records())
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "latest_model.json")
            output_dir = root / "prepare"
            registry_dir = root / "registry"
            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            eval_result = write_successful_evaluation(run_dir)
            registry_result = write_registry_result(registry_dir)
            smoke_result = PaperDailyResult(
                exit_code=1,
                status="BLOCKED",
                output_path=run_dir / "paper_daily" / "daily.json",
                markdown_path=run_dir / "paper_daily" / "daily.md",
                payload={
                    "reasons": ["paper_session_not_ready"],
                    "artifacts": {"daily_json": str(run_dir / "paper_daily" / "daily.json")},
                },
            )

            with mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.evaluate_approved_data",
                return_value=eval_result,
            ), mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.register_evaluation",
                return_value=registry_result,
            ), mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.run_paper_daily",
                return_value=smoke_result,
            ) as run_mock:
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                        "--run-offline-smoke",
                    ]
                )

            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))
            run_kwargs = run_mock.call_args.kwargs

        self.assertEqual(exit_code, 1)
        self.assertEqual(readiness["status"], "BLOCKED")
        self.assertFalse(readiness["ready_for_paper_daily"])
        self.assertTrue(readiness["registry"]["registered"])
        self.assertTrue(readiness["offline_smoke"]["ran"])
        self.assertEqual(readiness["offline_smoke"]["exit_code"], 1)
        self.assertIn("offline_smoke_blocked", readiness["reasons"])
        self.assertIn("paper_session_not_ready", readiness["reasons"])
        self.assertFalse(run_kwargs["confirm_paper"])
        self.assertFalse(run_kwargs["confirm_auto_close"])
        self.assertFalse(run_kwargs["confirm_auto_submit"])
        self.assertFalse(run_kwargs["send_telegram"])

    def test_offline_smoke_operational_error_marks_prepare_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=daily_records())
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "latest_model.json")
            output_dir = root / "prepare"
            registry_dir = root / "registry"
            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            eval_result = write_successful_evaluation(run_dir)
            registry_result = write_registry_result(registry_dir)
            stderr = io.StringIO()

            with mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.evaluate_approved_data",
                return_value=eval_result,
            ), mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.register_evaluation",
                return_value=registry_result,
            ), mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.run_paper_daily",
                side_effect=PaperDailyOperationalError("smoke brokerless failure"),
            ), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                        "--run-offline-smoke",
                    ]
                )

            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)
        self.assertEqual(readiness["status"], "ERROR")
        self.assertFalse(readiness["ready_for_paper_daily"])
        self.assertTrue(readiness["offline_smoke"]["ran"])
        self.assertEqual(readiness["offline_smoke"]["status"], "ERROR")
        self.assertEqual(readiness["offline_smoke"]["exit_code"], 2)
        self.assertIn("offline_smoke_operational_error", readiness["reasons"])
        self.assertIn("smoke brokerless failure", readiness["reasons"])
        self.assertIn("smoke brokerless failure", stderr.getvalue())

    def test_rejected_evaluation_writes_readiness_without_config_or_registry(self) -> None:
        records = daily_records()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=records)
            universe = write_universe(root / "universe.yml", ("SPY",))
            risk = write_risk(root / "risk.yml")
            signal_model = write_signal_model(root / "latest_model.json")
            output_dir = root / "prepare"
            registry_dir = root / "registry"

            with mock.patch("trading_ai.evaluation.approved_data.read_records", return_value=records):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--risk",
                        str(risk),
                        "--signal-model",
                        str(signal_model),
                        "--output-dir",
                        str(output_dir),
                        "--registry-dir",
                        str(registry_dir),
                        "--min-test-samples",
                        "9999",
                    ]
                )

            run_dir = output_dir / "core_etfs" / "1d" / "2026-06-16"
            readiness = json.loads((run_dir / "readiness.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(readiness["status"], "REJECTED")
        self.assertFalse(readiness["ready_for_paper_daily"])
        self.assertFalse(readiness["registry"]["registered"])
        self.assertIsNone(readiness["paper_daily_config_path"])
        self.assertFalse((run_dir / "paper_daily.generated.yml").exists())
        self.assertIn("insufficient_test_samples", readiness["reasons"])

    def test_missing_source_is_operational_error_with_readiness_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe = write_universe(root / "universe.yml", ("SPY",))
            output_dir = root / "prepare"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--source",
                        str(root / "missing.csv"),
                        "--dataset-id",
                        "core_etfs",
                        "--frequency",
                        "1d",
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--config",
                        str(universe),
                        "--license-note",
                        "manual approval",
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            readiness = json.loads(
                (output_dir / "core_etfs" / "1d" / "2026-06-16" / "readiness.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(readiness["status"], "ERROR")
        self.assertFalse(readiness["ready_for_paper_daily"])
        self.assertIn("source file not found", stderr.getvalue())

    def test_parquet_dependency_error_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.csv"
            source.write_text("timestamp,symbol,open,high,low,close,volume\n", encoding="utf-8")
            output_dir = root / "prepare"
            stderr = io.StringIO()

            with mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.import_approved_data",
                side_effect=ParquetDependencyError(PARQUET_DEPENDENCY_MESSAGE),
            ), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--source",
                        str(source),
                        "--dataset-id",
                        "core_etfs",
                        "--frequency",
                        "1d",
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--license-note",
                        "manual approval",
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn('pip install -e ".[research]"', stderr.getvalue())

    def test_prepare_does_not_build_alpaca_client_or_read_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved_dir = write_approved_package(root, records=daily_records())
            run_dir = root / "prepare" / "core_etfs" / "1d" / "2026-06-16"
            run_dir.mkdir(parents=True)
            summary_path = run_dir / "evaluation_summary.json"
            data_quality_path = run_dir / "data_quality.json"
            promotion_path = run_dir / "promotion_decision.json"
            summary_path.write_text(json.dumps({"reasons": [], "artifacts": {}}), encoding="utf-8")
            data_quality_path.write_text("{}", encoding="utf-8")
            promotion_path.write_text("{}", encoding="utf-8")
            eval_result = ApprovedEvaluationResult(
                exit_code=0,
                status="APPROVED",
                output_dir=run_dir,
                summary_path=summary_path,
                summary_markdown_path=run_dir / "evaluation_summary.md",
                data_quality_path=data_quality_path,
                promotion_decision_path=promotion_path,
            )
            registry_result = EvaluationRegistrationResult(
                run_id="approved-core_etfs-1d-2026-06-16",
                status="APPROVED",
                registry_dir=root / "registry",
                run_path=root / "registry" / "runs" / "run.json",
                index_path=root / "registry" / "index.json",
                markdown_path=root / "registry" / "index.md",
            )

            with mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.evaluate_approved_data",
                return_value=eval_result,
            ), mock.patch(
                "trading_ai.evaluation.paper_daily_prepare.register_evaluation",
                return_value=registry_result,
            ), mock.patch(
                "trading_ai.cli.build_alpaca_paper_client",
                side_effect=AssertionError("alpaca client should not be built"),
            ), mock.patch(
                "trading_ai.execution.alpaca_connection.load_alpaca_paper_credentials",
                side_effect=AssertionError("credentials should not be read"),
            ):
                exit_code = main(
                    [
                        "prepare-paper-daily",
                        "--approved-dir",
                        str(approved_dir),
                        "--from",
                        "2026-03-01",
                        "--to",
                        "2026-06-16",
                        "--as-of-date",
                        "2026-06-16",
                        "--output-dir",
                        str(root / "prepare"),
                        "--registry-dir",
                        str(root / "registry"),
                    ]
                )

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
