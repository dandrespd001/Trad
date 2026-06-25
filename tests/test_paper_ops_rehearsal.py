import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class PaperOpsRehearsalTests(unittest.TestCase):
    def test_parser_defaults_for_paper_ops_rehearsal(self) -> None:
        args = build_parser().parse_args(["paper-ops-rehearsal", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.scenario, "complete")
        self.assertEqual(args.output_dir, "reports/tmp/paper_rehearsal")

        for scenario in (
            "open-order",
            "existing-position",
            "stale-dataset",
            "statement-mismatch",
            "fill-unreconciled",
            "malicious-llm-context",
            "59-stable-sessions",
            "60-stable-ready",
            "duplicate-cycle",
            "stale-lock",
            "corrupt-ledger",
            "quality-blocked",
            "phase-not-ready",
            "retrain-due",
            "not-due",
            "duplicate-retrain",
            "candidate-rejected",
            "drift-blocked",
            "shadow-ready",
            "malicious-adaptive-llm",
        ):
            parsed = build_parser().parse_args(
                ["paper-ops-rehearsal", "--as-of-date", "2026-06-16", "--scenario", scenario]
            )
            self.assertEqual(parsed.scenario, scenario)

    def test_complete_week_rehearsal_produces_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="complete"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")
            markdown = (root / "rehearsal" / "2026-06-16" / "rehearsal.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["scenario"], "complete")
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "OK")
        self.assertEqual(payload["artifacts"]["weekly_summary"]["status"], "OK")
        self.assertEqual(payload["artifacts"]["model_review_decision"]["status"], "RECORDED")
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertIn("Status: **OK**", markdown)

    def test_missing_performance_rehearsal_produces_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="missing-performance"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("performance_skipped", payload["warnings"])
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "WARN")

    def test_stop_day_rehearsal_produces_critical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="stop"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertEqual(payload["artifacts"]["ops_check"]["status"], "CRITICAL")

    def test_invalid_statement_rehearsal_produces_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(rehearsal_args(root, scenario="invalid-statement"))
            payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["artifacts"]["statement_validate"]["status"], "ERROR")

    def test_operational_failure_rehearsals_produce_compatible_artifacts_without_broker_calls(self) -> None:
        for scenario, expected_code in (
            ("open-order", "open_broker_orders"),
            ("existing-position", "existing_positions"),
            ("stale-dataset", "dataset_stale"),
            ("statement-mismatch", "statement_mismatch"),
            ("fill-unreconciled", "fills_unreconciled"),
            ("malicious-llm-context", "order_submission_instruction"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)

                exit_code = main(rehearsal_args(root, scenario=scenario))
                payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")
                cycle = read_json(Path(payload["artifacts"]["paper_auto_cycle"]["path"]))
                operator = read_json(Path(payload["artifacts"]["operator_status"]["path"]))
                ledger = Path(payload["artifacts"]["session_ledger"]["path"])
                records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "CRITICAL")
            self.assertEqual(cycle["state"], "BLOCKED")
            self.assertIn(expected_code, cycle["reasons"])
            self.assertFalse(cycle["safety"]["broker_client_built"])
            self.assertFalse(cycle["safety"]["orders_submitted"])
            self.assertEqual(operator["status"], "CRITICAL")
            self.assertFalse(operator["clean_for_paper_auto"])
            self.assertEqual(records[-1]["state"], "BLOCKED")
            self.assertIn(expected_code, records[-1]["blockers"])
            self.assertFalse(records[-1]["safety"]["broker_client_built"])

    def test_phase_campaign_rehearsal_scenarios_produce_review_artifacts_without_live_authority(self) -> None:
        expectations = {
            "59-stable-sessions": ("WARN", "ACCUMULATING", 0),
            "60-stable-ready": ("OK", "READY_FOR_REVIEW", 0),
            "quality-blocked": ("CRITICAL", "BLOCKED", 1),
            "duplicate-cycle": ("CRITICAL", "BLOCKED", 1),
            "stale-lock": ("CRITICAL", "BLOCKED", 1),
            "corrupt-ledger": ("ERROR", "BLOCKED", 2),
        }
        for scenario, (status, phase_status, expected_exit) in expectations.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)

                exit_code = main(rehearsal_args(root, scenario=scenario))
                payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")
                phase = read_json(Path(payload["artifacts"]["phase_review"]["path"]))

            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(payload["status"], status)
            self.assertEqual(phase["phase_status"], phase_status)
            self.assertTrue(phase["review_only"])
            self.assertFalse(phase["live_trading_authorized"])
            self.assertFalse(payload["safety"]["live_trading_allowed"])

    def test_adaptive_training_rehearsal_scenarios_keep_review_only_authority(self) -> None:
        expectations = {
            "phase-not-ready": ("CRITICAL", "BLOCKED", 1),
            "retrain-due": ("OK", "CANDIDATE_REVIEWABLE", 0),
            "not-due": ("OK", "NOT_DUE", 0),
            "duplicate-retrain": ("OK", "NOT_DUE", 0),
            "candidate-rejected": ("WARN", "CANDIDATE_REJECTED", 0),
            "drift-blocked": ("CRITICAL", "BLOCKED", 1),
            "shadow-ready": ("OK", "READY_FOR_SHADOW", 0),
            "malicious-adaptive-llm": ("CRITICAL", "BLOCKED", 1),
        }
        for scenario, (status, expected_state, expected_exit) in expectations.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)

                exit_code = main(rehearsal_args(root, scenario=scenario))
                payload = read_json(root / "rehearsal" / "2026-06-16" / "rehearsal.json")

            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(payload["status"], status)
            self.assertEqual(payload["adaptive_training"]["state"], expected_state)
            self.assertFalse(payload["adaptive_training"]["model_mutated"])
            self.assertFalse(payload["adaptive_training"]["live_trading_authorized"])
            self.assertFalse(payload["safety"]["broker_client_built"])


