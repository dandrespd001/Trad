import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperOperatorStatusTests(unittest.TestCase):
    def test_parser_defaults_for_operator_status_are_read_only(self) -> None:
        args = build_parser().parse_args(["paper-operator-status", "--as-of-date", "2026-06-16"])

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.cycle_root, "reports/tmp/paper_auto_cycle")
        self.assertEqual(args.ledger, "reports/tmp/paper_auto_cycle/session_ledger.jsonl")
        self.assertIsNone(args.monitor)
        self.assertIsNone(args.performance)
        self.assertIsNone(args.lock_dir)
        self.assertEqual(args.max_lock_age_minutes, 90)
        self.assertEqual(args.output_dir, "reports/tmp/paper_operator_status")

    def test_clean_operator_status_allows_next_paper_auto_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle_root = root / "paper_auto_cycle"
            ledger = root / "session_ledger.jsonl"
            write_cycle(cycle_root / "2026-06-16" / "cycle.json", state="NO_TRADE_REVIEW")
            append_record(ledger, state="PAPER_CLOSED", blockers=[])
            monitor = write_json(
                root / "monitor.json",
                {
                    "status": "OK",
                    "broker_snapshot": {"status": "DISABLED", "counts": {"orders": 0, "positions": 0}},
                    "alerts": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            performance = write_json(
                root / "performance.json",
                {
                    "status": "OK",
                    "paper_metrics": {"pending_closeouts": 0, "unmatched_closeouts": 0},
                    "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0},
                    "blockers": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )

            exit_code = main(operator_args(root, cycle_root, ledger, monitor=monitor, performance=performance))
            payload = read_json(root / "operator" / "2026-06-16" / "operator_status.json")
            markdown = (root / "operator" / "2026-06-16" / "operator_status.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["clean_for_paper_auto"])
        self.assertEqual(payload["ledger_summary"]["clean_sessions"], 1)
        self.assertEqual(payload["ledger_summary"]["blocked_sessions"], 0)
        self.assertEqual(payload["next_safe_action"], "run_confirmed_paper_auto_cycle")
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertIn("Clean for paper auto: `True`", markdown)

    def test_operator_status_blocks_open_orders_pending_closeout_and_statement_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle_root = root / "paper_auto_cycle"
            ledger = root / "session_ledger.jsonl"
            write_cycle(cycle_root / "2026-06-16" / "cycle.json", state="PAPER_SUBMITTED")
            append_record(ledger, state="BLOCKED", blockers=["dataset_stale"])
            monitor = write_json(
                root / "monitor.json",
                {
                    "status": "CRITICAL",
                    "broker_snapshot": {"status": "OK", "counts": {"orders": 1, "positions": 1}},
                    "alerts": [{"severity": "CRITICAL", "code": "paper_execution_without_closeout"}],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )
            performance = write_json(
                root / "performance.json",
                {
                    "status": "WARN",
                    "paper_metrics": {"pending_closeouts": 1, "unmatched_closeouts": 0},
                    "statement_reconciliation": {"status": "DIFFERENCES", "missing_fills": 1},
                    "blockers": ["statement_missing_fill"],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )

            exit_code = main(operator_args(root, cycle_root, ledger, monitor=monitor, performance=performance))
            payload = read_json(root / "operator" / "2026-06-16" / "operator_status.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertFalse(payload["clean_for_paper_auto"])
        codes = {item["code"] for item in payload["blockers"]}
        self.assertIn("open_broker_orders", codes)
        self.assertIn("existing_positions", codes)
        self.assertIn("closeout_pending", codes)
        self.assertIn("statement_mismatch", codes)
        self.assertIn("fills_unreconciled", codes)
        self.assertEqual(payload["closeout_status"], "PENDING")
        self.assertEqual(payload["statement_status"], "DIFFERENCES")
        self.assertEqual(payload["unreconciled_fills"], 1)
        self.assertEqual(payload["next_safe_action"], "resolve_operator_blockers")

    def test_operator_status_blocks_statement_pending_when_fills_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle_root = root / "paper_auto_cycle"
            ledger = root / "session_ledger.jsonl"
            write_cycle(cycle_root / "2026-06-16" / "cycle.json", state="NO_TRADE_REVIEW")
            append_record(ledger, state="PAPER_CLOSED", blockers=[])
            performance = write_json(
                root / "performance.json",
                {
                    "status": "WARN",
                    "paper_metrics": {"fills": 1, "pending_closeouts": 0, "unmatched_closeouts": 0},
                    "statement_status": {
                        "status": "STATEMENT_PENDING",
                        "statement_present": False,
                        "unreconciled_fills": 0,
                    },
                    "statement_reconciliation": {"status": "NOT_REQUESTED", "missing_fills": 0, "local_fills": 1},
                    "blockers": [],
                    "safety": {"paper_only": True, "live_trading_authorized": False},
                },
            )

            exit_code = main(operator_args(root, cycle_root, ledger, performance=performance))
            payload = read_json(root / "operator" / "2026-06-16" / "operator_status.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "CRITICAL")
        self.assertFalse(payload["clean_for_paper_auto"])
        self.assertEqual(payload["statement_status"], "STATEMENT_PENDING")
        self.assertIn("statement_pending", {item["code"] for item in payload["blockers"]})

    def test_operator_status_reports_active_and_stale_cron_locks_without_removing_them(self) -> None:
        for age_seconds, expected_status, expected_code in (
            (60, "ACTIVE", "cycle_lock_active"),
            (7200, "STALE", "cycle_lock_stale"),
        ):
            with self.subTest(expected_status=expected_status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                cycle_root = root / "paper_auto_cycle"
                ledger = root / "session_ledger.jsonl"
                write_cycle(cycle_root / "2026-06-16" / "cycle.json", state="NO_TRADE_REVIEW")
                append_record(ledger, state="PAPER_CLOSED", blockers=[])
                lock_dir = root / "locks"
                lock_dir.mkdir()
                lock_path = lock_dir / "paper_auto_cycle_2026-06-16.lock"
                lock_path.write_text("generated_at=2026-06-16T10:00:00+00:00\n", encoding="utf-8")
                timestamp = time.time() - age_seconds
                os.utime(lock_path, (timestamp, timestamp))

                exit_code = main(
                    operator_args(root, cycle_root, ledger)
                    + ["--lock-dir", str(lock_dir), "--max-lock-age-minutes", "90"]
                )
                payload = read_json(root / "operator" / "2026-06-16" / "operator_status.json")
                lock_exists = lock_path.exists()

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["lock_status"], expected_status)
            self.assertTrue(lock_exists)
            self.assertIn(expected_code, {item["code"] for item in payload["blockers"]})


def operator_args(
    root: Path,
    cycle_root: Path,
    ledger: Path,
    *,
    monitor: Path | None = None,
    performance: Path | None = None,
) -> list[str]:
    args = [
        "paper-operator-status",
        "--as-of-date",
        "2026-06-16",
        "--cycle-root",
        str(cycle_root),
        "--ledger",
        str(ledger),
        "--output-dir",
        str(root / "operator"),
    ]
    if monitor is not None:
        args.extend(["--monitor", str(monitor)])
    if performance is not None:
        args.extend(["--performance", str(performance)])
    return args


def write_cycle(path: Path, *, state: str) -> Path:
    return write_json(
        path,
        {
            "schema_version": "1.0",
            "generated_at": "2026-06-16T10:00:00+00:00",
            "as_of_date": "2026-06-16",
            "state": state,
            "exit_code": 0,
            "reasons": [],
            "artifacts": {},
            "safety": {"paper_only": True, "live_trading_authorized": False},
        },
    )


def append_record(path: Path, *, state: str, blockers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_type": "paper_auto_cycle_session",
        "session_id": f"paper-auto-2026-06-16-{state.lower()}",
        "generated_at": "2026-06-16T10:00:00+00:00",
        "as_of_date": "2026-06-16",
        "state": state,
        "exit_code": 0 if state != "BLOCKED" else 1,
        "confirm_paper_auto": True,
        "blockers": blockers,
        "safety": {"paper_only": True, "live_trading_authorized": False},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
