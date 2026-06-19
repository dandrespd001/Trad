import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class PaperTrialDayTests(unittest.TestCase):
    def test_parser_accepts_trial_day_inputs(self) -> None:
        args = build_parser().parse_args(
            [
                "paper-trial-day",
                "--as-of-date",
                "2026-06-16",
                "--cycle",
                "/tmp/cycle.json",
                "--monitor",
                "/tmp/monitor.json",
                "--performance",
                "/tmp/performance.json",
                "--shadow-outcome",
                "/tmp/shadow.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.cycle, "/tmp/cycle.json")

    def test_clean_trial_day_writes_ok_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle = write_json(root / "cycle.json", {"state": "PAPER_CLOSED", "reasons": [], "safety": safe()})
            monitor = write_json(root / "monitor.json", {"status": "OK", "broker_snapshot": {"counts": {"orders": 0, "positions": 0}}, "safety": safe()})
            performance = write_json(root / "performance.json", {"status": "OK", "blockers": [], "paper_metrics": {"pending_closeouts": 0, "unmatched_closeouts": 0}, "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0}, "safety": safe()})
            shadow = write_json(root / "shadow.json", {"state": "RECORDED", "safety": safe()})

            exit_code = main(trial_args(root, cycle, monitor, performance, shadow))
            payload = read_json(root / "trial" / "2026-06-16" / "trial_day.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["trial_state"], "TRIAL_DAY_OK")
        self.assertTrue(payload["ready_for_next_trial_day"])
        self.assertEqual(payload["recovery_required"], False)
        self.assertFalse(payload["safety"]["live_trading_authorized"])

    def test_trial_day_requires_recovery_for_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle = write_json(root / "cycle.json", {"state": "BLOCKED", "reasons": ["require_clean_state_required"], "safety": safe()})
            monitor = write_json(root / "monitor.json", {"status": "CRITICAL", "broker_snapshot": {"counts": {"orders": 1, "positions": 0}}, "safety": safe()})
            performance = write_json(root / "performance.json", {"status": "WARN", "blockers": ["closeout_pending"], "paper_metrics": {"pending_closeouts": 1}, "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0}, "safety": safe()})
            shadow = write_json(root / "shadow.json", {"state": "BLOCKED", "reasons": ["missing_shadow_outcome_price"], "safety": safe()})

            exit_code = main(trial_args(root, cycle, monitor, performance, shadow))
            payload = read_json(root / "trial" / "2026-06-16" / "trial_day.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["trial_state"], "RECOVERY_REQUIRED")
        self.assertIn("cycle_blocked", payload["blockers"])
        self.assertIn("monitor_critical", payload["blockers"])
        self.assertIn("open_broker_orders", payload["blockers"])
        self.assertIn("closeout_pending", payload["blockers"])
        self.assertIn("shadow_outcome_blocked", payload["blockers"])

    def test_trial_day_returns_two_for_malformed_required_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cycle = root / "cycle.json"
            cycle.write_text("{bad json", encoding="utf-8")
            monitor = write_json(root / "monitor.json", {"status": "OK", "safety": safe()})
            performance = write_json(root / "performance.json", {"status": "OK", "safety": safe()})
            shadow = write_json(root / "shadow.json", {"state": "NO_SHADOW_SIGNAL", "safety": safe()})

            exit_code = main(trial_args(root, cycle, monitor, performance, shadow))
            payload = read_json(root / "trial" / "2026-06-16" / "trial_day.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["trial_state"], "ERROR")
        self.assertIn("artifact_read_error", payload["blockers"])


def trial_args(root: Path, cycle: Path, monitor: Path, performance: Path, shadow: Path) -> list[str]:
    return [
        "paper-trial-day",
        "--as-of-date",
        "2026-06-16",
        "--cycle",
        str(cycle),
        "--monitor",
        str(monitor),
        "--performance",
        str(performance),
        "--shadow-outcome",
        str(shadow),
        "--output-dir",
        str(root / "trial"),
    ]


def safe() -> dict[str, object]:
    return {"paper_only": True, "credentials_read": False, "orders_submitted": False, "live_trading_authorized": False}


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
