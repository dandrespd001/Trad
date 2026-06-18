import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    read_text_artifact,
    redact_secrets,
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
            "api_key=KEY secret_key=SECRET "
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
        self.assertNotIn("TELEGRAMTOKEN", redacted)
        self.assertNotIn("sk-proj-live-secret", redacted)
        self.assertIn("bot[redacted]/sendMessage", redacted)

    def test_paper_exit_code_mapping_is_shared(self) -> None:
        self.assertEqual(paper_exit_code("OK"), 0)
        self.assertEqual(paper_exit_code("WARN"), 0)
        self.assertEqual(paper_exit_code("CRITICAL"), 1)
        self.assertEqual(paper_exit_code("BLOCKED"), 1)
        self.assertEqual(paper_exit_code("ERROR"), 2)


if __name__ == "__main__":
    unittest.main()
