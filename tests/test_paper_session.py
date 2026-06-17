import json
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import main
from trading_ai.data.io import write_records
from trading_ai.data.sample import generate_sample_ohlcv
from trading_ai.features.engineering import build_features
from trading_ai.models.baseline import LogisticBaselineModel, save_model


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


def write_buy_model(path: Path) -> Path:
    save_model(
        LogisticBaselineModel(feature_names=("momentum_20",), intercept=1.0, coefficients=(5.0,)),
        str(path),
    )
    return path


def write_sample_source(path: Path, *, symbols: tuple[str, ...] = ("SPY",), end: str = "2026-06-16") -> Path:
    write_records(generate_sample_ohlcv(symbols=symbols, start="2026-03-01", end=end), path)
    return path


def write_reference_features(path: Path, *, symbols: tuple[str, ...] = ("SPY",), end: str = "2026-06-16") -> Path:
    rows = generate_sample_ohlcv(symbols=symbols, start="2026-03-01", end=end)
    write_records(build_features(rows), path)
    return path


class PaperSessionTests(unittest.TestCase):
    def test_ready_session_with_reference_features_writes_full_evidence_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            reference = write_reference_features(root / "reference_features.csv")
            output_dir = root / "paper_session"

            exit_code = main(
                paper_session_args(
                    root,
                    source=source,
                    reference=reference,
                    output_dir=output_dir,
                )
            )
            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            drift = read_json(output_dir / "monitoring" / "drift.json")
            signal = read_json(output_dir / "paper" / "paper_signal_order.json")
            artifacts = {
                "raw": (output_dir / "fresh_data" / "raw.csv").exists(),
                "features": (output_dir / "fresh_data" / "features.csv").exists(),
                "raw_manifest": (output_dir / "fresh_data" / "raw_manifest.json").exists(),
                "features_manifest": (output_dir / "fresh_data" / "features_manifest.json").exists(),
                "freshness": (output_dir / "fresh_data" / "freshness.json").exists(),
                "drift_markdown": (output_dir / "monitoring" / "drift.md").exists(),
                "audit_markdown": (output_dir / "audit" / "paper_audit.md").exists(),
                "session_markdown": (output_dir / "session.md").exists(),
            }

        self.assertEqual(exit_code, 0)
        self.assertTrue(session["ready_for_paper_review"])
        self.assertEqual(session["exit_code"], 0)
        self.assertEqual(session["summary"]["fail_count"], 0)
        self.assertFalse(session["summary"]["drift_detected"])
        self.assertTrue(audit["ready_for_paper_review"])
        self.assertEqual(audit["summary"]["fail_count"], 0)
        self.assertFalse(drift["drift_detected"])
        self.assertTrue(signal["submitted"])
        self.assertTrue(artifacts["raw"])
        self.assertTrue(artifacts["features"])
        self.assertTrue(artifacts["raw_manifest"])
        self.assertTrue(artifacts["features_manifest"])
        self.assertTrue(artifacts["freshness"])
        self.assertTrue(artifacts["drift_markdown"])
        self.assertTrue(artifacts["audit_markdown"])
        self.assertTrue(artifacts["session_markdown"])

    def test_ready_session_without_reference_features_warns_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            with mock.patch.dict(sys.modules, {"mlflow": None}):
                exit_code = main(paper_session_args(root, source=source, output_dir=output_dir))
            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")

        self.assertEqual(exit_code, 0)
        self.assertTrue(session["ready_for_paper_review"])
        self.assertEqual(session["summary"]["fail_count"], 0)
        self.assertIsNone(session["summary"]["drift_detected"])
        self.assertIsNone(session["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(session["stages"]["mlflow_candidate_review"]["status"], "skipped")
        self.assertIsNone(session["paths"]["mlflow_candidate_review"])
        self.assertIn("drift_report_missing", finding_codes(audit))
        self.assertFalse((output_dir / "monitoring" / "drift.json").exists())
        self.assertFalse((output_dir / "mlflow" / "paper_candidate_review.json").exists())

    def test_stale_freshness_blocks_session_but_writes_audit_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv", end="2026-06-01")
            output_dir = root / "paper_session"

            exit_code = main(paper_session_args(root, source=source, output_dir=output_dir, end="2026-06-01"))
            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            freshness = read_json(output_dir / "fresh_data" / "freshness.json")

        self.assertEqual(exit_code, 1)
        self.assertFalse(session["ready_for_paper_review"])
        self.assertFalse(audit["ready_for_paper_review"])
        self.assertIn("stale_symbol", freshness["reasons"])
        self.assertIn("freshness_blocked", finding_codes(audit))

    def test_high_threshold_blocks_session_with_no_buy_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            exit_code = main(
                paper_session_args(
                    root,
                    source=source,
                    output_dir=output_dir,
                    extra=["--signal-threshold", "1.1"],
                )
            )
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            signal = read_json(output_dir / "paper" / "paper_signal_order.json")

        self.assertEqual(exit_code, 1)
        self.assertFalse(audit["ready_for_paper_review"])
        self.assertIn("no_buy_signal", finding_codes(audit))
        self.assertIsNone(signal["selected_signal"])
        self.assertFalse(signal["submitted"])

    def test_invalid_source_csv_returns_two_without_session_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "bad.csv"
            source.write_text("timestamp,symbol,open\n2026-06-16,SPY,100\n", encoding="utf-8")
            output_dir = root / "paper_session"

            exit_code = main(paper_session_args(root, source=source, output_dir=output_dir))

        self.assertEqual(exit_code, 2)
        self.assertFalse((output_dir / "session.json").exists())
        self.assertFalse((output_dir / "fresh_data" / "freshness.json").exists())

    def test_session_does_not_read_alpaca_credentials_or_build_real_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            with mock.patch(
                "trading_ai.execution.alpaca_connection.load_alpaca_paper_credentials",
                side_effect=AssertionError("credentials should not be read"),
            ), mock.patch(
                "trading_ai.cli.build_alpaca_paper_client",
                side_effect=AssertionError("real paper client should not be built"),
            ):
                exit_code = main(paper_session_args(root, source=source, output_dir=output_dir))

            signal = read_json(output_dir / "paper" / "paper_signal_order.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(signal["mode"], "dry-run")
        self.assertEqual(signal["broker"], "alpaca")
        self.assertTrue(signal["account"]["dry_run"])

    def test_mlflow_candidate_review_passes_and_records_session_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            with mock.patch(
                "trading_ai.evaluation.mlflow_paper_candidate_review.review_mlflow_paper_candidate",
                side_effect=fake_passed_mlflow_review,
            ):
                exit_code = main(
                    paper_session_args(
                        root,
                        source=source,
                        output_dir=output_dir,
                        extra=["--review-mlflow-paper-candidate"],
                    )
                )

            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            review = read_json(output_dir / "mlflow" / "paper_candidate_review.json")
            session_markdown = (output_dir / "session.md").read_text(encoding="utf-8")
            review_markdown_exists = (output_dir / "mlflow" / "paper_candidate_review.md").exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(session["ready_for_paper_review"])
        self.assertEqual(session["stages"]["mlflow_candidate_review"]["status"], "passed")
        self.assertTrue(session["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(session["summary"]["mlflow_registry_run_id"], "registry-run-1")
        self.assertEqual(session["summary"]["mlflow_model_version"], "7")
        self.assertEqual(session["summary"]["mlflow_alias"], "paper-candidate")
        self.assertEqual(review["status"], "PASSED")
        self.assertEqual(audit["sources"]["mlflow_candidate_review_report"], str(output_dir / "mlflow" / "paper_candidate_review.json"))
        self.assertIn("MLflow paper-candidate review: `passed`", session_markdown)
        self.assertTrue(review_markdown_exists)

    def test_mlflow_candidate_review_failure_blocks_session_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            with mock.patch(
                "trading_ai.evaluation.mlflow_paper_candidate_review.review_mlflow_paper_candidate",
                side_effect=fake_failed_mlflow_review,
            ):
                exit_code = main(
                    paper_session_args(
                        root,
                        source=source,
                        output_dir=output_dir,
                        extra=["--review-mlflow-paper-candidate"],
                    )
                )

            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            review = read_json(output_dir / "mlflow" / "paper_candidate_review.json")

        self.assertEqual(exit_code, 1)
        self.assertFalse(session["ready_for_paper_review"])
        self.assertEqual(session["stages"]["mlflow_candidate_review"]["status"], "blocked")
        self.assertFalse(session["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(review["status"], "FAILED")
        self.assertIn("candidate failed smoke test", review["failures"])
        self.assertIn("mlflow_candidate_review_failed", finding_codes(audit))

    def test_mlflow_candidate_review_operational_error_returns_two(self) -> None:
        from trading_ai.evaluation.mlflow_paper_candidate_review import MlflowPaperCandidateOperationalError

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_sample_source(root / "source.csv")
            output_dir = root / "paper_session"

            with mock.patch(
                "trading_ai.evaluation.mlflow_paper_candidate_review.review_mlflow_paper_candidate",
                side_effect=MlflowPaperCandidateOperationalError("registry unavailable"),
            ):
                exit_code = main(
                    paper_session_args(
                        root,
                        source=source,
                        output_dir=output_dir,
                        extra=["--review-mlflow-paper-candidate"],
                    )
                )

        self.assertEqual(exit_code, 2)
        self.assertFalse((output_dir / "session.json").exists())

    def test_empty_features_write_failed_mlflow_review_and_block_without_calling_mlflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "empty_source.csv"
            source.write_text("timestamp,symbol,open,high,low,close,volume\n", encoding="utf-8")
            output_dir = root / "paper_session"

            with mock.patch(
                "trading_ai.evaluation.mlflow_paper_candidate_review.review_mlflow_paper_candidate",
                side_effect=AssertionError("MLflow review should not run for empty features"),
            ):
                exit_code = main(
                    paper_session_args(
                        root,
                        source=source,
                        output_dir=output_dir,
                        extra=["--review-mlflow-paper-candidate"],
                    )
                )

            session = read_json(output_dir / "session.json")
            audit = read_json(output_dir / "audit" / "paper_audit.json")
            review = read_json(output_dir / "mlflow" / "paper_candidate_review.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(session["stages"]["mlflow_candidate_review"]["status"], "blocked")
        self.assertFalse(session["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(review["status"], "FAILED")
        self.assertIn("feature source contains no rows", review["failures"][0])
        self.assertIn("mlflow_candidate_review_failed", finding_codes(audit))


def paper_session_args(
    root: Path,
    *,
    source: Path,
    output_dir: Path,
    reference: Path | None = None,
    end: str = "2026-06-16",
    extra: list[str] | None = None,
) -> list[str]:
    universe = write_universe(root / "universe.yml", ("SPY",))
    risk = write_risk(root / "risk.yml")
    model = write_buy_model(root / "model.json")
    args = [
        "paper-session",
        "--source-csv",
        str(source),
        "--from",
        "2026-03-01",
        "--to",
        end,
        "--config",
        str(universe),
        "--risk",
        str(risk),
        "--signal-model",
        str(model),
        "--as-of-date",
        "2026-06-16",
        "--output-dir",
        str(output_dir),
    ]
    if reference is not None:
        args.extend(["--reference-features", str(reference)])
    if extra:
        args.extend(extra)
    return args


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def fake_passed_mlflow_review(**kwargs: object) -> types.SimpleNamespace:
    output = Path(str(kwargs["output"]))
    markdown = Path(str(kwargs["markdown_output"]))
    payload = mlflow_review_payload(status="PASSED", failures=[])
    write_mlflow_review_artifacts(payload, output=output, markdown=markdown)
    return types.SimpleNamespace(report=payload, output_path=output, markdown_path=markdown)


def fake_failed_mlflow_review(**kwargs: object) -> types.SimpleNamespace:
    from trading_ai.evaluation.mlflow_paper_candidate_review import (
        MlflowPaperCandidateReviewResult,
        MlflowPaperCandidateValidationError,
    )

    output = Path(str(kwargs["output"]))
    markdown = Path(str(kwargs["markdown_output"]))
    payload = mlflow_review_payload(status="FAILED", failures=["candidate failed smoke test"])
    write_mlflow_review_artifacts(payload, output=output, markdown=markdown)
    result = MlflowPaperCandidateReviewResult(output_path=output, markdown_path=markdown, report=payload)
    raise MlflowPaperCandidateValidationError("candidate failed smoke test", result=result)


def mlflow_review_payload(*, status: str, failures: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": status,
        "registered_model_name": "approved-data-logistic-baseline",
        "alias": "paper-candidate",
        "model_version": "7",
        "model_uri": "models:/approved-data-logistic-baseline@paper-candidate",
        "registry_run_id": "registry-run-1",
        "local_registry_status": "APPROVED",
        "eligible_for_paper_challenger": True,
        "dataset_id": "core_etfs",
        "frequency": "1d",
        "as_of_date": "2026-06-16",
        "feature_names": ["momentum_20"],
        "feature_source": "fresh_data/features.csv",
        "prediction_sample": [
            {"symbol": "SPY", "timestamp": "2026-06-16", "probability": 0.72, "prediction": 1}
        ],
        "failures": failures,
        "warnings": [],
    }


def write_mlflow_review_artifacts(payload: dict[str, object], *, output: Path, markdown: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("# MLflow Paper Candidate Review\n", encoding="utf-8")


def finding_codes(report: dict[str, object]) -> set[str]:
    return {str(finding["code"]) for finding in report["findings"]}  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
