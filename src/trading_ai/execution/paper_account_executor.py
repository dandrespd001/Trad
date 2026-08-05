"""Same-host authority boundary for Alpaca paper-account mutations.

The wrapper binds the SDK client to the broker-reported paper account, derives
one canonical journal path, and acquires the account mutation lease immediately
around each broker side effect.  It is intentionally not a distributed fence:
all processes holding the credentials must cooperate with this boundary.
"""

from __future__ import annotations

import os
import stat
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from uuid import UUID

from trading_ai.execution.account_supervisor import (
    AccountLeaseIntegrityError,
    AccountMutationLease,
    canonical_account_executor_journal_path,
    canonical_account_journal_path,
)
from trading_ai.execution.order_journal import (
    DurableOrderJournal,
    OrderJournalError,
    OrderJournalStorageIdentity,
)

_AUDITED_ALPACA_PY_VERSION = "0.43.4"
_AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS = 5.0
_AUDITED_ALPACA_ALLOW_REDIRECTS = False
_HTTP_TRANSPORT_GUARD_ATTRIBUTE = "_trading_ai_http_transport_guard"
_SESSION_DISPATCH_METHODS = (
    "request",
    "prepare_request",
    "merge_environment_settings",
    "send",
    "get_adapter",
    "resolve_redirects",
    "get_redirect_target",
    "rebuild_auth",
    "rebuild_proxies",
    "rebuild_method",
    "should_strip_auth",
    "mount",
)
_HTTP_ADAPTER_DISPATCH_METHODS = (
    "send",
    "get_connection_with_tls_context",
    "cert_verify",
    "request_url",
    "add_headers",
    "build_response",
    "proxy_manager_for",
)
_TRADING_CLIENT_DISPATCH_METHODS = (
    "_request",
    "_one_request",
    "_get_default_headers",
    "_get_auth_headers",
    "get",
    "post",
    "delete",
    "get_account",
    "submit_order",
    "cancel_order_by_id",
)


class PaperAccountAuthorityError(RuntimeError):
    """Raised when paper-account mutation authority cannot be established."""


class PaperAccountIdentityError(PaperAccountAuthorityError):
    """Raised when the broker account identity or status is not trustworthy."""


class PaperAccountDispatchNotStartedError(PaperAccountAuthorityError):
    """Carry a registry-backed proof that the raw SDK method was never entered.

    Constructing this public exception alone proves nothing.  The supervised
    boundary must register the exact instance before a caller may consume it.
    Any failure after ``before_dispatch`` returns is left unregistered because
    the durable CAS has already authorized a mutation and its outcome may be
    ambiguous.
    """

_DISPATCH_NOT_STARTED_LOCK = threading.Lock()
_DISPATCH_NOT_STARTED_REGISTRY: dict[
    int,
    tuple[weakref.ReferenceType[BaseException], str],
] = {}


def paper_account_dispatch_not_started(
    exc: BaseException,
    *,
    operation: str,
) -> bool:
    """Consume a supervised, operation-bound proof that dispatch never started.

    Merely constructing or raising :class:`PaperAccountDispatchNotStartedError`
    does not create this capability.  Only the supervised wrapper registers its
    exact exception while handling a failure before the mutable body is entered.
    The registry is weak, thread-safe and one-shot so a stale exception cannot
    authorize a later retry.
    """

    with _DISPATCH_NOT_STARTED_LOCK:
        proof = _DISPATCH_NOT_STARTED_REGISTRY.pop(id(exc), None)
    return proof is not None and proof[0]() is exc and proof[1] == operation


def _registered_dispatch_not_started(
    exc: Exception,
    *,
    operation: str,
) -> Exception:
    candidate: Exception = exc
    try:
        weakref.ref(candidate)
    except TypeError:
        candidate = PaperAccountDispatchNotStartedError(
            f"paper {operation} did not reach its dispatch boundary"
        )
        candidate.__cause__ = exc

    candidate_id = id(candidate)

    def discard(reference: weakref.ReferenceType[BaseException]) -> None:
        with _DISPATCH_NOT_STARTED_LOCK:
            current = _DISPATCH_NOT_STARTED_REGISTRY.get(candidate_id)
            if current is not None and current[0] is reference:
                _DISPATCH_NOT_STARTED_REGISTRY.pop(candidate_id, None)

    reference = weakref.ref(candidate, discard)
    with _DISPATCH_NOT_STARTED_LOCK:
        _DISPATCH_NOT_STARTED_REGISTRY[candidate_id] = (reference, operation)
    return candidate


