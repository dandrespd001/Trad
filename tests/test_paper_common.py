import json
import tempfile
import unittest
from pathlib import Path
from datetime import date

from trading_ai.execution.paper_common import (
    as_of_date_to_date,
    as_of_date_to_iso,
    paper_exit_code,
    read_json_artifact,
    read_text_artifact,
    redact_secrets,
    reason_codes,
    write_json_artifact,
    write_text_artifact,
)


class PaperCommonTests(unittest.TestCase):
    def test_json_and_text_helpers_create_dirs_with_stable_encoding_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            json_path = root / "nested" / "artifact.json"
            text_path = root / "nested" / "artifact.md"

            write_json_artifact({"z": 1, "a": {"b": 2}}, json_path)
            write_text_artifact("# Paper\n", text_path)

            raw_json = json_path.read_text(encoding="utf-8")
            raw_text = text_path.read_text(encoding="utf-8")
            loaded = read_json_artifact(json_path)
            loaded_text = read_text_artifact(text_path)

        self.assertEqual(raw_json, json.dumps({"z": 1, "a": {"b": 2}}, indent=2, sort_keys=True))
        self.assertEqual(raw_text, "# Paper\n")
        self.assertEqual(loaded, {"a": {"b": 2}, "z": 1})
        self.assertEqual(loaded_text, "# Paper\n")

    def test_redact_secrets_covers_broker_telegram_and_api_token_shapes(self) -> None:
        text = (
            "api_key=KEY secret_key=SECRET secret=PLAINSECRET "
            "https://api.telegram.org/botTELEGRAMTOKEN/sendMessage "
            "Authorization: Bearer sk-proj-live-secret"
        )

        redacted = redact_secrets(
            text,
            env={
                "ALPACA_PAPER_API_KEY": "KEY",
                "ALPACA_PAPER_SECRET_KEY": "SECRET",
                "TELEGRAM_BOT_TOKEN": "TELEGRAMTOKEN",
            },
        )

        self.assertNotIn("KEY", redacted)
        self.assertNotIn("SECRET", redacted)
        self.assertNotIn("PLAINSECRET", redacted)
        self.assertNotIn("TELEGRAMTOKEN", redacted)
        self.assertNotIn("sk-proj-live-secret", redacted)
        self.assertIn("bot[redacted]/sendMessage", redacted)

    def test_paper_exit_code_mapping_is_shared(self) -> None:
        self.assertEqual(paper_exit_code("OK"), 0)
        self.assertEqual(paper_exit_code("WARN"), 0)
        self.assertEqual(paper_exit_code("CRITICAL"), 1)
        self.assertEqual(paper_exit_code("BLOCKED"), 1)
        self.assertEqual(paper_exit_code("ERROR"), 2)

    def test_as_of_date_to_date_accepts_iso_and_special_today(self) -> None:
        self.assertEqual(as_of_date_to_date("2026-06-16"), date(2026, 6, 16))
        self.assertEqual(as_of_date_to_iso("2026-06-16"), "2026-06-16")
        self.assertEqual(as_of_date_to_date("today"), date.today())

    def test_as_of_date_to_date_rejects_invalid_value(self) -> None:
        with self.assertRaises(ValueError):
            as_of_date_to_date("2026-06-16T00:00:00")

        with self.assertRaises(ValueError):
            as_of_date_to_date("not-a-date")

    def test_reason_codes_is_shared_normalizer(self) -> None:
        self.assertEqual(reason_codes(""), [])
        self.assertEqual(reason_codes(" one "), ["one"])
        self.assertEqual(reason_codes(["a", "b"]), ["a", "b"])
        self.assertCountEqual(reason_codes({"a", "b"}), ["a", "b"])
        self.assertEqual(reason_codes(("x", "y")), ["x", "y"])


if __name__ == "__main__":
    unittest.main()
