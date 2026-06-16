import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import main
from trading_ai.execution.alpaca_connection import (
    AlpacaPaperConnectionError,
    build_alpaca_paper_client,
    load_alpaca_paper_credentials,
)


class FakeTradingClient:
    def __init__(self, *, api_key: str, secret_key: str, paper: bool) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper


class AlpacaPaperConnectionTests(unittest.TestCase):
    def test_credentials_loader_requires_paper_key_and_secret_without_exposing_values(self) -> None:
        with self.assertRaises(AlpacaPaperConnectionError) as raised:
            load_alpaca_paper_credentials({"ALPACA_PAPER_API_KEY": "paper-key"})

        message = str(raised.exception)
        self.assertIn("ALPACA_PAPER_SECRET_KEY", message)
        self.assertNotIn("paper-key", message)

    def test_build_alpaca_paper_client_uses_paper_mode_with_supplied_client_class(self) -> None:
        client = build_alpaca_paper_client(
            env={
                "ALPACA_PAPER_API_KEY": "paper-key",
                "ALPACA_PAPER_SECRET_KEY": "paper-secret",
            },
            trading_client_cls=FakeTradingClient,
        )

        self.assertIsInstance(client, FakeTradingClient)
        self.assertEqual(client.api_key, "paper-key")
        self.assertEqual(client.secret_key, "paper-secret")
        self.assertTrue(client.paper)

    def test_cli_rejects_real_paper_without_explicit_confirmation(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(["paper", "--broker", "alpaca", "--real-paper"])

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-paper", stderr.getvalue())

    def test_cli_real_paper_read_account_fails_safe_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_status.json"
            stderr = io.StringIO()

            with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--read-account",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("ALPACA_PAPER_API_KEY", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_dry_run_read_account_writes_redacted_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_status.json"

            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--read-account",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["account"]["account_id"], "dry-run")
        self.assertNotIn("secret", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