class _FixedTimeoutSession:
    """Minimal ``requests.Session`` adapter with one immutable timeout policy."""

    __slots__ = (
        "__allow_redirects",
        "__request_callable",
        "__timeout_seconds",
        "__validate_transport",
    )

    def __init__(
        self,
        *,
        request_callable: Callable[..., Any],
        timeout_seconds: float,
        allow_redirects: bool,
        validate_transport: Callable[[], None],
    ) -> None:
        if not callable(request_callable):
            raise PaperAccountAuthorityError("paper trading HTTP session does not expose request")
        if type(timeout_seconds) is not float or timeout_seconds <= 0:
            raise PaperAccountAuthorityError("paper trading HTTP timeout is invalid")
        if allow_redirects is not False:
            raise PaperAccountAuthorityError("paper trading HTTP redirect policy is invalid")
        if not callable(validate_transport):
            raise PaperAccountAuthorityError("paper trading HTTP transport validator is invalid")
        self.__request_callable = request_callable
        self.__timeout_seconds = timeout_seconds
        self.__allow_redirects = allow_redirects
        self.__validate_transport = validate_transport

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Dispatch once through the captured session request with a fixed timeout."""

        self.__validate_transport()
        kwargs["timeout"] = self.__timeout_seconds
        kwargs["allow_redirects"] = self.__allow_redirects
        return self.__request_callable(method, url, **kwargs)

    def matches(
        self,
        *,
        request_callable: Callable[..., Any],
        timeout_seconds: float,
        allow_redirects: bool,
        validate_transport: Callable[[], None],
    ) -> bool:
        return (
            self.__request_callable is request_callable
            and type(self.__timeout_seconds) is float
            and self.__timeout_seconds == timeout_seconds
            and self.__allow_redirects is allow_redirects
            and allow_redirects is _AUDITED_ALPACA_ALLOW_REDIRECTS
            and self.__validate_transport is validate_transport
        )


@dataclass(frozen=True, slots=True)
class _SessionAdapterGuard:
    prefix: str
    adapter: Any
    retry_policy: Any
    methods: tuple[tuple[str, Any], ...]
    poolmanager: Any
    proxy_manager: Any


@dataclass(frozen=True, slots=True)
class _PaperTradingTargetGuard:
    client_type: type
    base_url: Any
    api_version: str
    methods: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _FixedTimeoutTransportGuard:
    adapter: _FixedTimeoutSession
    request_callable: Callable[..., Any]
    timeout_seconds: float
    allow_redirects: bool
    source_session: Any
    session_routing: Any
    session_adapters: tuple[_SessionAdapterGuard, ...]
    session_hooks: Any
    session_proxies: Any
    session_cookies: Any
    session_headers: Any
    session_methods: tuple[tuple[str, Any], ...]
    validate_transport: Callable[[], None]
    paper_target: _PaperTradingTargetGuard | None


class SupervisedAlpacaPaperClient:
    """Narrow Alpaca paper client that fences submit and cancel operations."""

    __slots__ = (
        "__account_id",
        "__client",
        "__closed",
        "__executor_journal_path",
        "__http_transport_guard",
        "__journal_identity",
        "__journal_path",
        "__persistent_lease",
    )

    def __init__(
        self,
        *,
        client: Any,
        account_id: str,
        persistent_lease: AccountMutationLease | None = None,
        supervisor_root: str | Path | None = None,
        _http_transport_guard: _FixedTimeoutTransportGuard | None = None,
    ) -> None:
        self.__client = client
        self.__account_id = account_id
        self.__closed = False
        self.__http_transport_guard = _http_transport_guard
        _require_single_attempt_transport(
            client,
            http_transport_guard=_http_transport_guard,
        )
        self.__journal_path = canonical_account_journal_path(
            broker="alpaca",
            environment="paper",
            account_id=account_id,
            root=supervisor_root,
        )
        self.__executor_journal_path = canonical_account_executor_journal_path(
            broker="alpaca",
            environment="paper",
            account_id=account_id,
            root=supervisor_root,
        )
        if persistent_lease is not None:
            persistent_lease.validate(
                broker="alpaca",
                environment="paper",
                account_id=account_id,
            )
            if persistent_lease.journal_path != self.__journal_path:
                raise AccountLeaseIntegrityError("persistent paper executor lease has the wrong journal path")
        self.__persistent_lease = persistent_lease
        self.__journal_identity = _prepare_canonical_journal(self.__journal_path)

    @property
    def account_id(self) -> str:
        return self.__account_id

    @property
    def order_journal_path(self) -> Path:
        return self.__journal_path

    @property
    def executor_journal_path(self) -> Path:
        return self.__executor_journal_path

    @property
    def persistent_authority(self) -> bool:
        return self.__persistent_lease is not None and self.__persistent_lease.active

    @property
    def fence_epoch(self) -> int | None:
        lease = self.__persistent_lease
        return None if lease is None else lease.fence.epoch

    def close(self) -> None:
        """Release a process-lifetime executor lease exactly once."""

        if self.__closed:
            return
        self.__closed = True
        lease = self.__persistent_lease
        self.__persistent_lease = None
        if lease is not None:
            lease.release()

    def __enter__(self) -> SupervisedAlpacaPaperClient:
        if self.__closed:
            raise PaperAccountAuthorityError("paper account authority is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def get_account(self) -> Any:
        account, account_id = _validated_active_account(self.__client)
        if account_id != self.__account_id:
            raise PaperAccountIdentityError("paper account identity changed after connection")
        return account

    def list_positions(self) -> Any:
        method = getattr(self.__client, "list_positions", None)
        if not callable(method):
            method = getattr(self.__client, "get_all_positions", None)
        if not callable(method):
            raise PaperAccountAuthorityError("paper client does not expose position reads")
        return method()

    def get_all_positions(self) -> Any:
        return self.list_positions()

    def get_orders(self, *, filter: Any) -> Any:
        return self.__client.get_orders(filter=filter)

    def get_order_by_id(self, order_id: str) -> Any:
        return self.__client.get_order_by_id(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Any:
        return self.__client.get_order_by_client_id(client_order_id)

    def get(self, path: str, params: Mapping[str, object]) -> Any:
        return self.__client.get(path, params)

    def submit_order(
        self,
        order_data: Any,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        mutation_id = _client_order_id(order_data)
        mutation_body_entered = False
        try:
            with self.__mutation(operation="submit", mutation_id=mutation_id):
                mutation_body_entered = True
                if before_dispatch is not None:
                    before_dispatch()
                _require_single_attempt_transport(
                    self.__client,
                    http_transport_guard=self.__http_transport_guard,
                )
                return self.__client.submit_order(order_data)
        except Exception as exc:
            if not mutation_body_entered:
                registered = _registered_dispatch_not_started(
                    exc,
                    operation="submit",
                )
                if registered is exc:
                    raise registered from None
                raise registered from exc
            raise

    def cancel_order_by_id(
        self,
        order_id: str,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        mutation_id = _required_text(order_id, label="order_id")
        mutation_body_entered = False
        try:
            with self.__mutation(operation="cancel", mutation_id=mutation_id):
                mutation_body_entered = True
                if before_dispatch is not None:
                    before_dispatch()
                _require_single_attempt_transport(
                    self.__client,
                    http_transport_guard=self.__http_transport_guard,
                )
                return self.__client.cancel_order_by_id(order_id)
        except Exception as exc:
            if not mutation_body_entered:
                registered = _registered_dispatch_not_started(
                    exc,
                    operation="cancel",
                )
                if registered is exc:
                    raise registered from None
                raise registered from exc
            raise

    @contextmanager
    def __mutation(self, *, operation: str, mutation_id: str) -> Iterator[None]:
        if self.__closed:
            raise PaperAccountAuthorityError("paper account authority is closed")
        persistent = self.__persistent_lease
        lease = persistent or AccountMutationLease.acquire(
            broker="alpaca",
            environment="paper",
            account_id=self.__account_id,
        )
        try:
            if lease.journal_path != self.__journal_path:
                raise AccountLeaseIntegrityError("paper account journal path changed")
            current_identity = _validated_journal_identity(self.__journal_path)
            if current_identity != self.__journal_identity:
                raise AccountLeaseIntegrityError("paper account journal identity changed")
            with lease.mutation(
                broker="alpaca",
                environment="paper",
                account_id=self.__account_id,
                operation=operation,
                mutation_id=mutation_id,
            ):
                _require_single_attempt_transport(
                    self.__client,
                    http_transport_guard=self.__http_transport_guard,
                )
                _account, current_account_id = _validated_active_account(self.__client)
                if current_account_id != self.__account_id:
                    raise PaperAccountIdentityError("paper account identity changed before broker mutation")
                yield
        finally:
            if persistent is None:
                lease.release()


def build_supervised_alpaca_paper_client(
    *,
    api_key: str,
    secret_key: str,
    trading_client_cls: type | None = None,
    supervisor_root: str | Path | None = None,
) -> SupervisedAlpacaPaperClient:
    """Build and bind the only production Alpaca paper SDK client."""

    raw_client = _build_raw_alpaca_paper_client(
        api_key=api_key,
        secret_key=secret_key,
        trading_client_cls=trading_client_cls,
    )
    http_transport_guard = _configured_http_transport_guard(raw_client)
    _require_single_attempt_transport(
        raw_client,
        http_transport_guard=http_transport_guard,
    )
    _account, account_id = _validated_active_account(raw_client)
    return SupervisedAlpacaPaperClient(
        client=raw_client,
        account_id=account_id,
        supervisor_root=supervisor_root,
        _http_transport_guard=http_transport_guard,
    )


def build_exclusive_alpaca_paper_client(
    *,
    api_key: str,
    secret_key: str,
    trading_client_cls: type | None = None,
    supervisor_root: str | Path | None = None,
) -> SupervisedAlpacaPaperClient:
    """Build the credential-owning client for one process-lifetime executor.

    Unlike the compatibility builder above, this constructor acquires the
    account lease once and holds it until :meth:`SupervisedAlpacaPaperClient.close`.
    A second executor for the same account therefore fails before it can expose
    a mutable client.
    """

    raw_client = _build_raw_alpaca_paper_client(
        api_key=api_key,
        secret_key=secret_key,
        trading_client_cls=trading_client_cls,
    )
    http_transport_guard = _configured_http_transport_guard(raw_client)
    _require_single_attempt_transport(
        raw_client,
        http_transport_guard=http_transport_guard,
    )
    _account, account_id = _validated_active_account(raw_client)
    lease = AccountMutationLease.acquire(
        broker="alpaca",
        environment="paper",
        account_id=account_id,
        root=supervisor_root,
    )
    try:
        return SupervisedAlpacaPaperClient(
            client=raw_client,
            account_id=account_id,
            persistent_lease=lease,
            supervisor_root=supervisor_root,
            _http_transport_guard=http_transport_guard,
        )
    except BaseException:
        lease.release()
        raise


def _build_raw_alpaca_paper_client(
    *,
    api_key: str,
    secret_key: str,
    trading_client_cls: type | None,
) -> Any:
    client_cls = trading_client_cls
    if client_cls is None:
        _require_audited_alpaca_py_version()
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - optional package
            raise PaperAccountAuthorityError(
                "alpaca-py is not installed; install the broker optional dependency before real paper access"
            ) from exc
        client_cls = TradingClient
    client = client_cls(api_key=api_key, secret_key=secret_key, paper=True)
    if _is_alpaca_rest_client(client):
        _require_exact_paper_trading_client(client)
        enforce_audited_alpaca_http_transport(client)
    else:
        # Test doubles deliberately need not emulate ``requests.Session``.
        _configure_single_attempt_transport(client)
    return client


def enforce_audited_alpaca_http_transport(client: Any) -> Any:
    """Install the audited one-attempt transport on a real Alpaca REST client.

    Non-SDK test doubles are returned unchanged.  A real ``RESTClient`` passed
    through an injection seam receives the same exact-version, retry and timeout
    contract as the default production constructor, closing the reasonable
    supplied-class bypass without forcing fakes to expose HTTP internals.
    """

    if not _is_alpaca_rest_client(client):
        return client
    _require_audited_alpaca_py_version()
    guard = _configured_http_transport_guard(client)
    if guard is None:
        _configure_single_attempt_transport(client, install_fixed_http_timeout=True)
    else:
        _require_single_attempt_transport(client, http_transport_guard=guard)
    return client


def require_audited_alpaca_http_transport(client: Any) -> None:
    """Fail closed if a real Alpaca REST client's audited guard is not intact."""

    if not _is_alpaca_rest_client(client):
        return
    _require_audited_alpaca_py_version()
    guard = _configured_http_transport_guard(client)
    if guard is None:
        raise PaperAccountAuthorityError("Alpaca SDK HTTP timeout guard is missing")
    _require_single_attempt_transport(client, http_transport_guard=guard)


