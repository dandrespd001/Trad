import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class TelegramControlInboxTests(unittest.TestCase):
    def test_parser_accepts_telegram_control_inbox_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "telegram-control-inbox",
                "--as-of-date",
                "2026-06-16",
                "--updates",
                "/tmp/updates.json",  # noqa: S108
                "--allowed-chat-id",
                "12345",
                "--allowed-user-id",
                "67890",
            ]
        )

        self.assertEqual(args.as_of_date, "2026-06-16")
        self.assertEqual(args.allowed_chat_id, ["12345"])
        self.assertEqual(args.allowed_user_id, ["67890"])
        self.assertEqual(args.environment, "paper")
        self.assertIsNone(args.state)

    def test_authorized_status_history_and_pause_create_audited_intents_without_broker_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates = write_json(
                root / "updates.json",
                {
                    "ok": True,
                    "result": [
                        update(10, chat_id="12345", user_id="67890", text="/status"),
                        update(11, chat_id="12345", user_id="67890", text="/history"),
                        update(12, chat_id="12345", user_id="67890", text="/pause news risk"),
                    ],
                },
            )

            exit_code = main(
                [
                    "telegram-control-inbox",
                    "--as-of-date",
                    "2026-06-16",
                    "--updates",
                    str(updates),
                    "--allowed-chat-id",
                    "12345",
                    "--allowed-user-id",
                    "67890",
                    "--output",
                    str(root / "control.json"),
                    "--ledger-output",
                    str(root / "control_ledger.jsonl"),
                ]
            )
            report = read_json(root / "control.json")
            ledger_rows = [json.loads(line) for line in (root / "control_ledger.jsonl").read_text().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(
            [intent["intent_type"] for intent in report["intents"]],
            ["STATUS_REQUESTED", "HISTORY_REQUESTED", "PAUSE_REQUESTED"],
        )
        self.assertFalse(report["intents"][0]["requires_confirmation"])
        self.assertFalse(report["intents"][1]["requires_confirmation"])
        self.assertTrue(report["intents"][2]["requires_confirmation"])
        self.assertFalse(report["safety"]["broker_client_built"])
        self.assertFalse(report["safety"]["orders_submitted"])
        self.assertEqual(len(ledger_rows), 3)
        self.assertEqual(ledger_rows[2]["intent_type"], "PAUSE_REQUESTED")

    def test_unauthorized_and_duplicate_updates_are_rejected_or_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates = write_json(
                root / "updates.json",
                {
                    "ok": True,
                    "result": [
                        update(8, chat_id="99999", user_id="67890", text="/status"),
                        update(9, chat_id="12345", user_id="00000", text="/pause no user"),
                        update(10, chat_id="12345", user_id="67890", text="/status"),
                        update(10, chat_id="12345", user_id="67890", text="/status"),
                    ],
                },
            )
            state = write_json(root / "state.json", {"last_update_id": 9, "processed_update_ids": [9]})

            exit_code = main(
                [
                    "telegram-control-inbox",
                    "--as-of-date",
                    "2026-06-16",
                    "--updates",
                    str(updates),
                    "--allowed-chat-id",
                    "12345",
                    "--allowed-user-id",
                    "67890",
                    "--state",
                    str(state),
                    "--output",
                    str(root / "control.json"),
                ]
            )
            report = read_json(root / "control.json")
            next_state = read_json(state)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(report["intents"]), 1)
        self.assertEqual(report["intents"][0]["source_update_id"], 10)
        self.assertIn("unauthorized_chat", report["rejected_updates"][0]["reason_codes"])
        self.assertIn("stale_or_duplicate_update", report["rejected_updates"][2]["reason_codes"])
        self.assertEqual(next_state["last_update_id"], 10)

    def test_trade_control_commands_emit_confirmation_required_intents_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updates = write_json(
                root / "updates.json",
                {
                    "ok": True,
                    "result": [
                        update(20, chat_id="12345", user_id="67890", text="/open SPY buy 1.00 breakout"),
                        update(21, chat_id="12345", user_id="67890", text="/close SPY end of day"),
                        update(22, chat_id="12345", user_id="67890", text="/restart operator reset"),
                    ],
                },
            )

            exit_code = main(
                [
                    "telegram-control-inbox",
                    "--as-of-date",
                    "2026-06-16",
                    "--updates",
                    str(updates),
                    "--allowed-chat-id",
                    "12345",
                    "--allowed-user-id",
                    "67890",
                    "--output",
                    str(root / "control.json"),
                ]
            )
            report = read_json(root / "control.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [intent["intent_type"] for intent in report["intents"]],
            ["OPEN_SIGNAL_REQUESTED", "FLATTEN_REQUESTED", "RESTART_REQUESTED"],
        )
        self.assertTrue(all(intent["requires_confirmation"] for intent in report["intents"]))
        self.assertTrue(all(intent["eligible_for_auto_execution"] is False for intent in report["intents"]))
        self.assertEqual(report["intents"][0]["symbol"], "SPY")
        self.assertEqual(report["intents"][0]["side"], "buy")
        self.assertEqual(report["authority"]["llm_authority"], "none")
        self.assertFalse(report["authority"]["orders_submitted"])


def update(update_id: int, *, chat_id: str, user_id: str, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_781_568_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "username": "operator"},
            "text": text,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
