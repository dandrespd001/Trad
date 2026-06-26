import contextlib
import hashlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.execution.live_execute_session import run_live_execute_session


class LiveExecuteSessionTests(unittest.TestCase):
    def test_parser_accepts_dry_run_only_inputs_and_no_submit_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "live-execute-session",
                "--as-of-date",
                "2026-06-16",
                "--readiness",
                "readiness.json",
                "--risk",
                "risk.yml",
                "--reviewer",
                "ops",
                "--reason",
                "dry-run rehearsal",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertTrue(args.dry_run)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "live-execute-session",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    "readiness.json",
                    "--risk",
                    "risk.yml",
                    "--reviewer",
                    "ops",
                    "--reason",
                    "dry-run rehearsal",
                    "--submit-real",
                ]
            )
        self.assertIn("unrecognized arguments: --submit-real", stderr.getvalue())

    def test_blocks_missing_readiness_but_writes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            risk = write_risk(root / "risk.yml")

            result = run_live_execute_session(
                as_of_date="2026-06-16",
                readiness=root / "missing.json",
                risk=risk,
                reviewer="ops",
                reason="dry-run rehearsal",
                output_dir=root / "live_execute",
            )

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("readiness_read_error", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])

    def test_blocks_readiness_not_ready_hash_mismatch_missing_human_context_and_dry_run_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_json(root / "readiness.json", readiness_payload("BLOCKED"))
            risk = write_risk(root / "risk.yml")
            readiness_hash = sha256_file(readiness)

            result = run_live_execute_session(
                as_of_date="2026-06-16",
                readiness=readiness,
                risk=risk,
                reviewer="",
                reason="",
                expected_readiness_hash="0" * 64,
                dry_run=False,
                output_dir=root / "live_execute",
            )
            payload = read_json(root / "live_execute" / "2026-06-16" / "live_execute_session.json")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("readiness_not_ready", payload["blockers"])
        self.assertIn("readiness_hash_mismatch", payload["blockers"])
        self.assertIn("human_review_required", payload["blockers"])
        self.assertIn("dry_run_required", payload["blockers"])
        self.assertEqual(payload["readiness_hash"], readiness_hash)
        self.assertFalse(payload["safety"]["orders_submitted"])

    def test_happy_path_dry_run_writes_command_evidence_without_building_live_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_json(root / "readiness.json", readiness_payload("READY_FOR_LIVE_CANARY"))
            risk = write_risk(root / "risk.yml")
            readiness_hash = sha256_file(readiness)
            risk_hash = sha256_file(risk)

            result = run_live_execute_session(
                as_of_date="2026-06-16",
                readiness=readiness,
                risk=risk,
                reviewer="ops",
                reason="readiness reviewed for dry-run",
                expected_readiness_hash=readiness_hash,
                command_evidence=["trading-ai live-execute-session --dry-run"],
                output_dir=root / "live_execute",
            )
            payload = read_json(root / "live_execute" / "2026-06-16" / "live_execute_session.json")
            markdown = (root / "live_execute" / "2026-06-16" / "live_execute_session.md").read_text(
                encoding="utf-8"
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(payload["status"], "DRY_RUN_READY")
        self.assertEqual(payload["readiness_hash"], readiness_hash)
        self.assertEqual(payload["risk_hash"], risk_hash)
        self.assertEqual(payload["reviewer"], "ops")
        self.assertEqual(payload["reason"], "readiness reviewed for dry-run")
        self.assertEqual(payload["command_evidence"], ["trading-ai live-execute-session --dry-run"])
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["credentials_read"])
        self.assertFalse(payload["safety"]["live_trading_authorized"])
        self.assertIn("Live Execute Session", markdown)

    def test_cli_writes_dry_run_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_json(root / "readiness.json", readiness_payload("READY_FOR_LIVE_CANARY"))
            risk = write_risk(root / "risk.yml")

            exit_code = main(
                [
                    "live-execute-session",
                    "--as-of-date",
                    "2026-06-16",
                    "--readiness",
                    str(readiness),
                    "--risk",
                    str(risk),
                    "--expected-readiness-hash",
                    sha256_file(readiness),
                    "--reviewer",
                    "ops",
                    "--reason",
                    "cli dry-run",
                    "--output-dir",
                    str(root / "live_execute"),
                ]
            )
            payload = read_json(root / "live_execute" / "2026-06-16" / "live_execute_session.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "DRY_RUN_READY")
        self.assertFalse(payload["safety"]["orders_submitted"])

    def test_breaker_missing_blocks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            readiness = write_json(root / "readiness.json", readiness_payload("READY_FOR_LIVE_CANARY"))
            risk = write_risk(root / "risk.yml")

            result = run_live_execute_session(
                as_of_date="2026-06-16",
                readiness=readiness,
                risk=risk,
                reviewer="ops",
                reason="dry-run rehearsal",
                expected_readiness_hash=sha256_file(readiness),
                breaker_state_path=root / "missing_breaker.json",
                output_dir=root / "live_execute",
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("breaker_tripped:breaker_missing_fail_closed", result.payload["blockers"])
        self.assertFalse(result.payload["safety"]["orders_submitted"])


def readiness_payload(state: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "as_of_date": "2026-06-16",
        "live_readiness_state": state,
        "status": "OK" if state == "READY_FOR_LIVE_CANARY" else "BLOCKED",
        "reviewer": "ops",
        "reason": "paper trial complete",
        "blockers": [] if state == "READY_FOR_LIVE_CANARY" else ["paper_evidence_not_ready"],
        "safety": {
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
            "live_trading_allowed": False,
        },
    }


def write_risk(path: Path) -> Path:
    path.write_text(
        textwrap.dedent(
            """
            risk_limits:
              max_daily_loss_pct: 0.02
              max_drawdown_pct: 0.10
              max_gross_exposure: 1.0
              max_single_position: 0.30
              paper_stage: CANARY
              paper_notional_usd: 1.0
              live_trading_allowed: false
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
