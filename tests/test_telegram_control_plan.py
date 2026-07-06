import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class TelegramControlPlanTests(unittest.TestCase):
    def test_parser_accepts_telegram_control_plan_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "telegram-control-plan",
                "--as-of-date",
                "2026-06-16",
                "--apply",
                "reports/tmp/telegram_control/apply.json",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.apply, "reports/tmp/telegram_control/apply.json")
        self.assertEqual(args.output, "reports/tmp/telegram_control/plan.json")
        self.assertIsNone(args.ledger_output)

    def test_control_plan_materializes_routed_steps_without_executing_broker_or_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            apply_report = write_json(
                root / "apply.json",
                apply_payload(
                    [
                        routed_decision(
                            intent_id="tgctl-open",
                            intent_type="OPEN_SIGNAL_REQUESTED",
                            gate="paper_signal_arbitration",
                            command="trading-ai paper-signal-arbitration",
                            args=["--as-of-date", "2026-06-16", "--readiness", "reports/tmp/paper/readiness.json"],
                            symbol="SPY",
                            side="buy",
                            notional=1.0,
                        ),
                        routed_decision(
                            intent_id="tgctl-flatten",
                            intent_type="FLATTEN_REQUESTED",
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
                    "telegram-control-plan",
                    "--as-of-date",
                    "2026-06-16",
                    "--apply",
                    str(apply_report),
                    "--output",
                    str(root / "plan.json"),
                    "--ledger-output",
                    str(root / "plan.jsonl"),
                ]
            )
            report = read_json(root / "plan.json")
            ledger_rows = [json.loads(line) for line in (root / "plan.jsonl").read_text().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["summary"]["route_count"], 2)
        self.assertEqual([step["gate"] for step in report["steps"]], ["paper_signal_arbitration", "paper_safe_flatten"])
        self.assertEqual(report["steps"][0]["status"], "PENDING_OPERATOR_GATE")
        self.assertEqual(report["steps"][0]["symbol"], "SPY")
        self.assertEqual(report["steps"][0]["side"], "buy")
        self.assertEqual(report["steps"][0]["notional"], 1.0)
        self.assertEqual(report["steps"][0]["argv"][0], "trading-ai paper-signal-arbitration")
        self.assertIn("--readiness", report["steps"][0]["argv"])
        self.assertEqual(report["steps"][1]["argv"][0], "trading-ai paper-safe-flatten")
        self.assertTrue(all(step["paper_only"] for step in report["steps"]))
        self.assertTrue(all(step["orders_submitted"] is False for step in report["steps"]))
        self.assertTrue(all(step["broker_client_built"] is False for step in report["steps"]))
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual([row["record_type"] for row in ledger_rows], ["telegram_control_plan_step", "telegram_control_plan_step"])

    def test_control_plan_blocks_unsafe_or_stale_apply_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            apply_report = write_json(
                root / "apply.json",
                {
                    **apply_payload(
                        [
                            routed_decision(
                                intent_id="tgctl-open",
                                intent_type="OPEN_SIGNAL_REQUESTED",
                                gate="paper_signal_arbitration",
                                command="trading-ai paper-signal-arbitration",
                                args=["--as-of-date", "2026-06-15"],
                                symbol="SPY",
                                route_overrides={"orders_submitted": True},
                            )
                        ]
                    ),
                    "as_of_date": "2026-06-15",
                },
            )

            exit_code = main(
                [
                    "telegram-control-plan",
                    "--as-of-date",
                    "2026-06-16",
                    "--apply",
                    str(apply_report),
                    "--output",
                    str(root / "plan.json"),
                ]
            )
            report = read_json(root / "plan.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("apply_as_of_date_mismatch", report["blockers"])
        self.assertIn("route_orders_submitted:tgctl-open", report["blockers"])
        self.assertEqual(report["steps"], [])


def apply_payload(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T20:00:00+00:00",
        "as_of_date": "2026-06-16",
        "environment": "paper",
        "status": "OK",
        "source": {"inbox_path": "control.json"},
        "decisions": decisions,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
            "live_trading_allowed": False,
        },
        "authority": {
            "control_plane": "telegram_intents_only",
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
    }


def routed_decision(
    *,
    intent_id: str,
    intent_type: str,
    gate: str,
    command: str,
    args: list[str],
    symbol: str | None = None,
    side: str | None = None,
    notional: float | None = None,
    route_overrides: dict[str, object] | None = None,
) -> dict[str, Any]:
    route = {
        "gate": gate,
        "command": command,
        "args": args,
        "intent_id": intent_id,
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "requires_operator_confirmation": True,
        "paper_only": True,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }
    if route_overrides:
        route.update(route_overrides)
    return {
        "intent_id": intent_id,
        "intent_type": intent_type,
        "decision": "ROUTED",
        "action": "ROUTE_TO_SIGNAL_GATE" if gate == "paper_signal_arbitration" else "ROUTE_TO_FLATTEN_GATE",
        "reason_codes": [],
        "requires_confirmation": True,
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "risk_changed": False,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
        "suggested_next_command": command + " " + " ".join(args),
        "route": route,
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