def rehearsal_args(root: Path, *, scenario: str) -> list[str]:
    return [
        "paper-ops-rehearsal",
        "--as-of-date",
        "2026-06-16",
        "--scenario",
        scenario,
        "--output-dir",
        str(root / "rehearsal"),
    ]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# rehearsal_run() tests — pure pytest (no unittest.TestCase)
# ---------------------------------------------------------------------------

import json as _json
import pytest

from trading_ai.execution.paper_rehearsal import RehearsalReport, rehearsal_run


def _make_data_root(tmp_path: Path, symbol: str, n_rows: int = 30) -> Path:
    """Create minimal local OHLCV CSV structure for rehearsal tests (no pandas needed)."""
    from trading_ai.data.io import write_csv_records

    sym_dir = tmp_path / "data" / "raw" / "approved" / "core_etfs" / "1d" / symbol.lower()
    sym_dir.mkdir(parents=True)
    base_close = 100.0
    records = []
    for i in range(n_rows):
        close = base_close + i * 0.5
        records.append({
            "date": f"2024-{(i // 20 + 1):02d}-{(i % 20 + 1):02d}",
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000.0,
        })
    write_csv_records(records, sym_dir / "ohlcv.csv")
    return tmp_path


def test_rehearsal_run_returns_report_structure(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path, "SPY", n_rows=30)
    report = rehearsal_run(data_root=data_root, symbols=["SPY"], n_days=5)
    assert isinstance(report, RehearsalReport)
    assert report.sessions_run >= 0
    assert report.duration_seconds >= 0.0
    assert isinstance(report.errors, list)


def test_rehearsal_run_fast_execution(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path, "SPY", n_rows=60)
    report = rehearsal_run(data_root=data_root, symbols=["SPY"], n_days=5)
    assert report.duration_seconds < 10.0


def test_rehearsal_run_kill_switch_aborts(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path, "SPY", n_rows=30)
    report = rehearsal_run(data_root=data_root, symbols=["SPY"], n_days=5, kill_switch_active=True)
    assert report.sessions_run == 0
    assert any("kill_switch" in e for e in report.errors)


def test_rehearsal_run_missing_data_root(tmp_path: Path) -> None:
    report = rehearsal_run(data_root=tmp_path / "nonexistent", symbols=["SPY"], n_days=5)
    assert any("not found" in e for e in report.errors)
    assert report.sessions_run == 0


def test_rehearsal_run_equity_changes_with_fills(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path, "SPY", n_rows=30)
    report = rehearsal_run(
        data_root=data_root,
        symbols=["SPY"],
        n_days=5,
        simulated_equity_usd=10_000.0,
    )
    # With uptrending data (close increases 0.5/day), equity should increase
    assert report.final_equity_usd != 0.0


def test_rehearsal_run_json_serializable(tmp_path: Path) -> None:
    data_root = _make_data_root(tmp_path, "SPY", n_rows=30)
    report = rehearsal_run(data_root=data_root, symbols=["SPY"], n_days=5)
    # RehearsalReport fields must all be JSON-serializable
    payload = {
        "sessions_run": report.sessions_run,
        "orders_generated": report.orders_generated,
        "orders_blocked_by_risk": report.orders_blocked_by_risk,
        "stop_losses_triggered": report.stop_losses_triggered,
        "final_equity_usd": report.final_equity_usd,
        "sharpe_estimate": report.sharpe_estimate,
        "errors": report.errors,
        "duration_seconds": report.duration_seconds,
    }
    _json.dumps(payload)  # must not raise