def _is_alpaca_rest_client(client: Any) -> bool:
    try:
        from alpaca.common.rest import RESTClient
    except ImportError:  # pragma: no cover - optional package or injected fake module
        return False
    return isinstance(client, RESTClient)


def _require_exact_paper_trading_client(client: Any) -> None:
    """Reject every real REST injection except the audited TradingClient."""

    try:
        from alpaca.trading.client import TradingClient
    except ImportError as exc:  # pragma: no cover - optional package
        raise PaperAccountAuthorityError(
            "alpaca-py TradingClient is unavailable for paper access"
        ) from exc
    if type(client) is not TradingClient:
        raise PaperAccountAuthorityError(
            "paper trading REST client type is outside the audited boundary"
        )


def _paper_trading_target_guard(client: Any) -> _PaperTradingTargetGuard | None:
    """Seal the exact official paper endpoint for a real TradingClient."""

    try:
        from alpaca.common.enums import BaseURL
        from alpaca.trading.client import TradingClient
    except ImportError:  # pragma: no cover - optional package
        return None
    if not isinstance(client, TradingClient):
        return None
    guard = _PaperTradingTargetGuard(
        client_type=TradingClient,
        base_url=BaseURL.TRADING_PAPER,
        api_version="v2",
        methods=_capture_dispatch_methods(
            client,
            _TRADING_CLIENT_DISPATCH_METHODS,
            label="TradingClient",
        ),
    )
    _require_paper_trading_target(client, guard)
    return guard


