import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from trading_ai.execution import paper_account_executor as executor_module
from trading_ai.execution.account_supervisor import (
    AccountLeaseBusyError,
    AccountLeaseIntegrityError,
    AccountMutationLease,
    account_scope_sha256,
)
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder
from trading_ai.execution.order_journal import DurableOrderJournal
from trading_ai.execution.paper_account_executor import (
    PaperAccountAuthorityError,
    PaperAccountDispatchNotStartedError,
    PaperAccountIdentityError,
    SupervisedAlpacaPaperClient,
    build_exclusive_alpaca_paper_client,
    build_supervised_alpaca_paper_client,
)
from trading_ai.risk.policy import RiskLimits

ACCOUNT_A = "f9ef2f82-c09b-4af0-a439-243fe31f77d9"
ACCOUNT_B = "65e49744-7025-4aca-80bb-baa55dc85cd8"
TEST_API_KEY = "paper-key"  # noqa: S105 - inert test credential
TEST_SECRET_KEY = "paper-secret"  # noqa: S105 - inert test credential


class FakeRawPaperClient:
    def __init__(self, accounts: list[object]) -> None:
        self.accounts = list(accounts)
        self.submit_calls: list[object] = []
        self.cancel_calls: list[str] = []

    def get_account(self) -> object:
        if len(self.accounts) > 1:
            return self.accounts.pop(0)
        return self.accounts[0]

    def submit_order(self, order_data: object) -> dict[str, object]:
        self.submit_calls.append(order_data)
        if isinstance(order_data, dict):
            return {"id": "broker-order", "status": "accepted", **order_data}
        quantity = getattr(order_data, "qty", None)
        notional = getattr(order_data, "notional", None)
        return {
            "id": "broker-order",
            "status": "accepted",
            "symbol": str(getattr(order_data, "symbol", "")),
            "side": str(getattr(getattr(order_data, "side", ""), "value", "")),
            "type": "market",
            "time_in_force": str(getattr(getattr(order_data, "time_in_force", ""), "value", "")),
            "client_order_id": str(getattr(order_data, "client_order_id", "")),
            "qty": None if quantity is None else str(quantity),
            "notional": None if notional is None else str(notional),
        }

    def cancel_order_by_id(self, order_id: str) -> dict[str, str]:
        self.cancel_calls.append(order_id)
        return {"id": order_id}

    def get_order_by_client_id(self, client_order_id: str) -> object:
        error = RuntimeError(f"order not found: {client_order_id}")
        error.status_code = 404  # type: ignore[attr-defined]
        raise error


def active_account(account_id: str = ACCOUNT_A) -> dict[str, str]:
    return {"id": account_id, "status": "ACTIVE"}


def active_order() -> dict[str, object]:
    return {
        "id": "broker-order",
        "client_order_id": "intent-cancel",
        "symbol": "SPY",
        "side": "buy",
        "type": "market",
        "order_type": "market",
        "time_in_force": "day",
        "status": "accepted",
        "notional": "1",
        "qty": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2026-07-15T12:00:00Z",
        "created_at": "2026-07-15T12:00:00Z",
        "updated_at": "2026-07-15T12:00:00Z",
        "expires_at": "2026-07-15T20:00:00Z",
    }


