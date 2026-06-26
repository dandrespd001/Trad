import unittest
from pathlib import Path

from trading_ai.execution.live_connection import (
    AlpacaLiveConnectionError,
    build_alpaca_live_client,
    load_alpaca_live_credentials,
)


class FakeTradingClient:
    def __init__(self, *, api_key: str, secret_key: str, paper: bool) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper


class AlpacaLiveConnectionTests(unittest.TestCase):
    def test_credentials_loader_requires_live_key_and_secret_without_exposing_values(self) -> None:
        with self.assertRaises(AlpacaLiveConnectionError) as raised:
            load_alpaca_live_credentials({"ALPACA_LIVE_API_KEY": "live-key"})

        message = str(raised.exception)
        self.assertIn("ALPACA_LIVE_SECRET_KEY", message)
        self.assertNotIn("live-key", message)

    def test_build_alpaca_live_client_uses_live_mode_with_supplied_client_class(self) -> None:
        client = build_alpaca_live_client(
            env={
                "ALPACA_LIVE_API_KEY": "live-key",
                "ALPACA_LIVE_SECRET_KEY": "live-secret",
            },
            trading_client_cls=FakeTradingClient,
        )

        self.assertIsInstance(client, FakeTradingClient)
        self.assertEqual(client.api_key, "live-key")
        self.assertEqual(client.secret_key, "live-secret")
        self.assertFalse(client.paper)

    def test_live_paper_false_boundary_is_confined_to_live_connection_source(self) -> None:
        matches = []
        for path in Path("src").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "paper=False" in line and "confirm_paper=False" not in line:
                    matches.append(path.as_posix())

        self.assertEqual(matches, ["src/trading_ai/execution/live_connection.py"])


if __name__ == "__main__":
    unittest.main()
