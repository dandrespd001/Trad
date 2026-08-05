import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.cli import main
from trading_ai.execution import paper_account_executor as executor_module
from trading_ai.execution.alpaca_connection import (
    AlpacaPaperConnectionError,
    build_alpaca_crypto_market_data_client,
    build_alpaca_market_data_client,
    build_alpaca_paper_client,
    load_alpaca_paper_credentials,
)
from trading_ai.execution.paper_account_executor import SupervisedAlpacaPaperClient


class FakeTradingClient:
    last_init: tuple[str, str, bool] | None = None

    def __init__(self, *, api_key: str, secret_key: str, paper: bool) -> None:
        type(self).last_init = (api_key, secret_key, paper)

    def get_account(self) -> dict[str, str]:
        return {"id": "f9ef2f82-c09b-4af0-a439-243fe31f77d9", "status": "ACTIVE"}


class AlpacaPaperConnectionTests(unittest.TestCase):
    def test_credentials_loader_requires_paper_key_and_secret_without_exposing_values(self) -> None:
        with self.assertRaises(AlpacaPaperConnectionError) as raised:
            load_alpaca_paper_credentials({"ALPACA_PAPER_API_KEY": "paper-key"})

        message = str(raised.exception)
        self.assertIn("ALPACA_PAPER_SECRET_KEY", message)
        self.assertNotIn("paper-key", message)

    def test_build_alpaca_paper_client_uses_paper_mode_with_supplied_client_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "trading_ai.execution.account_supervisor.default_account_supervisor_root",
            return_value=Path(temp_dir) / "authority",
        ):
            client = build_alpaca_paper_client(
                env={
                    "ALPACA_PAPER_API_KEY": "paper-key",
                    "ALPACA_PAPER_SECRET_KEY": "paper-secret",
                },
                trading_client_cls=FakeTradingClient,
            )

        self.assertIsInstance(client, SupervisedAlpacaPaperClient)
        self.assertEqual(FakeTradingClient.last_init, ("paper-key", "paper-secret", True))
        self.assertEqual(client.account_id, "f9ef2f82-c09b-4af0-a439-243fe31f77d9")

    def test_real_stock_and_crypto_market_data_use_one_timed_http_attempt(self) -> None:
        try:
            from alpaca.common.exceptions import APIError
            from requests import HTTPError
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        env = {
            "ALPACA_PAPER_API_KEY": "paper-key",
            "ALPACA_PAPER_SECRET_KEY": "paper-secret",
        }
        builders = (build_alpaca_market_data_client, build_alpaca_crypto_market_data_client)
        for builder in builders:
            with self.subTest(builder=builder.__name__):
                response = mock.Mock(
                    status_code=429,
                    text='{"code": 429, "message": "inert rate limit"}',
                )
                response.raise_for_status.side_effect = HTTPError(response=response)
                with mock.patch(
                    "requests.sessions.Session.request",
                    return_value=response,
                ) as request:
                    client = builder(env=env)
                    with self.assertRaises(APIError):
                        client.get("/inert-latest-trade")

                request.assert_called_once()
                self.assertEqual(
                    request.call_args.kwargs["timeout"],
                    executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
                )
                self.assertEqual(client._retry, 0)  # noqa: SLF001
                self.assertEqual(client._retry_codes, [])  # noqa: SLF001

    def test_real_stock_and_crypto_market_data_timeouts_are_single_attempt(self) -> None:
        try:
            from requests import ConnectTimeout, ReadTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        env = {
            "ALPACA_PAPER_API_KEY": "paper-key",
            "ALPACA_PAPER_SECRET_KEY": "paper-secret",
        }
        builders = (build_alpaca_market_data_client, build_alpaca_crypto_market_data_client)
        for builder in builders:
            for error_type in (ConnectTimeout, ReadTimeout):
                with self.subTest(builder=builder.__name__, error=error_type.__name__), mock.patch(
                    "requests.sessions.Session.request",
                    side_effect=error_type("inert timeout"),
                ) as request:
                    client = builder(env=env)
                    with self.assertRaises(error_type):
                        client.get("/inert-latest-trade")

                request.assert_called_once()
                self.assertEqual(
                    request.call_args.kwargs["timeout"],
                    executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
                )
                self.assertEqual(client._retry, 0)  # noqa: SLF001
                self.assertEqual(client._retry_codes, [])  # noqa: SLF001

    def test_supplied_real_market_data_classes_cannot_bypass_transport_guard(self) -> None:
        try:
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from requests import ConnectTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        env = {
            "ALPACA_PAPER_API_KEY": "paper-key",
            "ALPACA_PAPER_SECRET_KEY": "paper-secret",
        }
        cases = (
            (build_alpaca_market_data_client, StockHistoricalDataClient),
            (build_alpaca_crypto_market_data_client, CryptoHistoricalDataClient),
        )
        for builder, client_cls in cases:
            with self.subTest(builder=builder.__name__), mock.patch(
                "requests.sessions.Session.request",
                side_effect=ConnectTimeout("inert timeout"),
            ) as request:
                client = builder(env=env, client_cls=client_cls)
                with self.assertRaises(ConnectTimeout):
                    client.get("/inert-latest-trade")

            request.assert_called_once()
            self.assertEqual(
                request.call_args.kwargs["timeout"],
                executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
            )
            self.assertEqual(client._retry, 0)  # noqa: SLF001

    def test_cli_rejects_real_paper_without_explicit_confirmation(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(["paper", "--broker", "alpaca", "--real-paper"])

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-paper", stderr.getvalue())

    def test_cli_real_paper_read_account_fails_on_missing_executor_identity_not_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_status.json"
            stderr = io.StringIO()

            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch(
                    "trading_ai.execution.paper_executor_ipc.pwd.getpwnam",
                    side_effect=KeyError("executor identity absent"),
                ),
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("direct broker client must not be built"),
                ) as direct_constructor,
                contextlib.redirect_stderr(stderr),
            ):
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
            self.assertIn("paper executor service identity is not provisioned", stderr.getvalue())
            self.assertNotIn("ALPACA_PAPER", stderr.getvalue())
            direct_constructor.assert_not_called()
            self.assertFalse(output.exists())

    def test_cli_dry_run_read_account_writes_redacted_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_status.json"

            with mock.patch(
                "trading_ai.cli.PaperExecutorBrokerClient",
                side_effect=AssertionError("dry-run must not create an executor IPC client"),
            ) as executor_constructor:
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
            executor_constructor.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["account"]["account_id"], "dry-run")
        self.assertNotIn("secret", json.dumps(payload).lower())

    def test_cli_rejects_multiple_actions_before_creating_any_broker_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "paper_status.json"
            stderr = io.StringIO()

            with (
                mock.patch(
                    "trading_ai.cli.PaperExecutorBrokerClient",
                    side_effect=AssertionError("invalid action set must not create IPC"),
                ) as executor_constructor,
                mock.patch(
                    "trading_ai.execution.alpaca_connection.build_alpaca_paper_client",
                    side_effect=AssertionError("invalid action set must not create a direct client"),
                ) as direct_constructor,
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "paper",
                        "--broker",
                        "alpaca",
                        "--real-paper",
                        "--confirm-paper",
                        "--read-account",
                        "--list-orders",
                        "--output",
                        str(output),
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("one action at a time", stderr.getvalue())
        executor_constructor.assert_not_called()
        direct_constructor.assert_not_called()
        self.assertFalse(output.exists())

    def test_cli_rejects_orphan_order_identifier_before_creating_a_client(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch(
                "trading_ai.cli.PaperExecutorBrokerClient",
                side_effect=AssertionError("invalid arguments must not create IPC"),
            ) as executor_constructor,
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--real-paper",
                    "--confirm-paper",
                    "--order-id",
                    "broker-order-1",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("requires --get-order or --cancel-order", stderr.getvalue())
        executor_constructor.assert_not_called()

    def test_cli_rejects_dry_run_order_lookup_before_creating_a_client(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch(
                "trading_ai.cli.AlpacaPaperBroker",
                side_effect=AssertionError("invalid dry-run lookup must not create a broker"),
            ) as broker_constructor,
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "paper",
                    "--broker",
                    "alpaca",
                    "--dry-run",
                    "--get-order",
                    "--order-id",
                    "broker-order-1",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--get-order requires --real-paper", stderr.getvalue())
        broker_constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