def _require_paper_trading_target(
    client: Any,
    guard: _PaperTradingTargetGuard | None,
) -> None:
    if guard is None:
        return
    if (
        type(client) is not guard.client_type
        or not _dispatch_methods_match(client, guard.methods)
        or getattr(client, "_base_url", None) is not guard.base_url
        or getattr(client, "_sandbox", None) is not True
        or type(getattr(client, "_api_version", None)) is not str
        or client._api_version != guard.api_version
        or getattr(client, "_use_basic_auth", None) is not False
        or getattr(client, "_use_raw_data", None) is not False
        or getattr(client, "_oauth_token", None) is not None
        or type(getattr(client, "_api_key", None)) is not str
        or not client._api_key
        or type(getattr(client, "_secret_key", None)) is not str
        or not client._secret_key
    ):
        raise PaperAccountAuthorityError(
            "paper trading SDK target or authentication mode is not audited"
        )


def _require_audited_alpaca_py_version() -> None:
    try:
        installed_version = package_version("alpaca-py")
    except PackageNotFoundError as exc:  # pragma: no cover - optional package
        raise PaperAccountAuthorityError(
            "alpaca-py is not installed; install the broker optional dependency before real paper access"
        ) from exc
    if installed_version != _AUDITED_ALPACA_PY_VERSION:
        raise PaperAccountAuthorityError("alpaca-py version is outside the audited single-attempt contract")


