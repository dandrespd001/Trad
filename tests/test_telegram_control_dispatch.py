import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class TelegramControlDispatchTests(unittest.TestCase):
    def test_parser_accepts_telegram_control_dispatch_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "telegram-control-dispatch",
                "--as-of-date",
                "2026-06-16",
                "--plan",
                "reports/tmp/telegram_control/plan.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.plan, "reports/tmp/telegram_control/plan.json")
        self.assertEqual(args.output, "reports/tmp/telegram_control/dispatch.json")
        self.assertIsNone(args.ledger_output)
        self.assertTrue(args.dry_run)

    def test_dispatch_dry_run_marks_allowed_steps_ready_without_executing_commands_or_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = write_json(
                root / "plan.json",
                plan_payload(
                    [
                        step(
                            step_id="telegram-control-route-001",
                            intent_id="tgctl-open",
                            gate="paper_signal_arbitration",
                            command="trading-ai paper-signal-arbitration",
                            args=["--as-of-date", "2026-06-16", "--readiness", "reports/tmp/paper/readiness.json"],
                            symbol="SPY",
                            side="buy",
                            notional=1.0,
                        ),
                        step(
                            step_id="telegram-control-route-002",
                            intent_id="tgctl-flatten",
                            gate="paper_safe_flatten",
                            command="trading-ai paper-safe-flatten",
                            args=["--as-of-date", "2026-06-16", "--confirm-paper", "--confirm-flatten"],
                            symbol="SPY",
                        ),
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-dispatch",
                    "--as-of-date",
                    "2026-06-16",
                    "--plan",
                    str(plan),
                    "--output",
                    str(root / "dispatch.json"),
                    "--ledger-output",
                    str(root / "dispatch.jsonl"),
                ]
            )
            report = read_json(root / "dispatch.json")
            ledger_rows = [json.loads(line) for line in (root / "dispatch.jsonl").read_text().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["summary"]["ready_count"], 2)
        self.assertEqual(report["summary"]["blocked_count"], 0)
        self.assertEqual([decision["status"] for decision in report["decisions"]], ["READY_FOR_OPERATOR", "READY_FOR_OPERATOR"])
        self.assertEqual(report["decisions"][0]["argv"][0], "trading-ai paper-signal-arbitration")
        self.assertIn("--readiness", report["decisions"][0]["argv"])
        self.assertEqual(report["decisions"][1]["gate"], "paper_safe_flatten")
        self.assertTrue(all(decision["dry_run"] for decision in report["decisions"]))
        self.assertTrue(all(decision["subprocess_started"] is False for decision in report["decisions"]))
        self.assertTrue(all(decision["orders_submitted"] is False for decision in report["decisions"]))
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual(
            [row["record_type"] for row in ledger_rows],
            ["telegram_control_dispatch_decision", "telegram_control_dispatch_decision"],
        )

    def test_dispatch_blocks_unknown_gate_or_stale_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = write_json(
                root / "plan.json",
                {
                    **plan_payload(
                        [
                            step(
                                step_id="telegram-control-route-001",
                                intent_id="tgctl-open",
                                gate="paper-live-execute",
                                command="trading-ai live-execute-session",
                                args=["--as-of-date", "2026-06-15"],
                                symbol="SPY",
                            )
                        ]
                    ),
                    "as_of_date": "2026-06-15",
                },
            )

            exit_code = main(
                [
                    "telegram-control-dispatch",
                    "--as-of-date",
                    "2026-06-16",
                    "--plan",
                    str(plan),
                    "--output",
                    str(root / "dispatch.json"),
                ]
            )
            report = read_json(root / "dispatch.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("plan_as_of_date_mismatch", report["blockers"])
        self.assertEqual(report["decisions"][0]["status"], "BLOCKED")
        self.assertIn("gate_not_allowed", report["decisions"][0]["reason_codes"])
        self.assertFalse(report["decisions"][0]["subprocess_started"])
        self.assertFalse(report["safety"]["orders_submitted"])


def plan_payload(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T20:05:00+00:00",
        "as_of_date": "2026-06-16",
        "environment": "paper",
        "status": "OK",
        "source": {"apply_path": "apply.json"},
        "summary": {"route_count": len(steps), "skipped_decision_count": 0, "blocker_count": 0},
        "steps": steps,
        "skipped_decisions": [],
        "blockers": [],
        "authority": {
            "control_plane": "telegram_intents_only",
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
            "live_trading_allowed": False,
        },
    }


def step(
    *,
    step_id: str,
    intent_id: str,
    gate: str,
    command: str,
    args: list[str],
    symbol: str | None = None,
    side: str | None = None,
    notional: float | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "status": "PENDING_OPERATOR_GATE",
        "intent_id": intent_id,
        "intent_type": "OPEN_SIGNAL_REQUESTED",
        "gate": gate,
        "command": command,
        "args": args,
        "argv": [command, *args],
        "suggested_next_command": command + " " + " ".join(args),
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "requires_operator_confirmation": True,
        "paper_only": True,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
