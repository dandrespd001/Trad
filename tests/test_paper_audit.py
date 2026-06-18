import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.cli import build_parser
from trading_ai.execution.paper_audit import evaluate_paper_audit


def fresh_report(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "allowed": True,
        "reasons": [],
        "as_of_date": "2026-06-16",
        "max_age_days": 5,
        "symbols": {
            "SPY": {"symbol": "SPY", "status": "fresh", "timestamp": "2026-06-16", "age_days": 0}
        },
        "raw_path": "/tmp/fresh_data/raw.csv",
        "features_path": "/tmp/fresh_data/features.csv",
    }
    payload.update(overrides)
    return payload


def signal_report(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": "dry-run",
        "broker": "alpaca",
        "preflight": {"allowed": True, "reasons": [], "checked_at": "2026-06-16", "max_feature_age_days": 5},
        "submitted": True,
        "selected_signal": {
            "timestamp": "2026-06-16",
            "symbol": "SPY",
            "probability": 0.71,
            "threshold": 0.5,
            "action": "buy",
        },
        "order_intent": {
            "symbol": "SPY",
            "side": "buy",
            "client_order_id": "signal-spy-20260616",
            "type": "market",
            "time_in_force": "day",
            "notional": 1.0,
        },
        "order_result": {
            "accepted": True,
            "status": "dry_run_accepted",
            "reasons": [],
            "dry_run": True,
            "broker_response": None,
        },
    }
    payload.update(overrides)
    return payload