def _configure_single_attempt_transport(
    client: Any,
    *,
    install_fixed_http_timeout: bool = False,
) -> None:
    """Pin retries and the audited SDK's HTTP timeout before any broker read.

    ``alpaca-py`` 0.43.x defaults to three retries for HTTP 429/504 across all
    methods, including POST and DELETE.  Those retries would run after the
    executor's durable dispatch marker and deadline guard.  The SDK does not
    expose retry controls on ``TradingClient``, so this boundary pins its SDK
    counters, replaces inherited Session state with an owned transport, and
    revalidates both layers fail-closed.
    """

    try:
        client._retry = 0
        client._retry_wait = 0
        client._retry_codes = []
    except (AttributeError, TypeError) as exc:
        raise PaperAccountAuthorityError("paper trading transport cannot disable broker SDK retries") from exc
    http_transport_guard = None
    if install_fixed_http_timeout:
        paper_target = _paper_trading_target_guard(client)
        session, session_adapters, session_methods = _build_audited_requests_session()
        request_callable = getattr(session, "request", None)
        if not callable(request_callable):
            raise PaperAccountAuthorityError("paper trading HTTP session does not expose request")
        validate_transport = partial(
            _require_audited_http_dispatch,
            client,
            paper_target,
            session,
            routing=session.adapters,
            guarded=session_adapters,
            hooks=session.hooks,
            proxies=session.proxies,
            cookies=session.cookies,
            headers=session.headers,
            methods=session_methods,
        )
        adapter = _FixedTimeoutSession(
            request_callable=request_callable,
            timeout_seconds=_AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,
            allow_redirects=_AUDITED_ALPACA_ALLOW_REDIRECTS,
            validate_transport=validate_transport,
        )
        http_transport_guard = _FixedTimeoutTransportGuard(
            adapter=adapter,
            request_callable=request_callable,
            timeout_seconds=_AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS,
            allow_redirects=_AUDITED_ALPACA_ALLOW_REDIRECTS,
            source_session=session,
            session_routing=session.adapters,
            session_adapters=session_adapters,
            session_hooks=session.hooks,
            session_proxies=session.proxies,
            session_cookies=session.cookies,
            session_headers=session.headers,
            session_methods=session_methods,
            validate_transport=validate_transport,
            paper_target=paper_target,
        )
        try:
            client._session = adapter
            setattr(client, _HTTP_TRANSPORT_GUARD_ATTRIBUTE, http_transport_guard)
        except (AttributeError, TypeError) as exc:
            raise PaperAccountAuthorityError("paper trading HTTP timeout cannot be installed") from exc
    _require_single_attempt_transport(
        client,
        http_transport_guard=http_transport_guard,
    )


