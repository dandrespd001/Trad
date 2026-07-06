import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main
from trading_ai.execution.paper_risk_state import RiskState, load_risk_state, save_risk_state
from trading_ai.execution.paper_signal_approval import compute_plan_hash, load_signal_approval_registry


class TelegramControlApplyTests(unittest.TestCase):
    def test_parser_accepts_telegram_control_apply_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "telegram-control-apply",
                "--as-of-date",
                "2026-06-16",
                "--inbox",
                "/tmp/control.json",  # noqa: S108
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.inbox, "/tmp/control.json")  # noqa: S108
        self.assertFalse(args.confirm_telegram_control)
        self.assertIsNone(args.state)
        self.assertIsNone(args.status_report)
        self.assertIsNone(args.history_report)

    def test_status_and_history_intents_attach_local_telegram_responses_without_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            status_report = write_json(
                root / "telegram_status.json",
                {
                    "schema_version": "1.0",
                    "as_of_date": "2026-06-16",
                    "status": "OK",
                    "message": "Paper trading 2026-06-16\nStatus: OK\nOpen positions: none",
                    "telegram": {"send_enabled": False, "sent": False, "parse_mode": "plain_text"},
                    "safety": {
                        "paper_only": True,
                        "broker_client_built": False,
                        "credentials_read": False,
                        "orders_submitted": False,
                        "live_trading_authorized": False,
                        "live_trading_allowed": False,
                    },
                },
            )
            history_report = write_json(
                root / "telegram_history.json",
                {
                    "schema_version": "1.0",
                    "as_of_date": "2026-06-16",
                    "status": "OK",
                    "message": "Paper trading history 2026-06-16\nLedger: events=3",
                    "telegram": {"send_enabled": False, "sent": False, "parse_mode": "plain_text"},
                    "safety": {
                        "paper_only": True,
                        "broker_client_built": False,
                        "credentials_read": False,
                        "orders_submitted": False,
                        "live_trading_authorized": False,
                        "live_trading_allowed": False,
                    },
                },
            )
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent("tgctl-status", "STATUS_REQUESTED", requires_confirmation=False),
                        intent("tgctl-history", "HISTORY_REQUESTED", requires_confirmation=False),
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--status-report",
                    str(status_report),
                    "--history-report",
                    str(history_report),
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual([decision["action"] for decision in report["decisions"]], ["STATUS_RESPONSE", "HISTORY_RESPONSE"])
        self.assertEqual(report["decisions"][0]["telegram_response"]["message"], "Paper trading 2026-06-16\nStatus: OK\nOpen positions: none")
        self.assertEqual(report["decisions"][1]["telegram_response"]["message"], "Paper trading history 2026-06-16\nLedger: events=3")
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_pause_intent_with_confirmation_latches_paper_kill_switch_without_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(as_of_date="2026-06-16", kill_switch_active=False), risk_state)
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent(
                            "tgctl-pause",
                            "PAUSE_REQUESTED",
                            reason="news risk",
                            requires_confirmation=True,
                        )
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--confirm-telegram-control",
                    "--output",
                    str(root / "apply.json"),
                    "--ledger-output",
                    str(root / "apply_ledger.jsonl"),
                ]
            )
            report = read_json(root / "apply.json")
            saved_state = load_risk_state(risk_state)
            ledger_rows = [json.loads(line) for line in (root / "apply_ledger.jsonl").read_text().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["decisions"][0]["decision"], "APPLIED")
        self.assertEqual(report["decisions"][0]["action"], "KILL_SWITCH_TRIPPED")
        self.assertTrue(saved_state.kill_switch_active)
        self.assertEqual(saved_state.kill_switch_reason, "telegram_pause:news risk")
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual(ledger_rows[0]["intent_id"], "tgctl-pause")

    def test_resume_intent_without_confirmation_blocks_and_keeps_kill_switch_latched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            risk_state = root / "risk_state.json"
            save_risk_state(
                RiskState(
                    as_of_date="2026-06-16",
                    kill_switch_active=True,
                    kill_switch_reason="telegram_pause:news risk",
                ),
                risk_state,
            )
            inbox = write_json(
                root / "control.json",
                inbox_payload([intent("tgctl-resume", "RESUME_REQUESTED", requires_confirmation=True)]),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")
            saved_state = load_risk_state(risk_state)

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["decisions"][0]["decision"], "BLOCKED")
        self.assertIn("confirmation_required", report["decisions"][0]["reason_codes"])
        self.assertTrue(saved_state.kill_switch_active)

    def test_routed_control_intents_emit_machine_readable_routes_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(as_of_date="2026-06-16"), risk_state)
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent(
                            "tgctl-open",
                            "OPEN_SIGNAL_REQUESTED",
                            symbol="SPY",
                            side="buy",
                            notional=1.0,
                            requires_confirmation=True,
                        ),
                        intent(
                            "tgctl-signal",
                            "SIGNAL_REQUESTED",
                            symbol="QQQ",
                            side="hold",
                            requires_confirmation=True,
                        ),
                        intent(
                            "tgctl-close",
                            "FLATTEN_REQUESTED",
                            symbol="SPY",
                            requires_confirmation=True,
                        ),
                        intent(
                            "tgctl-restart",
                            "RESTART_REQUESTED",
                            requires_confirmation=True,
                        ),
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--confirm-telegram-control",
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [decision["action"] for decision in report["decisions"]],
            [
                "ROUTE_TO_SIGNAL_GATE",
                "ROUTE_TO_SIGNAL_GATE",
                "ROUTE_TO_FLATTEN_GATE",
                "ROUTE_TO_OPERATOR_RESTART",
            ],
        )
        self.assertTrue(all(decision["decision"] == "ROUTED" for decision in report["decisions"]))
        self.assertIn("paper-signal-arbitration", report["decisions"][0]["suggested_next_command"])
        self.assertIn("paper-safe-flatten", report["decisions"][2]["suggested_next_command"])
        self.assertEqual(report["decisions"][0]["route"]["gate"], "paper_signal_arbitration")
        self.assertEqual(report["decisions"][0]["route"]["symbol"], "SPY")
        self.assertEqual(report["decisions"][0]["route"]["side"], "buy")
        self.assertEqual(report["decisions"][0]["route"]["notional"], 1.0)
        self.assertEqual(report["decisions"][1]["route"]["gate"], "paper_signal_arbitration")
        self.assertEqual(report["decisions"][1]["route"]["symbol"], "QQQ")
        self.assertEqual(report["decisions"][1]["route"]["side"], "hold")
        self.assertEqual(report["decisions"][2]["route"]["gate"], "paper_safe_flatten")
        self.assertEqual(report["decisions"][2]["route"]["symbol"], "SPY")
        self.assertEqual(report["decisions"][3]["route"]["gate"], "paper_auto_cycle")
        self.assertIn("--confirm-paper-auto", report["decisions"][3]["route"]["args"])
        self.assertTrue(all(decision["route"]["paper_only"] for decision in report["decisions"]))
        self.assertTrue(all(decision["route"]["orders_submitted"] is False for decision in report["decisions"]))
        self.assertFalse(report["authority"]["orders_submitted"])
        self.assertFalse(report["safety"]["broker_client_built"])

    def test_approve_and_veto_intents_are_applied_and_written_to_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = signal_plan_fixture()
            plan_path = write_json(root / "signal_plan.json", plan)
            plan_hash = compute_plan_hash(plan)
            registry_dir = root / "signal_approval"
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(as_of_date="2026-06-16"), risk_state)
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent(
                            "tgctl-approve",
                            "APPROVE_SIGNAL_PLAN_REQUESTED",
                            requires_confirmation=False,
                            extra={"plan_hash_prefix": plan_hash[:8]},
                        ),
                        intent(
                            "tgctl-veto",
                            "VETO_SIGNAL_PLAN_REQUESTED",
                            requires_confirmation=False,
                            extra={"plan_hash_prefix": plan_hash[:8], "veto_reason": "news risk"},
                        ),
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--signal-plan",
                    str(plan_path),
                    "--signal-approval-registry-dir",
                    str(registry_dir),
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")
            registry = load_signal_approval_registry("2026-06-16", registry_dir=registry_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(
            [decision["decision"] for decision in report["decisions"]],
            ["APPLIED", "APPLIED"],
        )
        self.assertEqual(report["decisions"][0]["action"], "SIGNAL_PLAN_APPROVED")
        self.assertEqual(report["decisions"][1]["action"], "SIGNAL_PLAN_VETOED")
        self.assertFalse(registry["fail_closed"])
        self.assertEqual(len(registry["records"]), 2)
        self.assertEqual(registry["records"][0]["verdict"], "approved")
        self.assertEqual(registry["records"][1]["verdict"], "vetoed")
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])

    def test_approve_intent_without_signal_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(as_of_date="2026-06-16"), risk_state)
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent(
                            "tgctl-approve",
                            "APPROVE_SIGNAL_PLAN_REQUESTED",
                            requires_confirmation=False,
                            extra={"plan_hash_prefix": "deadbeef"},
                        )
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["decisions"][0]["decision"], "BLOCKED")
        self.assertEqual(report["decisions"][0]["action"], "REJECT_INTENT")
        self.assertIn("signal_plan_artifact_missing", report["decisions"][0]["reason_codes"])

    def test_approve_intent_with_mismatched_plan_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = signal_plan_fixture()
            plan_path = write_json(root / "signal_plan.json", plan)
            registry_dir = root / "signal_approval"
            risk_state = root / "risk_state.json"
            save_risk_state(RiskState(as_of_date="2026-06-16"), risk_state)
            inbox = write_json(
                root / "control.json",
                inbox_payload(
                    [
                        intent(
                            "tgctl-approve",
                            "APPROVE_SIGNAL_PLAN_REQUESTED",
                            requires_confirmation=False,
                            extra={"plan_hash_prefix": "deadbeef"},
                        )
                    ]
                ),
            )

            exit_code = main(
                [
                    "telegram-control-apply",
                    "--as-of-date",
                    "2026-06-16",
                    "--inbox",
                    str(inbox),
                    "--risk-state-path",
                    str(risk_state),
                    "--signal-plan",
                    str(plan_path),
                    "--signal-approval-registry-dir",
                    str(registry_dir),
                    "--output",
                    str(root / "apply.json"),
                ]
            )
            report = read_json(root / "apply.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["decisions"][0]["decision"], "BLOCKED")
        self.assertEqual(report["decisions"][0]["action"], "REJECT_INTENT")
        self.assertIn("plan_hash_mismatch", report["decisions"][0]["reason_codes"])


def signal_plan_fixture() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-16T14:00:00+00:00",
        "as_of_date": "2026-06-16",
        "decision": "ELIGIBLE_FOR_PAPER",
        "eligible_for_paper": True,
        "selected_symbol": "SPY",
    }


def inbox_payload(intents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "OK",
        "as_of_date": "2026-06-16",
        "environment": "paper",
        "intents": intents,
        "safety": {"orders_submitted": False, "broker_client_built": False},
        "authority": {"llm_authority": "none", "orders_submitted": False},
    }


def intent(
    intent_id: str,
    intent_type: str,
    *,
    symbol: str | None = None,
    side: str | None = None,
    notional: float | None = None,
    reason: str = "operator requested control action",
    requires_confirmation: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "intent_id": intent_id,
        "intent_type": intent_type,
        "as_of_date": "2026-06-16",
        "environment": "paper",
        "user_id": "67890",
        "chat_id": "12345",
        "source_update_id": 123,
        "requires_confirmation": requires_confirmation,
        "eligible_for_auto_execution": False,
        "reason": reason,
        "broker_client_built": False,
        "orders_submitted": False,
    }
    if symbol is not None:
        payload["symbol"] = symbol
    if side is not None:
        payload["side"] = side
    if notional is not None:
        payload["notional"] = notional
    if extra:
        payload.update(extra)
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