class PaperAccountExecutorTests(unittest.TestCase):
    def _roots(self, root: Path):
        return mock.patch(
            "trading_ai.execution.account_supervisor.default_account_supervisor_root",
            return_value=root,
        )

    def test_builder_binds_active_uuid_and_exposes_no_root_or_account_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, self._roots(Path(temp_dir) / "authority"):
            raw = FakeRawPaperClient([active_account()])
            client = build_supervised_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
            )

        self.assertIsInstance(client, SupervisedAlpacaPaperClient)
        self.assertEqual(client.account_id, ACCOUNT_A)
        parameters = inspect.signature(build_supervised_alpaca_paper_client).parameters
        self.assertNotIn("account_id", parameters)
        self.assertNotIn("root", parameters)
        self.assertNotIn("order_journal_path", parameters)

    def test_outer_retry_wrapper_is_confined_to_read_only_broker_methods(self) -> None:
        source_path = Path(executor_module.__file__).with_name("alpaca_paper.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        callsites: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == "_call_with_retry"
                for candidate in ast.walk(node)
            ):
                callsites.add(node.name)

        self.assertLessEqual(
            callsites,
            {
                "read_account",
                "read_positions",
                "list_orders",
                "list_fill_activities",
                "get_order",
                "get_order_by_client_id",
                "_lookup_existing_order",
            },
        )
        self.assertTrue(
            callsites.isdisjoint(
                {
                    "submit_order",
                    "cancel_order",
                    "_submit_broker_order",
                }
            )
        )

    def test_builder_disables_hidden_sdk_retries_and_mutation_revalidates_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, self._roots(Path(temp_dir) / "authority"):
            raw = FakeRawPaperClient([active_account()])
            raw._retry = 3  # noqa: SLF001 - emulate alpaca-py RESTClient defaults
            raw._retry_wait = 3  # noqa: SLF001
            raw._retry_codes = [429, 504]  # noqa: SLF001
            client = build_supervised_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
            )

            self.assertEqual(raw._retry, 0)  # noqa: SLF001
            self.assertEqual(raw._retry_wait, 0)  # noqa: SLF001
            self.assertEqual(raw._retry_codes, [])  # noqa: SLF001

            raw._retry = 1  # noqa: SLF001 - simulate runtime policy tampering
            with self.assertRaisesRegex(
                PaperAccountAuthorityError,
                "retry policy is not single-attempt",
            ):
                client.submit_order(SimpleNamespace(client_order_id="intent-1"))

        self.assertEqual(raw.submit_calls, [])

    def test_installed_alpaca_sdk_makes_one_physical_attempt_on_429_and_504(
        self,
    ) -> None:
        try:
            from alpaca.common.exceptions import APIError
            from requests import HTTPError
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        for status_code in (429, 504):
            with self.subTest(status_code=status_code):
                response = mock.Mock()
                response.status_code = status_code
                response.text = f'{{"code": {status_code}, "message": "inert test error"}}'
                response.raise_for_status.side_effect = HTTPError(response=response)
                with mock.patch(
                    "requests.sessions.Session.request",
                    return_value=response,
                ) as request:
                    raw = executor_module._build_raw_alpaca_paper_client(  # noqa: SLF001
                        api_key=TEST_API_KEY,
                        secret_key=TEST_SECRET_KEY,
                        trading_client_cls=None,
                    )

                    with self.assertRaises(APIError):
                        raw.post("/orders", {"symbol": "SPY"})

                request.assert_called_once()
                self.assertEqual(
                    request.call_args.kwargs["timeout"],
                    executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
                )
                self.assertEqual(raw._retry, 0)  # noqa: SLF001
                self.assertEqual(raw._retry_codes, [])  # noqa: SLF001

    def test_installed_sdk_connect_and_read_timeouts_make_one_post_attempt(self) -> None:
        try:
            from requests import ConnectTimeout, ReadTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        for timeout_error in (ConnectTimeout("connect timeout"), ReadTimeout("read timeout")):
            with self.subTest(error_type=type(timeout_error).__name__), mock.patch(
                "requests.sessions.Session.request",
                side_effect=timeout_error,
            ) as request:
                raw = executor_module._build_raw_alpaca_paper_client(  # noqa: SLF001
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=None,
                )

                with self.assertRaises(type(timeout_error)):
                    raw.post("/orders", {"symbol": "SPY"})

            request.assert_called_once()
            self.assertEqual(request.call_args.args[0], "POST")
            self.assertEqual(
                request.call_args.kwargs["timeout"],
                executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
            )

    def test_installed_sdk_cancel_uses_one_delete_with_fixed_timeout(self) -> None:
        try:
            from alpaca.trading.client import TradingClient  # noqa: F401
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        response = mock.Mock(status_code=204, text="")
        response.raise_for_status.return_value = None
        with mock.patch(
            "requests.sessions.Session.request",
            return_value=response,
        ) as request:
            raw = executor_module._build_raw_alpaca_paper_client(  # noqa: SLF001
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=None,
            )
            raw.cancel_order_by_id(ACCOUNT_B)

        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], "DELETE")
        self.assertEqual(
            request.call_args.kwargs["timeout"],
            executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
        )

    def test_redirect_responses_never_trigger_a_second_physical_attempt(self) -> None:
        try:
            from requests import Response
            from requests.adapters import HTTPAdapter
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        for status_code in (301, 307, 308):
            with self.subTest(status_code=status_code):
                physical_attempts: list[str] = []

                def redirecting_send(
                    _adapter: object,
                    request: object,
                    _attempts: list[str] = physical_attempts,
                    _status_code: int = status_code,
                    **_kwargs: object,
                ) -> Response:
                    _attempts.append(str(getattr(request, "method", "")))
                    response = Response()
                    response.status_code = _status_code
                    response.headers["Location"] = str(getattr(request, "url", ""))
                    response.url = str(getattr(request, "url", ""))
                    response.request = request  # type: ignore[assignment]
                    response._content = b""  # noqa: SLF001 - inert synthetic response
                    return response

                with mock.patch.object(
                    HTTPAdapter,
                    "send",
                    redirecting_send,
                ):
                    raw = executor_module._build_raw_alpaca_paper_client(  # noqa: SLF001
                        api_key=TEST_API_KEY,
                        secret_key=TEST_SECRET_KEY,
                        trading_client_cls=None,
                    )
                    raw.post("/orders", {"symbol": "SPY"})

                    self.assertEqual(physical_attempts, ["POST"])
                    physical_attempts.clear()
                    raw._session.request(  # noqa: SLF001 - prove caller cannot enable redirects
                        "POST",
                        "https://paper-api.alpaca.markets/v2/orders",
                        allow_redirects=True,
                    )

                self.assertEqual(physical_attempts, ["POST"])

    def test_supplied_real_sdk_class_cannot_bypass_timeout_guard(self) -> None:
        try:
            from alpaca.common.enums import BaseURL
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        response = mock.Mock(status_code=200, text='{"inert": "account"}')
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "id": ACCOUNT_A,
            "account_number": "INERT-PAPER-ACCOUNT",
            "status": "ACTIVE",
        }
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch("requests.sessions.Session.request", return_value=response) as request,
        ):
            client = build_supervised_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=TradingClient,
                supervisor_root=Path(temp_dir) / "authority",
            )

        request.assert_called_once()
        self.assertEqual(
            request.call_args.kwargs["timeout"],
            executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
        )
        raw = object.__getattribute__(client, "_SupervisedAlpacaPaperClient__client")
        self.assertEqual(raw._retry, 0)  # noqa: SLF001
        self.assertIs(raw._base_url, BaseURL.TRADING_PAPER)  # noqa: SLF001
        self.assertIs(raw._sandbox, True)  # noqa: SLF001
        self.assertEqual(raw._api_version, "v2")  # noqa: SLF001
        self.assertIs(raw._use_basic_auth, False)  # noqa: SLF001
        self.assertIs(raw._use_raw_data, False)  # noqa: SLF001
        self.assertIsNone(raw._oauth_token)  # noqa: SLF001
        self.assertIsNotNone(  # noqa: SLF001
            getattr(raw, executor_module._HTTP_TRANSPORT_GUARD_ATTRIBUTE),
        )

    def test_injected_sdk_session_is_replaced_and_owned_adapter_is_revalidated(self) -> None:
        try:
            from alpaca.trading.client import TradingClient
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        raw = TradingClient(
            api_key=TEST_API_KEY,
            secret_key=TEST_SECRET_KEY,
            paper=True,
        )
        source_session = raw._session  # noqa: SLF001 - inspect audited SDK transport
        retrying_adapter = HTTPAdapter(
            max_retries=Retry(total=2, allowed_methods=None),
        )
        source_session.mount("https://", retrying_adapter)
        injected_hook = mock.Mock()
        source_session.hooks["response"].append(injected_hook)
        source_session.auth = ("inert-user", "inert-password")
        source_session.proxies = {"https": "http://127.0.0.1:9"}
        source_session.cookies.set("inert", "cookie")
        account_response = mock.Mock(
            status_code=200,
            text='{"id":"' + ACCOUNT_A + '","status":"ACTIVE"}',
        )
        account_response.raise_for_status.return_value = None
        account_response.json.return_value = {
            "id": ACCOUNT_A,
            "account_number": "INERT-PAPER-ACCOUNT",
            "status": "ACTIVE",
        }
        mutation_response = mock.Mock(status_code=200, text="{}")
        mutation_response.raise_for_status.return_value = None
        mutation_response.json.return_value = {}

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "requests.sessions.Session.request",
                side_effect=(account_response, mutation_response, mutation_response),
            ) as request,
        ):
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            raw.post("/orders", {"symbol": "SPY"})
            raw.delete("/orders/broker-order")

            self.assertEqual(request.call_count, 3)
            self.assertEqual(
                [call.args[0] for call in request.call_args_list[1:]],
                ["POST", "DELETE"],
            )
            guard = getattr(
                raw,
                executor_module._HTTP_TRANSPORT_GUARD_ATTRIBUTE,
            )
            self.assertIs(raw._session, guard.adapter)  # noqa: SLF001
            self.assertIsNot(guard.source_session, source_session)
            self.assertFalse(guard.source_session.trust_env)
            self.assertEqual(guard.source_session.hooks, {"response": []})
            self.assertEqual(guard.source_session.proxies, {})
            self.assertEqual(len(guard.source_session.cookies), 0)
            self.assertEqual(retrying_adapter.max_retries.total, 2)
            injected_hook.assert_not_called()
            for owned in guard.session_adapters:
                retry_policy = owned.adapter.max_retries
                self.assertEqual(retry_policy.total, 0)
                self.assertEqual(retry_policy.connect, 0)
                self.assertEqual(retry_policy.read, 0)
                self.assertEqual(retry_policy.status, 0)
                self.assertEqual(retry_policy.allowed_methods, frozenset())

            request.reset_mock()
            guard.session_adapters[0].adapter.max_retries = Retry(
                total=2,
                allowed_methods=None,
            )
            try:
                with self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "adapter retry guard is not intact",
                ):
                    client.submit_order(SimpleNamespace(client_order_id="adapter-tamper"))
                request.assert_not_called()

                guard.session_adapters[0].adapter.max_retries = (
                    guard.session_adapters[0].retry_policy
                )
                guard.source_session.trust_env = True
                with self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "session guard is not intact",
                ):
                    client.cancel_order_by_id("session-tamper")
                request.assert_not_called()

                guard.source_session.trust_env = False
                guard.source_session.hooks["response"].append(mock.Mock())
                with self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "session guard is not intact",
                ):
                    client.submit_order(SimpleNamespace(client_order_id="hook-tamper"))
                request.assert_not_called()
            finally:
                client.close()

    def test_real_trading_target_tamper_blocks_before_http(self) -> None:
        try:
            from alpaca.common.enums import BaseURL
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        raw = TradingClient(
            api_key=TEST_API_KEY,
            secret_key=TEST_SECRET_KEY,
            paper=True,
        )
        account_response = mock.Mock(
            status_code=200,
            text='{"id":"' + ACCOUNT_A + '","status":"ACTIVE"}',
        )
        account_response.raise_for_status.return_value = None
        account_response.json.return_value = {
            "id": ACCOUNT_A,
            "account_number": "INERT-PAPER-ACCOUNT",
            "status": "ACTIVE",
        }
        tamper_cases = (
            ("_base_url", BaseURL.TRADING_LIVE, BaseURL.TRADING_PAPER),
            ("_sandbox", False, True),
            ("_api_version", "v1", "v2"),
            ("_use_basic_auth", True, False),
            ("_use_raw_data", True, False),
            ("_oauth_token", "oauth-disabled", None),
        )

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "requests.sessions.Session.request",
                return_value=account_response,
            ) as request,
        ):
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            self.assertIn("paper-api.alpaca.markets/v2/account", request.call_args.args[1])
            request.reset_mock()
            try:
                for attribute, tampered, restored in tamper_cases:
                    with self.subTest(attribute=attribute):
                        setattr(raw, attribute, tampered)
                        try:
                            with self.assertRaisesRegex(
                                PaperAccountAuthorityError,
                                "target or authentication mode is not audited",
                            ):
                                raw.post("/orders", {"client_order_id": "target-tamper"})
                        finally:
                            setattr(raw, attribute, restored)
                        request.assert_not_called()

                raw._base_url = BaseURL.TRADING_LIVE  # noqa: SLF001
                with self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "target or authentication mode is not audited",
                ):
                    client.submit_order(SimpleNamespace(client_order_id="live-target"))
                request.assert_not_called()
            finally:
                raw._base_url = BaseURL.TRADING_PAPER  # noqa: SLF001
                client.close()

    def test_injected_live_trading_client_is_rejected_before_first_read(self) -> None:
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        live_client = TradingClient(
            api_key=TEST_API_KEY,
            secret_key=TEST_SECRET_KEY,
            paper=False,
        )
        with (
            mock.patch("requests.sessions.Session.request") as request,
            self.assertRaisesRegex(
                PaperAccountAuthorityError,
                "target or authentication mode is not audited",
            ),
        ):
            build_supervised_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: live_client,
            )

        request.assert_not_called()

    def test_non_trading_rest_and_trading_subclass_injections_are_rejected(self) -> None:
        try:
            from alpaca.common.enums import BaseURL
            from alpaca.common.rest import RESTClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        class CustomRESTClient(RESTClient):
            pass

        class TradingClientSubclass(TradingClient):
            pass

        injected_clients = (
            RESTClient(
                base_url=BaseURL.TRADING_PAPER,
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
            ),
            CustomRESTClient(
                base_url=BaseURL.TRADING_PAPER,
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
            ),
            TradingClientSubclass(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                paper=True,
            ),
        )
        for injected in injected_clients:
            with (
                self.subTest(client_type=type(injected).__name__),
                mock.patch("requests.sessions.Session.request") as request,
                self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "REST client type is outside the audited boundary",
                ),
            ):
                build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda injected=injected, **_kwargs: injected,
                )
            request.assert_not_called()

    def test_installed_sdk_timeout_tamper_before_mutation_has_no_side_effect(self) -> None:
        try:
            from alpaca.trading.client import TradingClient  # noqa: F401
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        account_payload = {
            "id": ACCOUNT_A,
            "account_number": "INERT-PAPER-ACCOUNT",
            "status": "ACTIVE",
        }
        response = mock.Mock()
        response.status_code = 200
        response.text = '{"inert": "account"}'
        response.json.return_value = account_payload

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch("requests.sessions.Session.request", return_value=response) as request,
        ):
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=None,
                supervisor_root=Path(temp_dir) / "authority",
            )
            try:
                request.assert_called_once()
                self.assertEqual(
                    request.call_args.kwargs["timeout"],
                    executor_module._AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,  # noqa: SLF001
                )
                request.reset_mock()
                raw = object.__getattribute__(
                    client,
                    "_SupervisedAlpacaPaperClient__client",
                )
                raw._session = object()  # noqa: SLF001 - simulate runtime transport tampering

                with self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "HTTP timeout guard is not intact",
                ):
                    client.submit_order(SimpleNamespace(client_order_id="intent-timeout-tamper"))

                request.assert_not_called()
            finally:
                client.close()

    def test_owned_session_dispatch_overrides_are_blocked_before_http(self) -> None:
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        raw = TradingClient(
            api_key=TEST_API_KEY,
            secret_key=TEST_SECRET_KEY,
            paper=True,
        )
        account_response = mock.Mock(
            status_code=200,
            text='{"id":"' + ACCOUNT_A + '","status":"ACTIVE"}',
        )
        account_response.raise_for_status.return_value = None
        account_response.json.return_value = {
            "id": ACCOUNT_A,
            "account_number": "INERT-PAPER-ACCOUNT",
            "status": "ACTIVE",
        }
        forged_attempts: list[str] = []

        def forged_dispatch(*_args: object, **_kwargs: object) -> object:
            forged_attempts.extend(("attempt-1", "attempt-2"))
            raise AssertionError("forged dispatch must never run")

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch(
                "requests.sessions.Session.request",
                return_value=account_response,
            ) as request,
        ):
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            guard = getattr(
                raw,
                executor_module._HTTP_TRANSPORT_GUARD_ATTRIBUTE,
            )
            request.reset_mock()
            try:
                for index, method_name in enumerate(
                    (
                        "send",
                        "prepare_request",
                        "merge_environment_settings",
                        "get_adapter",
                    ),
                    start=1,
                ):
                    with self.subTest(method_name=method_name):
                        setattr(guard.source_session, method_name, forged_dispatch)
                        try:
                            with self.assertRaisesRegex(
                                PaperAccountAuthorityError,
                                "session type is not audited",
                            ):
                                client.submit_order(
                                    SimpleNamespace(
                                        client_order_id=f"dispatch-override-{index}"
                                    )
                                )
                            with self.assertRaisesRegex(
                                PaperAccountAuthorityError,
                                "session type is not audited",
                            ):
                                raw.post(
                                    "/orders",
                                    {"client_order_id": f"raw-override-{index}"},
                                )
                        finally:
                            delattr(guard.source_session, method_name)
                        request.assert_not_called()
                        self.assertEqual(forged_attempts, [])

                owned_adapter = guard.session_adapters[0].adapter
                owned_adapter.send = forged_dispatch
                try:
                    with self.assertRaisesRegex(
                        PaperAccountAuthorityError,
                        "adapter retry guard is not intact",
                    ):
                        raw.post("/orders", {"client_order_id": "adapter-send-override"})
                finally:
                    delattr(owned_adapter, "send")
                request.assert_not_called()
                self.assertEqual(forged_attempts, [])

                with (
                    mock.patch.object(
                        type(guard.source_session),
                        "send",
                        forged_dispatch,
                    ),
                    self.assertRaisesRegex(
                        PaperAccountAuthorityError,
                        "session type is not audited",
                    ),
                ):
                    client.cancel_order_by_id("class-send-override")
                request.assert_not_called()
                self.assertEqual(forged_attempts, [])
            finally:
                client.close()

    def test_submit_timeout_before_callback_is_durable_and_safely_retryable(self) -> None:
        try:
            from requests import ConnectTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        class PreDispatchTimeoutRaw(FakeRawPaperClient):
            def __init__(self) -> None:
                super().__init__([active_account()])
                self.account_reads = 0

            def get_account(self) -> object:
                self.account_reads += 1
                if self.account_reads == 2:
                    raise ConnectTimeout("account preflight timeout")
                return active_account()

        class FixedMarketData:
            def get_stock_latest_trade(self, _request: object) -> dict[str, object]:
                return {"SPY": SimpleNamespace(price=100.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = PreDispatchTimeoutRaw()
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=root,
            )
            try:
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    market_data=FixedMarketData(),
                )
                order = PaperOrder(
                    symbol="SPY",
                    side="buy",
                    notional=1.0,
                    client_order_id="intent-pre-dispatch",
                    reference_price=100.0,
                    position_intent="open",
                )

                deferred = broker.submit_order(order)
                after_deferred = DurableOrderJournal(client.order_journal_path).get(
                    order.client_order_id
                )
                event = DurableOrderJournal(client.order_journal_path).events(
                    order.client_order_id
                )[-1]
                submitted = broker.submit_order(order)
            finally:
                client.close()

        self.assertEqual(deferred.status, "submit_deferred")
        self.assertEqual(deferred.reasons, ("paper_account_pre_dispatch_failed",))
        self.assertIsNotNone(after_deferred)
        assert after_deferred is not None
        self.assertEqual(after_deferred.state.value, "intent_recorded")
        self.assertEqual(event.event_type, "submit_not_dispatched")
        self.assertEqual(event.metadata["classification"], "not_dispatched_retryable")
        self.assertTrue(submitted.accepted, submitted)
        self.assertEqual(len(raw.submit_calls), 1)

    def test_submit_timeout_after_callback_is_unresolved_and_never_reposted(self) -> None:
        try:
            from requests import ReadTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        class PostDispatchTimeoutRaw(FakeRawPaperClient):
            def submit_order(self, order_data: object) -> object:
                self.submit_calls.append(order_data)
                raise ReadTimeout("submit response timeout")

        class FixedMarketData:
            def get_stock_latest_trade(self, _request: object) -> dict[str, object]:
                return {"SPY": SimpleNamespace(price=100.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            raw = PostDispatchTimeoutRaw([active_account()])
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            try:
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    market_data=FixedMarketData(),
                )
                order = PaperOrder(
                    symbol="SPY",
                    side="buy",
                    notional=1.0,
                    client_order_id="intent-post-dispatch",
                    reference_price=100.0,
                    position_intent="open",
                )

                first = broker.submit_order(order)
                second = broker.submit_order(order)
                events = DurableOrderJournal(client.order_journal_path).events(order.client_order_id)
            finally:
                client.close()

        self.assertEqual(first.status, "submit_unresolved")
        self.assertEqual(second.status, "submit_unresolved")
        self.assertEqual(len(raw.submit_calls), 1)
        self.assertNotIn("submit_not_dispatched", [event.event_type for event in events])

    def test_raw_client_cannot_forge_not_dispatched_after_submit_started(self) -> None:
        class ForgingRawClient(FakeRawPaperClient):
            def submit_order(self, order_data: object) -> object:
                self.submit_calls.append(order_data)
                raise PaperAccountDispatchNotStartedError(
                    "forged after physical submit"
                )

        class FixedMarketData:
            def get_stock_latest_trade(self, _request: object) -> dict[str, object]:
                return {"SPY": SimpleNamespace(price=100.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            raw = ForgingRawClient([active_account()])
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            try:
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    market_data=FixedMarketData(),
                )
                order = PaperOrder(
                    symbol="SPY",
                    side="buy",
                    notional=1.0,
                    client_order_id="intent-forged-proof",
                    reference_price=100.0,
                    position_intent="open",
                )

                first = broker.submit_order(order)
                second = broker.submit_order(order)
                event_types = tuple(
                    event.event_type
                    for event in DurableOrderJournal(client.order_journal_path).events(
                        order.client_order_id
                    )
                )
            finally:
                client.close()

        self.assertEqual(first.status, "submit_unresolved")
        self.assertEqual(second.status, "submit_unresolved")
        self.assertEqual(len(raw.submit_calls), 1)
        self.assertNotIn("submit_not_dispatched", event_types)

    def test_cancel_timeout_before_callback_is_durable_and_safely_retryable(self) -> None:
        try:
            from requests import ConnectTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        class PreDispatchCancelTimeoutRaw(FakeRawPaperClient):
            def __init__(self) -> None:
                super().__init__([active_account()])
                self.account_reads = 0
                self.order = active_order()

            def get_account(self) -> object:
                self.account_reads += 1
                if self.account_reads == 2:
                    raise ConnectTimeout("cancel account preflight timeout")
                return active_account()

            def get_order_by_client_id(self, _client_order_id: str) -> object:
                return self.order

            def get_order_by_id(self, _order_id: str) -> object:
                return self.order

        with tempfile.TemporaryDirectory() as temp_dir:
            raw = PreDispatchCancelTimeoutRaw()
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            try:
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )
                deferred = broker.cancel_order(client_order_id="intent-cancel")
                event = DurableOrderJournal(client.order_journal_path).events("intent-cancel")[-1]
                canceled = broker.cancel_order(client_order_id="intent-cancel")
            finally:
                client.close()

        self.assertEqual(deferred.status, "cancel_deferred")
        self.assertEqual(deferred.reasons, ("paper_account_pre_dispatch_failed",))
        self.assertEqual(event.event_type, "cancel_not_dispatched")
        self.assertEqual(event.metadata["classification"], "not_dispatched_retryable")
        self.assertTrue(canceled.accepted, canceled)
        self.assertEqual(raw.cancel_calls, ["broker-order"])

    def test_cancel_timeout_after_callback_is_unresolved_and_never_redeleted(self) -> None:
        try:
            from requests import ReadTimeout
        except ImportError as exc:  # pragma: no cover - broker extra is optional
            self.skipTest(str(exc))

        class PostDispatchCancelTimeoutRaw(FakeRawPaperClient):
            def __init__(self) -> None:
                super().__init__([active_account()])
                self.order = active_order()

            def get_order_by_client_id(self, _client_order_id: str) -> object:
                return self.order

            def get_order_by_id(self, _order_id: str) -> object:
                return self.order

            def cancel_order_by_id(self, order_id: str) -> object:
                self.cancel_calls.append(order_id)
                raise ReadTimeout("cancel response timeout")

        with tempfile.TemporaryDirectory() as temp_dir:
            raw = PostDispatchCancelTimeoutRaw()
            client = build_exclusive_alpaca_paper_client(
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=lambda **_kwargs: raw,
                supervisor_root=Path(temp_dir) / "authority",
            )
            try:
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )
                first = broker.cancel_order(client_order_id="intent-cancel")
                second = broker.cancel_order(client_order_id="intent-cancel")
                event_types = [
                    event.event_type
                    for event in DurableOrderJournal(client.order_journal_path).events("intent-cancel")
                ]
            finally:
                client.close()

        self.assertEqual(first.status, "cancel_unresolved")
        self.assertEqual(second.status, "cancel_unresolved")
        self.assertEqual(raw.cancel_calls, ["broker-order"])
        self.assertIn("cancel_dispatch_attempted", event_types)
        self.assertNotIn("cancel_not_dispatched", event_types)

    def test_unreviewed_alpaca_sdk_version_fails_before_client_construction(
        self,
    ) -> None:
        with (
            mock.patch.object(
                executor_module,
                "package_version",
                return_value="0.44.0",
            ),
            mock.patch(
                "builtins.__import__",
                side_effect=AssertionError("SDK import must not be reached"),
            ) as sdk_import,
            self.assertRaisesRegex(
                PaperAccountAuthorityError,
                "outside the audited single-attempt contract",
            ),
        ):
            executor_module._build_raw_alpaca_paper_client(  # noqa: SLF001
                api_key=TEST_API_KEY,
                secret_key=TEST_SECRET_KEY,
                trading_client_cls=None,
            )

        sdk_import.assert_not_called()

    def test_missing_malformed_or_inactive_identity_fails_before_authority(self) -> None:
        invalid_accounts = (
            {"id": None, "status": "ACTIVE"},
            {"id": True, "status": "ACTIVE"},
            {"id": "not-a-uuid", "status": "ACTIVE"},
            {"id": ACCOUNT_A, "status": "SUSPENDED"},
        )
        for account in invalid_accounts:
            with self.subTest(account=account), self.assertRaises(PaperAccountIdentityError):
                build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda account=account, **_kwargs: FakeRawPaperClient([account]),
                )

    def test_submit_and_cancel_use_canonical_account_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                order = SimpleNamespace(client_order_id="intent-1")

                client.submit_order(order)
                client.cancel_order_by_id("broker-order")

            scope = account_scope_sha256(
                broker="alpaca",
                environment="paper",
                account_id=ACCOUNT_A,
            )
            self.assertEqual(client.order_journal_path, root / f"{scope}.orders.sqlite3")
            self.assertEqual(
                client.executor_journal_path,
                root / f"{scope}.executor.sqlite3",
            )
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(client.order_journal_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(raw.submit_calls, [order])
            self.assertEqual(raw.cancel_calls, ["broker-order"])

    def test_cancel_before_dispatch_runs_inside_account_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                callback_observed_lock = False

                def before_dispatch() -> None:
                    nonlocal callback_observed_lock
                    with self.assertRaises(AccountLeaseBusyError):
                        AccountMutationLease.acquire(
                            broker="alpaca",
                            environment="paper",
                            account_id=ACCOUNT_A,
                        )
                    callback_observed_lock = True

                client.cancel_order_by_id(
                    "broker-order",
                    before_dispatch=before_dispatch,
                )

            self.assertTrue(callback_observed_lock)
            self.assertEqual(raw.cancel_calls, ["broker-order"])

    def test_existing_account_lease_blocks_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                lease = AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id=ACCOUNT_A,
                )
                try:
                    with self.assertRaises(AccountLeaseBusyError):
                        client.submit_order(SimpleNamespace(client_order_id="intent-1"))
                finally:
                    lease.release()

            self.assertEqual(raw.submit_calls, [])

    def test_exclusive_executor_holds_one_lease_for_its_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_exclusive_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                self.assertTrue(client.persistent_authority)
                self.assertEqual(client.fence_epoch, 1)

                client.submit_order(SimpleNamespace(client_order_id="intent-1"))
                client.cancel_order_by_id("broker-order")

                with self.assertRaises(AccountLeaseBusyError):
                    AccountMutationLease.acquire(
                        broker="alpaca",
                        environment="paper",
                        account_id=ACCOUNT_A,
                    )

                self.assertEqual(client.fence_epoch, 1)
                client.close()
                self.assertFalse(client.persistent_authority)

                next_lease = AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id=ACCOUNT_A,
                )
                try:
                    self.assertEqual(next_lease.fence.epoch, 2)
                finally:
                    next_lease.release()

            self.assertEqual(len(raw.submit_calls), 1)
            self.assertEqual(raw.cancel_calls, ["broker-order"])

    def test_second_exclusive_executor_is_rejected_and_closed_client_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            with self._roots(root):
                first = build_exclusive_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: FakeRawPaperClient([active_account()]),
                )
                try:
                    with self.assertRaises(AccountLeaseBusyError):
                        build_exclusive_alpaca_paper_client(
                            api_key=TEST_API_KEY,
                            secret_key=TEST_SECRET_KEY,
                            trading_client_cls=lambda **_kwargs: FakeRawPaperClient([active_account()]),
                        )
                finally:
                    first.close()

                first.close()
                with self.assertRaisesRegex(PaperAccountAuthorityError, "closed"):
                    first.submit_order(SimpleNamespace(client_order_id="intent-2"))

    def test_broker_defers_before_submit_claim_and_retries_once_after_lease_release(self) -> None:
        class FixedMarketData:
            def __init__(self) -> None:
                self.reads = 0

            def get_stock_latest_trade(self, _request: object) -> dict[str, object]:
                self.reads += 1
                return {"SPY": SimpleNamespace(price=100.0)}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            market_data = FixedMarketData()
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                    market_data=market_data,
                )
                lease = AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id=ACCOUNT_A,
                )
                order = SimpleNamespace(
                    symbol="SPY",
                    side="buy",
                    quantity=None,
                    notional=1.0,
                    estimated_position_weight=0.0,
                    projected_gross_exposure=0.0,
                    daily_pnl_pct=0.0,
                    current_drawdown_pct=0.0,
                    reference_price=100.0,
                    client_order_id="intent-deferred",
                    order_type="market",
                    limit_price=None,
                    position_intent="open",
                )
                try:
                    deferred = broker.submit_order(order)
                finally:
                    lease.release()

                journal = DurableOrderJournal(client.order_journal_path)
                deferred_record = journal.get("intent-deferred")
                self.assertIsNotNone(deferred_record)
                assert deferred_record is not None
                self.assertEqual(deferred.status, "submit_deferred")
                self.assertEqual(deferred.reasons, ("account_mutation_busy",))
                self.assertEqual(deferred_record.state.value, "intent_recorded")
                self.assertEqual(raw.submit_calls, [])

                submitted = broker.submit_order(order)

            self.assertTrue(submitted.accepted, submitted)
            self.assertEqual(submitted.status, "submitted")
            self.assertEqual(len(raw.submit_calls), 1)
            self.assertEqual(market_data.reads, 1)
            submitted_record = journal.get("intent-deferred")
            self.assertIsNotNone(submitted_record)
            assert submitted_record is not None
            self.assertEqual(submitted_record.state.value, "acknowledged")

    def test_reducing_position_is_revalidated_inside_lease_before_submit(self) -> None:
        class ChangingPositionRaw(FakeRawPaperClient):
            def __init__(self) -> None:
                super().__init__([active_account()])
                self.position_reads = 0

            def list_positions(self) -> list[object]:
                self.position_reads += 1
                if self.position_reads > 1:
                    return []
                return [
                    SimpleNamespace(
                        symbol="SPY",
                        qty="1",
                        market_value="100",
                    )
                ]

            @staticmethod
            def get_orders(*, filter: object) -> list[object]:  # noqa: A002
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = ChangingPositionRaw()
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                broker = AlpacaPaperBroker(
                    client=client,
                    allowlist=("SPY",),
                    risk_limits=RiskLimits(),
                    dry_run=False,
                )
                result = broker.submit_order(
                    SimpleNamespace(
                        symbol="SPY",
                        side="sell",
                        quantity=1.0,
                        notional=None,
                        estimated_position_weight=0.0,
                        projected_gross_exposure=0.0,
                        daily_pnl_pct=0.0,
                        current_drawdown_pct=0.0,
                        reference_price=None,
                        client_order_id="intent-reduce",
                        order_type="market",
                        limit_price=None,
                        position_intent="close",
                    )
                )

            self.assertFalse(result.accepted)
            self.assertEqual(result.status, "risk_rejected")
            self.assertEqual(result.reasons, ("reducing_position_missing",))
            self.assertEqual(raw.position_reads, 2)
            self.assertEqual(raw.submit_calls, [])
            record = DurableOrderJournal(client.order_journal_path).get("intent-reduce")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.state.value, "intent_recorded")

    def test_identity_change_is_rejected_immediately_before_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account(ACCOUNT_A), active_account(ACCOUNT_B)])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                with self.assertRaises(PaperAccountIdentityError):
                    client.submit_order(SimpleNamespace(client_order_id="intent-1"))

            self.assertEqual(raw.submit_calls, [])

    def test_replaced_canonical_journal_blocks_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
                original = client.order_journal_path
                original.rename(root / "original-journal.sqlite3")
                DurableOrderJournal(original)
                original.chmod(0o600)

                with self.assertRaisesRegex(AccountLeaseIntegrityError, "identity changed"):
                    client.submit_order(SimpleNamespace(client_order_id="intent-1"))

            self.assertEqual(raw.submit_calls, [])

    def test_preexisting_unsafe_journal_permissions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            root.mkdir(mode=0o700)
            scope = account_scope_sha256(
                broker="alpaca",
                environment="paper",
                account_id=ACCOUNT_A,
            )
            journal_path = root / f"{scope}.orders.sqlite3"
            DurableOrderJournal(journal_path)
            journal_path.chmod(0o644)

            with (
                self._roots(root),
                self.assertRaisesRegex(
                    PaperAccountAuthorityError,
                    "permissions",
                ),
            ):
                build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: FakeRawPaperClient([active_account()]),
                )

    def test_real_broker_uses_supervised_canonical_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "authority"
            raw = FakeRawPaperClient([active_account()])
            with self._roots(root):
                client = build_supervised_alpaca_paper_client(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                    trading_client_cls=lambda **_kwargs: raw,
                )
            broker = AlpacaPaperBroker(
                client=client,
                allowlist=("SPY",),
                risk_limits=mock.Mock(),
                dry_run=False,
                order_journal_path=Path(temp_dir) / "caller-selected.sqlite3",
            )

            self.assertEqual(
                broker._order_journal_path,  # noqa: SLF001 - authority boundary assertion
                client.order_journal_path,
            )

    def test_raw_trading_sdk_constructors_are_confined_to_authority_modules(self) -> None:
        imports: set[str] = set()
        paper_modes: dict[bool, set[str]] = {True: set(), False: set()}
        for path in Path("src/trading_ai").rglob("*.py"):
            relative = path.as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "alpaca.trading.client"
                    and any(alias.name == "TradingClient" for alias in node.names)
                ):
                    imports.add(relative)
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "paper"
                            and isinstance(keyword.value, ast.Constant)
                            and type(keyword.value.value) is bool
                        ):
                            paper_modes[keyword.value.value].add(relative)

        self.assertEqual(
            imports,
            {
                "src/trading_ai/execution/live_connection.py",
                "src/trading_ai/execution/paper_account_executor.py",
            },
        )
        self.assertEqual(
            paper_modes[True],
            {"src/trading_ai/execution/paper_account_executor.py"},
        )
        self.assertEqual(
            paper_modes[False],
            {"src/trading_ai/execution/live_connection.py"},
        )

    def test_direct_paper_connection_module_is_confined_to_executor_daemon(self) -> None:
        protected_module = "trading_ai.execution.alpaca_connection"
        allowed_paths = {
            "src/trading_ai/execution/alpaca_connection.py",
            "src/trading_ai/execution/paper_executor_daemon.py",
        }
        violations: list[str] = []
        for path in Path("src/trading_ai").rglob("*.py"):
            relative = path.as_posix()
            if relative in allowed_paths:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == protected_module:
                    violations.append(f"{relative}:{node.lineno}:from-import")
                elif isinstance(node, ast.Import) and any(
                    alias.name == protected_module for alias in node.names
                ):
                    violations.append(f"{relative}:{node.lineno}:module-import")

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