def _configured_http_transport_guard(client: Any) -> _FixedTimeoutTransportGuard | None:
    guard = getattr(client, _HTTP_TRANSPORT_GUARD_ATTRIBUTE, None)
    if guard is None:
        return None
    if type(guard) is not _FixedTimeoutTransportGuard:
        raise PaperAccountAuthorityError("paper trading HTTP timeout guard is invalid")
    return guard


def _build_audited_requests_session() -> tuple[
    Any,
    tuple[_SessionAdapterGuard, ...],
    tuple[tuple[str, Any], ...],
]:
    """Create a boundary-owned Session with no inherited injection state."""

    try:
        from requests import Session
        from requests.adapters import HTTPAdapter
        from requests.cookies import RequestsCookieJar
        from requests.structures import CaseInsensitiveDict
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - required by audited alpaca-py
        raise PaperAccountAuthorityError(
            "paper trading HTTP adapter dependencies are unavailable"
        ) from exc

    session = Session()
    session.trust_env = False
    session.auth = None
    session.proxies = {}
    session.hooks = {"response": []}
    session.cookies = RequestsCookieJar()
    session.params = {}
    session.stream = False
    session.verify = True
    session.cert = None
    session.headers = CaseInsensitiveDict()
    session.adapters.clear()
    session_methods = _capture_dispatch_methods(
        session,
        _SESSION_DISPATCH_METHODS,
        label="HTTP session",
    )

    guarded: list[_SessionAdapterGuard] = []
    for prefix in ("https://", "http://"):
        retry_policy = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
            other=0,
            allowed_methods=frozenset(),
        )
        adapter = HTTPAdapter(max_retries=retry_policy)
        adapter_methods = _capture_dispatch_methods(
            adapter,
            _HTTP_ADAPTER_DISPATCH_METHODS,
            label="HTTP adapter",
        )
        session.mount(prefix, adapter)
        guarded.append(
            _SessionAdapterGuard(
                prefix=prefix,
                adapter=adapter,
                retry_policy=retry_policy,
                methods=adapter_methods,
                poolmanager=adapter.poolmanager,
                proxy_manager=adapter.proxy_manager,
            )
        )
    result = tuple(guarded)
    _require_requests_session(
        session,
        routing=session.adapters,
        guarded=result,
        hooks=session.hooks,
        proxies=session.proxies,
        cookies=session.cookies,
        headers=session.headers,
        methods=session_methods,
    )
    return session, result, session_methods


def _capture_dispatch_methods(
    instance: Any,
    names: tuple[str, ...],
    *,
    label: str,
) -> tuple[tuple[str, Any], ...]:
    """Capture exact class functions and reject per-instance dispatch seams."""

    instance_state = vars(instance)
    captured: list[tuple[str, Any]] = []
    for name in names:
        class_method = getattr(type(instance), name, None)
        if name in instance_state or not callable(class_method):
            raise PaperAccountAuthorityError(f"paper trading {label} method is not audited")
        captured.append((name, class_method))
    return tuple(captured)