def mlflow_review_report(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "PASSED",
        "registered_model_name": "approved-data-logistic-baseline",
        "alias": "paper-candidate",
        "model_version": "3",
        "model_uri": "models:/approved-data-logistic-baseline@paper-candidate",
        "registry_run_id": "registry-run-1",
        "local_registry_status": "APPROVED",
        "eligible_for_paper_challenger": True,
        "feature_source": "/tmp/fresh_data/features.csv",
        "prediction_sample": [{"symbol": "SPY", "timestamp": "2026-06-16", "probability": 0.7, "prediction": 1}],
        "failures": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


class PaperAuditTests(unittest.TestCase):
    def test_parser_defaults_write_audit_reports_to_tmp_latest_paths(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-audit",
                "--freshness-report",
                "freshness.json",
                "--signal-report",
                "signal.json",
            ]
        )

        self.assertEqual(args.output, "reports/tmp/paper_audit/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/paper_audit/latest.md")

    def test_audit_allows_ready_signal_order_session(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            backtest_report={"metrics": {"sharpe": 1.2, "max_drawdown": -0.03}},
            promotion_report={"approved": True, "reasons": []},
            generated_at="2026-06-16T00:00:00+00:00",
        ).to_dict()

        self.assertTrue(report["ready_for_paper_review"])
        self.assertEqual(report["summary"]["fail_count"], 0)
        self.assertEqual(report["summary"]["selected_symbol"], "SPY")
        self.assertEqual(report["summary"]["mode"], "dry-run")
        self.assertNotIn("mlflow_candidate_review_passed", report["summary"])

    def test_audit_blocks_when_freshness_is_blocked(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(allowed=False, reasons=["stale_symbol"]),
            signal_report=signal_report(),
        ).to_dict()

        self.assertFalse(report["ready_for_paper_review"])
        self.assertIn("freshness_blocked", finding_codes(report))

    def test_audit_blocks_when_preflight_is_blocked(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(preflight={"allowed": False, "reasons": ["stale_features"]}),
        ).to_dict()

        self.assertFalse(report["ready_for_paper_review"])
        self.assertIn("preflight_blocked", finding_codes(report))

    def test_audit_blocks_when_selected_signal_is_missing_or_not_buy(self) -> None:
        missing = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(selected_signal=None, order_intent=None, submitted=False),
        ).to_dict()
        hold = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(selected_signal={"symbol": "SPY", "timestamp": "2026-06-16", "action": "hold"}),
        ).to_dict()

        self.assertIn("no_buy_signal", finding_codes(missing))
        self.assertIn("no_buy_signal", finding_codes(hold))

    def test_audit_warns_for_unmatched_optional_reconciliation(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            reconciliation_report={"reconciliation": {"matched": False, "differences": ["not_filled_yet"]}},
        ).to_dict()

        self.assertTrue(report["ready_for_paper_review"])
        self.assertIn("reconciliation_unmatched", finding_codes(report))
        self.assertEqual(report["summary"]["fail_count"], 0)

    def test_audit_warns_for_detected_feature_drift_without_blocking(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            drift_report={
                "drift_detected": True,
                "summary": {"drifted_feature_count": 1, "warn_count": 2},
            },
            backtest_report={"metrics": {"sharpe": 1.2}},
            promotion_report={"approved": True, "reasons": []},
        ).to_dict()

        self.assertTrue(report["ready_for_paper_review"])
        self.assertIn("feature_drift_detected", finding_codes(report))
        self.assertEqual(report["summary"]["fail_count"], 0)
        self.assertTrue(report["summary"]["drift_detected"])
        self.assertEqual(report["summary"]["drifted_feature_count"], 1)
        self.assertEqual(report["summary"]["drift_warning_count"], 2)

    def test_audit_warns_when_drift_report_is_missing_without_blocking(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            backtest_report={"metrics": {"sharpe": 1.2}},
            promotion_report={"approved": True, "reasons": []},
        ).to_dict()

        self.assertTrue(report["ready_for_paper_review"])
        self.assertIn("drift_report_missing", finding_codes(report))
        self.assertEqual(report["summary"]["fail_count"], 0)

    def test_audit_accepts_passed_mlflow_candidate_review(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            backtest_report={"metrics": {"sharpe": 1.2}},
            promotion_report={"approved": True, "reasons": []},
            mlflow_candidate_review_report=mlflow_review_report(),
        ).to_dict()

        self.assertTrue(report["ready_for_paper_review"])
        self.assertEqual(report["summary"]["fail_count"], 0)
        self.assertTrue(report["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(report["summary"]["mlflow_registry_run_id"], "registry-run-1")
        self.assertEqual(report["summary"]["mlflow_model_version"], "3")
        self.assertEqual(report["summary"]["mlflow_alias"], "paper-candidate")
        self.assertNotIn("mlflow_candidate_review_failed", finding_codes(report))

    def test_audit_blocks_failed_mlflow_candidate_review(self) -> None:
        report = evaluate_paper_audit(
            freshness_report=fresh_report(),
            signal_report=signal_report(),
            mlflow_candidate_review_report=mlflow_review_report(
                status="FAILED",
                failures=["prediction row count mismatch"],
            ),
        ).to_dict()

        self.assertFalse(report["ready_for_paper_review"])
        self.assertFalse(report["summary"]["mlflow_candidate_review_passed"])
        self.assertIn("mlflow_candidate_review_failed", finding_codes(report))

    def test_cli_writes_json_and_markdown_for_ready_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            freshness_path = write_json(root / "freshness.json", fresh_report())
            signal_path = write_json(root / "signal.json", signal_report())
            drift_path = write_json(
                root / "drift.json",
                {"drift_detected": False, "summary": {"drifted_feature_count": 0, "warn_count": 0}},
            )
            output = root / "audit.json"
            markdown = root / "audit.md"

            exit_code = main(
                [
                    "paper-audit",
                    "--freshness-report",
                    str(freshness_path),
                    "--signal-report",
                    str(signal_path),
                    "--drift-report",
                    str(drift_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--as-of-date",
                    "2026-06-16",
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ready_for_paper_review"])
        self.assertEqual(payload["summary"]["fail_count"], 0)
        self.assertFalse(payload["summary"]["drift_detected"])
        self.assertIn("READY", markdown_text)
        self.assertIn("signal.json", markdown_text)

    def test_cli_includes_passed_mlflow_candidate_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            freshness_path = write_json(root / "freshness.json", fresh_report())
            signal_path = write_json(root / "signal.json", signal_report())
            mlflow_path = write_json(root / "mlflow.json", mlflow_review_report())
            output = root / "audit.json"
            markdown = root / "audit.md"

            exit_code = main(
                [
                    "paper-audit",
                    "--freshness-report",
                    str(freshness_path),
                    "--signal-report",
                    str(signal_path),
                    "--mlflow-candidate-review-report",
                    str(mlflow_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--as-of-date",
                    "2026-06-16",
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            markdown_text = markdown.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["summary"]["mlflow_candidate_review_passed"])
        self.assertEqual(payload["sources"]["mlflow_candidate_review_report"], str(mlflow_path))
        self.assertIn("MLflow Paper Candidate", markdown_text)

    def test_cli_returns_one_and_records_findings_for_blocked_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            freshness_path = write_json(root / "freshness.json", fresh_report(allowed=False, reasons=["missing_symbol"]))
            signal_path = write_json(root / "signal.json", signal_report())
            output = root / "audit.json"
            markdown = root / "audit.md"

            exit_code = main(
                [
                    "paper-audit",
                    "--freshness-report",
                    str(freshness_path),
                    "--signal-report",
                    str(signal_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready_for_paper_review"])
        self.assertIn("freshness_blocked", finding_codes(payload))

    def test_cli_missing_mlflow_candidate_review_report_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            freshness_path = write_json(root / "freshness.json", fresh_report())
            signal_path = write_json(root / "signal.json", signal_report())
            output = root / "audit.json"
            markdown = root / "audit.md"
            missing = root / "missing_mlflow.json"

            exit_code = main(
                [
                    "paper-audit",
                    "--freshness-report",
                    str(freshness_path),
                    "--signal-report",
                    str(signal_path),
                    "--mlflow-candidate-review-report",
                    str(missing),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertIn("mlflow_candidate_review_failed", finding_codes(payload))
        mlflow_findings = [
            finding
            for finding in payload["findings"]
            if finding["code"] == "mlflow_candidate_review_failed"
        ]
        self.assertIn("cannot read MLflow paper-candidate review report", mlflow_findings[0]["message"])


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def finding_codes(report: dict[str, object]) -> set[str]:
    return {str(finding["code"]) for finding in report["findings"]}  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