def _require_requests_session(
    session: Any,
    *,
    routing: Any,
    guarded: tuple[_SessionAdapterGuard, ...],
    hooks: Any,
    proxies: Any,
    cookies: Any,
    headers: Any,
    methods: tuple[tuple[str, Any], ...],
) -> None:
    """Fail closed if owned session state or a lower retry policy changed."""

    try:
        from requests import Session
        from requests.adapters import HTTPAdapter
        from requests.cookies import RequestsCookieJar
        from requests.structures import CaseInsensitiveDict
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - required by audited alpaca-py
        raise PaperAccountAuthorityError(
            "paper trading HTTP adapter dependencies are unavailable"
        ) from exc
    if type(session) is not Session or not _dispatch_methods_match(session, methods):
        raise PaperAccountAuthorityError("paper trading HTTP session type is not audited")
    adapters = getattr(session, "adapters", None)
    if adapters is not routing or not isinstance(adapters, Mapping):
        raise PaperAccountAuthorityError("paper trading HTTP adapter routing changed")
    if (
        getattr(session, "trust_env", None) is not False
        or getattr(session, "auth", None) is not None
        or getattr(session, "proxies", None) is not proxies
        or type(proxies) is not dict
        or proxies
        or getattr(session, "hooks", None) is not hooks
        or type(hooks) is not dict
        or tuple(hooks) != ("response",)
        or type(hooks["response"]) is not list
        or hooks["response"]
        or getattr(session, "cookies", None) is not cookies
        or type(cookies) is not RequestsCookieJar
        or len(cookies) != 0
        or getattr(session, "headers", None) is not headers
        or type(headers) is not CaseInsensitiveDict
        or headers
        or type(getattr(session, "params", None)) is not dict
        or session.params
        or getattr(session, "stream", None) is not False
        or getattr(session, "verify", None) is not True
        or getattr(session, "cert", None) is not None
    ):
        raise PaperAccountAuthorityError("paper trading HTTP session guard is not intact")
    current = tuple(adapters.items())
    if len(current) != len(guarded) or not guarded:
        raise PaperAccountAuthorityError("paper trading HTTP adapter routing changed")

    for (prefix, adapter), expected in zip(current, guarded, strict=True):
        retry = getattr(adapter, "max_retries", None)
        if (
            prefix != expected.prefix
            or adapter is not expected.adapter
            or type(adapter) is not HTTPAdapter
            or not _dispatch_methods_match(adapter, expected.methods)
            or retry is not expected.retry_policy
            or type(retry) is not Retry
            or retry.total != 0
            or retry.connect != 0
            or retry.read != 0
            or retry.redirect != 0
            or retry.status != 0
            or retry.other != 0
            or retry.allowed_methods != frozenset()
            or retry.history
            or getattr(adapter, "poolmanager", None) is not expected.poolmanager
            or getattr(adapter, "proxy_manager", None) is not expected.proxy_manager
            or type(expected.proxy_manager) is not dict
            or expected.proxy_manager
        ):
            raise PaperAccountAuthorityError(
                "paper trading HTTP adapter retry guard is not intact"
            )


def _require_audited_http_dispatch(
    client: Any,
    paper_target: _PaperTradingTargetGuard | None,
    session: Any,
    *,
    routing: Any,
    guarded: tuple[_SessionAdapterGuard, ...],
    hooks: Any,
    proxies: Any,
    cookies: Any,
    headers: Any,
    methods: tuple[tuple[str, Any], ...],
) -> None:
    """Revalidate destination and owned transport immediately before HTTP."""

    _require_paper_trading_target(client, paper_target)
    _require_requests_session(
        session,
        routing=routing,
        guarded=guarded,
        hooks=hooks,
        proxies=proxies,
        cookies=cookies,
        headers=headers,
        methods=methods,
    )


def _dispatch_methods_match(
    instance: Any,
    expected: tuple[tuple[str, Any], ...],
) -> bool:
    instance_state = vars(instance)
    return bool(expected) and all(
        name not in instance_state and getattr(type(instance), name, None) is class_method
        for name, class_method in expected
    )


def _require_single_attempt_transport(
    client: Any,
    *,
    http_transport_guard: _FixedTimeoutTransportGuard | None = None,
) -> None:
    retry_attempts = getattr(client, "_retry", None)
    retry_wait = getattr(client, "_retry_wait", None)
    retry_codes = getattr(client, "_retry_codes", None)
    if (
        type(retry_attempts) is not int
        or retry_attempts != 0
        or type(retry_wait) is not int
        or retry_wait != 0
        or type(retry_codes) is not list
        or retry_codes
    ):
        raise PaperAccountAuthorityError("paper trading transport retry policy is not single-attempt")
    if http_transport_guard is None:
        return
    adapter = http_transport_guard.adapter
    if (
        type(adapter) is not _FixedTimeoutSession
        or getattr(client, _HTTP_TRANSPORT_GUARD_ATTRIBUTE, None) is not http_transport_guard
        or getattr(client, "_session", None) is not adapter
        or type(http_transport_guard.timeout_seconds) is not float
        or http_transport_guard.timeout_seconds != _AUDITED_ALPACA_HTTP_TIMEOUT_SECONDS
        or http_transport_guard.allow_redirects is not _AUDITED_ALPACA_ALLOW_REDIRECTS
        or not adapter.matches(
            request_callable=http_transport_guard.request_callable,
            timeout_seconds=http_transport_guard.timeout_seconds,
            allow_redirects=http_transport_guard.allow_redirects,
            validate_transport=http_transport_guard.validate_transport,
        )
    ):
        raise PaperAccountAuthorityError("paper trading HTTP timeout guard is not intact")
    _require_paper_trading_target(client, http_transport_guard.paper_target)
    _require_requests_session(
        http_transport_guard.source_session,
        routing=http_transport_guard.session_routing,
        guarded=http_transport_guard.session_adapters,
        hooks=http_transport_guard.session_hooks,
        proxies=http_transport_guard.session_proxies,
        cookies=http_transport_guard.session_cookies,
        headers=http_transport_guard.session_headers,
        methods=http_transport_guard.session_methods,
    )


def _validated_active_account(client: Any) -> tuple[Any, str]:
    get_account = getattr(client, "get_account", None)
    if not callable(get_account):
        raise PaperAccountIdentityError("paper client does not expose account identity")
    account = get_account()
    raw_account_id = _field(account, "id")
    if isinstance(raw_account_id, bool) or raw_account_id is None:
        raise PaperAccountIdentityError("paper account id is missing or invalid")
    try:
        account_id = str(UUID(str(raw_account_id).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise PaperAccountIdentityError("paper account id must be a canonical UUID") from exc

    raw_status = _field(account, "status")
    status_value = getattr(raw_status, "value", raw_status)
    status = str(status_value).strip().lower() if status_value is not None else ""
    if status.startswith("accountstatus."):
        status = status.rsplit(".", 1)[-1]
    if status != "active":
        raise PaperAccountIdentityError("paper account is not active")
    return account, account_id


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _client_order_id(order_data: Any) -> str:
    value = _field(order_data, "client_order_id")
    return _required_text(value, label="client_order_id")


def _required_text(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise PaperAccountAuthorityError(f"{label} must be a string")
    clean = value.strip()
    if not clean or len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise PaperAccountAuthorityError(f"{label} is invalid")
    return clean


def _prepare_canonical_journal(path: Path) -> OrderJournalStorageIdentity:
    try:
        path.lstat()
    except FileNotFoundError:
        existed = False
    except OSError as exc:
        raise PaperAccountAuthorityError("cannot inspect canonical paper journal") from exc
    else:
        existed = True
        _validate_private_journal_file(path)

    try:
        journal = DurableOrderJournal(path)
        if not existed:
            os.chmod(path, 0o600, follow_symlinks=False)
        _validate_private_journal_file(path)
        return journal.storage_identity()
    except (OSError, OrderJournalError) as exc:
        raise PaperAccountAuthorityError("canonical paper journal is unavailable") from exc


def _validated_journal_identity(path: Path) -> OrderJournalStorageIdentity:
    try:
        _validate_private_journal_file(path)
        return DurableOrderJournal(path).storage_identity()
    except (OSError, OrderJournalError) as exc:
        raise AccountLeaseIntegrityError("canonical paper journal cannot be trusted") from exc


def _validate_private_journal_file(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PaperAccountAuthorityError("canonical paper journal must be a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise PaperAccountAuthorityError("canonical paper journal ownership is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PaperAccountAuthorityError("canonical paper journal permissions are unsafe")
