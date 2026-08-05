"""Private Unix-socket transport for the single paper-account executor.

The protocol is deliberately small and JSON-only.  It never uses pickle,
dynamic imports, shell commands, TCP, redirects, or caller-selected endpoints.
One request is accepted per connection. Bounded reader threads validate frames,
while the thread calling ``serve_once`` remains the only thread that invokes the
broker-facing application.

This module is transport only.  The broker application layered on top remains
responsible for durable intent idempotency, broker-first recovery, risk checks,
and deciding which operations are mutable.
"""

from __future__ import annotations

import grp
import json
import math
import os
import pwd
import re
import socket
import stat
import struct
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread
from typing import Any

IPC_SCHEMA_VERSION = 4
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DEADLINE_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_FRAME_TIMEOUT_SECONDS = 1.0
DEFAULT_EXECUTOR_SERVICE_USER = "trading-ai-paper-executor"
DEFAULT_EXECUTOR_IPC_GROUP = "trading-ai-paper-ipc"
DEFAULT_EXECUTOR_RUNTIME_DIRECTORY = Path("/run/trading-ai-paper")
_FRAME_HEADER_BYTES = 4
_MAX_PENDING_FRAME_READERS = 16
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_UNKNOWN_REQUEST_ID = "0" * 32
DEFAULT_MUTATING_OPERATIONS = frozenset(
    {
        "cancel_order",
        "latch_kill_switch",
        "mark_order_reconciled",
        "start_safe_flatten",
        "submit_order",
    }
)


class PaperExecutorIpcError(RuntimeError):
    """Base class for executor transport failures."""


class PaperExecutorUnavailableError(PaperExecutorIpcError):
    """Raised when no request could be completed through the executor."""


class PaperExecutorOutcomeUnknownError(PaperExecutorIpcError):
    """Raised after a mutable request may have reached the executor."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        operation: str,
        phase: str,
        target: ExecutorTarget | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.operation = operation
        self.phase = phase
        self.target = target


class PaperExecutorProtocolError(PaperExecutorIpcError):
    """Raised when a peer violates the bounded JSON protocol."""


class PaperExecutorAlreadyRunningError(PaperExecutorIpcError):
    """Raised when the configured socket path is already occupied."""


class PaperExecutorRemoteError(PaperExecutorIpcError):
    """A safe, classified error returned by the executor application."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class PaperExecutorRequestError(PaperExecutorIpcError):
    """Classified application rejection safe to return over IPC."""

    def __init__(self, code: str, message: str) -> None:
        clean_code = _validated_error_code(code)
        clean_message = _validated_safe_message(message)
        super().__init__(clean_message)
        self.code = clean_code
        self.message = clean_message


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ExecutorTarget:
    account_scope_sha256: str
    policy_sha256: str
    authz_policy_sha256: str
    run_id: str
    fence_epoch: int


@dataclass(frozen=True)
class PaperExecutorRequest:
    """Validated request envelope delivered to the executor application."""

    request_id: str
    operation: str
    deadline_unix_ms: int
    deadline_monotonic: float
    payload: Mapping[str, Any]
    peer: PeerCredentials
    target: ExecutorTarget | None = None


@dataclass(frozen=True)
class SocketIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class ExecutorSocketTrust:
    server_uid: int
    socket_gid: int
    runtime_mode: int = 0o750
    socket_mode: int = 0o660


@dataclass
class _PendingFrameReader:
    connection: socket.socket
    peer: PeerCredentials
    thread: Thread | None = None


@dataclass(frozen=True)
class _ReaderResult:
    connection: socket.socket
    peer: PeerCredentials
    request_id: str
    request: PaperExecutorRequest | None
    error: Exception | None


def default_paper_executor_socket_path() -> Path:
    """Return the fixed system-level socket without trusting caller state."""

    return DEFAULT_EXECUTOR_RUNTIME_DIRECTORY / "alpaca-paper-executor.sock"


def default_paper_executor_socket_trust() -> ExecutorSocketTrust:
    """Resolve the statically provisioned daemon and IPC group identities."""

    try:
        server_uid = pwd.getpwnam(DEFAULT_EXECUTOR_SERVICE_USER).pw_uid
        socket_gid = grp.getgrnam(DEFAULT_EXECUTOR_IPC_GROUP).gr_gid
    except (KeyError, TypeError, AttributeError) as exc:
        raise PaperExecutorUnavailableError("paper executor service identity is not provisioned") from exc
    return _validated_socket_trust(ExecutorSocketTrust(server_uid=server_uid, socket_gid=socket_gid))


class PaperExecutorClient:
    """One-shot client with no automatic retry and strict response validation."""

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        mutating_operations: frozenset[str] = DEFAULT_MUTATING_OPERATIONS,
        socket_trust: ExecutorSocketTrust | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        using_system_default = socket_path is None
        if not using_system_default and socket_trust is None:
            raise PaperExecutorProtocolError("explicit executor socket path requires explicit socket trust")
        self.socket_path = Path(socket_path) if socket_path is not None else default_paper_executor_socket_path()
        self.socket_trust = _validated_socket_trust(socket_trust or default_paper_executor_socket_trust())
        self.timeout_seconds = _validated_timeout(timeout_seconds)
        self.mutating_operations = frozenset(_validated_operation(item) for item in mutating_operations)
        self._clock = clock

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
        target: ExecutorTarget | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_operation = _validated_operation(operation)
        timeout = self.timeout_seconds if timeout_seconds is None else _validated_timeout(timeout_seconds)
        clean_request_id = uuid.uuid4().hex if request_id is None else _validated_request_id(request_id)
        clean_target = None if target is None else _validated_target(target)
        if clean_operation in self.mutating_operations and clean_target is None:
            raise PaperExecutorProtocolError("mutating executor request requires an exact server target")
        now = self._clock()
        if not math.isfinite(now):
            raise PaperExecutorProtocolError("client clock is not finite")
        deadline_unix_ms = int((now + timeout) * 1_000)
        request = {
            "schema_version": IPC_SCHEMA_VERSION,
            "request_id": clean_request_id,
            "operation": clean_operation,
            "deadline_unix_ms": deadline_unix_ms,
            "payload": dict(payload or {}),
            "target": None if clean_target is None else _target_to_dict(clean_target),
        }
        encoded = _encode_frame(request, max_bytes=MAX_REQUEST_BYTES, label="request")
        identity = _validated_socket_identity(self.socket_path, trust=self.socket_trust)
        may_have_sent = False
        phase = "connect"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(str(self.socket_path))
            phase = "authenticate"
            server_peer = _peer_credentials(connection)
            if server_peer.uid != self.socket_trust.server_uid:
                raise PaperExecutorProtocolError("executor server uid is not trusted")
            if _validated_socket_identity(self.socket_path, trust=self.socket_trust) != identity:
                raise PaperExecutorProtocolError("executor socket changed during connect")
            phase = "send"
            may_have_sent = True
            connection.sendall(encoded)
            phase = "receive"
            response = _receive_frame(
                connection,
                max_bytes=MAX_RESPONSE_BYTES,
                label="response",
            )
            phase = "validate"
            parsed = _validated_response(response, request_id=clean_request_id)
            if clean_target is not None:
                _validate_server_matches_target(parsed["server"], clean_target)
        except PaperExecutorProtocolError as exc:
            if may_have_sent and clean_operation in self.mutating_operations:
                raise PaperExecutorOutcomeUnknownError(
                    "paper executor returned an invalid response after mutation dispatch",
                    request_id=clean_request_id,
                    operation=clean_operation,
                    phase=phase,
                    target=clean_target,
                ) from exc
            raise
        except (OSError, TimeoutError) as exc:
            if may_have_sent and clean_operation in self.mutating_operations:
                raise PaperExecutorOutcomeUnknownError(
                    "paper executor response was not received; reconcile by durable intent before retry",
                    request_id=clean_request_id,
                    operation=clean_operation,
                    phase=phase,
                    target=clean_target,
                ) from exc
            raise PaperExecutorUnavailableError("paper executor is unavailable") from exc
        finally:
            connection.close()

        if not parsed["ok"]:
            error = parsed["error"]
            if clean_operation in self.mutating_operations and error["code"] in {
                "command_outcome_unknown",
                "executor_internal_error",
            }:
                raise PaperExecutorOutcomeUnknownError(
                    "paper executor reported an unresolved mutation outcome",
                    request_id=clean_request_id,
                    operation=clean_operation,
                    phase="remote_result",
                    target=clean_target,
                )
            raise PaperExecutorRemoteError(str(error["code"]), str(error["message"]))
        result = parsed["payload"]
        if not isinstance(result, dict):  # defensive; validated above
            raise PaperExecutorProtocolError("executor response payload must be an object")
        return result


class PaperExecutorServer:
    """AF_UNIX server with bounded readers and a serial application dispatcher."""

    def __init__(
        self,
        *,
        handler: Callable[[PaperExecutorRequest], Mapping[str, Any]],
        peer_admitter: Callable[[PeerCredentials], bool],
        socket_gid: int,
        socket_path: str | Path | None = None,
        metadata: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        mutating_operations: frozenset[str] = DEFAULT_MUTATING_OPERATIONS,
        request_timeout_seconds: float = DEFAULT_FRAME_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else default_paper_executor_socket_path()
        self._handler = handler
        if not callable(peer_admitter):
            raise PaperExecutorProtocolError("executor peer admitter is required")
        if os.getuid() != os.geteuid():
            raise PaperExecutorProtocolError("executor server uid transition is forbidden")
        self._peer_admitter = peer_admitter
        self._socket_trust = _validated_socket_trust(ExecutorSocketTrust(server_uid=os.getuid(), socket_gid=socket_gid))
        self._metadata = metadata or {}
        self._mutating_operations = frozenset(_validated_operation(item) for item in mutating_operations)
        self._request_timeout_seconds = _validated_timeout(request_timeout_seconds)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._listener: socket.socket | None = None
        self._socket_identity: SocketIdentity | None = None
        self._reader_lock = Lock()
        self._closing = Event()
        self._pending_readers: dict[int, _PendingFrameReader] = {}
        self._reader_threads: set[Thread] = set()
        self._mutation_results: Queue[_ReaderResult] = Queue(maxsize=_MAX_PENDING_FRAME_READERS)
        self._observation_results: Queue[_ReaderResult] = Queue(maxsize=_MAX_PENDING_FRAME_READERS)
        self._active_connections: set[socket.socket] = set()

    @property
    def started(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        if self._listener is not None:
            raise PaperExecutorAlreadyRunningError("paper executor server is already started")
        with self._reader_lock:
            if self._pending_readers or self._reader_threads or self._active_connections:
                raise PaperExecutorUnavailableError("paper executor readers are still shutting down")
            self._closing.clear()
        _validate_unix_socket_path(self.socket_path)
        _validate_runtime_directory(self.socket_path.parent, trust=self._socket_trust)
        _require_socket_path_absent(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: SocketIdentity | None = None
        try:
            previous_umask = os.umask(0o777)
            try:
                listener.bind(str(self.socket_path))
            finally:
                os.umask(previous_umask)
            bound_identity = _socket_path_identity(self.socket_path)
            os.chown(
                self.socket_path,
                self._socket_trust.server_uid,
                self._socket_trust.socket_gid,
                follow_symlinks=False,
            )
            os.chmod(
                self.socket_path,
                self._socket_trust.socket_mode,
                follow_symlinks=False,
            )
            listener.listen(16)
            listener.settimeout(0.25)
            identity = _validated_socket_identity(
                self.socket_path,
                trust=self._socket_trust,
            )
        except BaseException:
            listener.close()
            # Never unlink a socket merely because bind failed: another
            # executor could have won the race after the path check.  Once
            # this listener has bound, however, the inode belongs to us and
            # may be cleaned up safely if later setup fails.
            if bound_identity is not None:
                _unlink_owned_socket(self.socket_path, expected=bound_identity)
            raise
        self._listener = listener
        self._socket_identity = identity

    def serve_once(self) -> None:
        self._reap_finished_readers()
        if self._dispatch_next_result():
            return
        listener = self._listener
        if listener is None:
            raise PaperExecutorUnavailableError("paper executor server is not started")
        try:
            connection, _address = listener.accept()
        except TimeoutError:
            if self._dispatch_next_result():
                return
            raise
        reader_started = False
        try:
            connection.settimeout(self._request_timeout_seconds)
            peer = _peer_credentials(connection)
            try:
                admitted = self._peer_admitter(peer)
            except Exception:
                admitted = False
            if admitted is not True:
                return
            reader_started = self._start_frame_reader(connection, peer=peer)
        finally:
            if not reader_started:
                connection.close()
        self._dispatch_next_result()

    def serve_forever(self, stop_event: Event) -> None:
        if self._listener is None:
            self.start()
        while not stop_event.is_set():
            try:
                self.serve_once()
            except TimeoutError:
                continue
            except OSError as exc:
                if stop_event.is_set():
                    break
                raise PaperExecutorUnavailableError("paper executor accept failed") from exc

    def close(self) -> None:
        with self._reader_lock:
            self._closing.set()
            listener = self._listener
            identity = self._socket_identity
            self._listener = None
            self._socket_identity = None
            pending = tuple(self._pending_readers.values())
            self._pending_readers.clear()
            threads = tuple(self._reader_threads)
            self._reader_threads.clear()
            active_connections = tuple(self._active_connections)
        if listener is not None:
            listener.close()
        for pending_reader in pending:
            _shutdown_connection(pending_reader.connection)
        for connection in active_connections:
            _shutdown_connection(connection)
        self._drain_result_queues()
        running_thread = current_thread()
        for thread in threads:
            if thread is not running_thread:
                thread.join()
        self._drain_result_queues()
        if identity is not None:
            _unlink_owned_socket(self.socket_path, expected=identity)

    def __enter__(self) -> PaperExecutorServer:
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _start_frame_reader(
        self,
        connection: socket.socket,
        *,
        peer: PeerCredentials,
    ) -> bool:
        pending = _PendingFrameReader(connection=connection, peer=peer)
        thread = Thread(
            target=self._read_frame,
            args=(pending,),
            name=f"paper-executor-frame-{peer.uid}",
            daemon=False,
        )
        pending.thread = thread
        with self._reader_lock:
            if (
                self._closing.is_set()
                or peer.uid in self._pending_readers
                or len(self._pending_readers) >= _MAX_PENDING_FRAME_READERS
            ):
                return False
            self._pending_readers[peer.uid] = pending
            self._reader_threads.add(thread)
            try:
                thread.start()
            except RuntimeError as exc:
                self._pending_readers.pop(peer.uid, None)
                self._reader_threads.discard(thread)
                raise PaperExecutorUnavailableError("paper executor frame reader could not start") from exc
        return True

    def _read_frame(self, pending: _PendingFrameReader) -> None:
        result = self._read_and_validate_frame(pending.connection, peer=pending.peer)
        queue = (
            self._mutation_results
            if result.request is not None and result.request.operation in self._mutating_operations
            else self._observation_results
        )
        close_connection = False
        with self._reader_lock:
            current = self._pending_readers.get(pending.peer.uid)
            if current is not pending or self._closing.is_set():
                close_connection = True
            else:
                try:
                    queue.put_nowait(result)
                except Full:
                    close_connection = True
                    self._pending_readers.pop(pending.peer.uid, None)
        if close_connection:
            _shutdown_connection(pending.connection)

    def _read_and_validate_frame(
        self,
        connection: socket.socket,
        *,
        peer: PeerCredentials,
    ) -> _ReaderResult:
        request_id = _UNKNOWN_REQUEST_ID
        try:
            request = _receive_frame(
                connection,
                max_bytes=MAX_REQUEST_BYTES,
                label="request",
            )
            # Echo a syntactically valid request id even when a later field
            # (for example an expired deadline) rejects the request.  This
            # lets the client authenticate the response-to-request binding.
            if isinstance(request, dict) and "request_id" in request:
                request_id = _validated_request_id(request["request_id"])
            wall_now = self._clock()
            validated = _validated_request(request, now=wall_now)
            monotonic_now = self._monotonic_clock()
            if not math.isfinite(monotonic_now):
                raise PaperExecutorProtocolError("executor monotonic clock is not finite")
            deadline_monotonic = monotonic_now + (validated["deadline_unix_ms"] / 1_000.0 - wall_now)
            if not math.isfinite(deadline_monotonic):
                raise PaperExecutorProtocolError("executor monotonic deadline is invalid")
            request_id = validated["request_id"]
            target = validated["target"]
            application_request = PaperExecutorRequest(
                request_id=validated["request_id"],
                operation=validated["operation"],
                deadline_unix_ms=validated["deadline_unix_ms"],
                deadline_monotonic=deadline_monotonic,
                payload=validated["payload"],
                peer=peer,
                target=target,
            )
            return _ReaderResult(
                connection=connection,
                peer=peer,
                request_id=request_id,
                request=application_request,
                error=None,
            )
        except Exception as exc:
            return _ReaderResult(
                connection=connection,
                peer=peer,
                request_id=request_id,
                request=None,
                error=exc,
            )

    def _dispatch_next_result(self) -> bool:
        reader_result = self._next_reader_result()
        if reader_result is None:
            return False
        with self._reader_lock:
            if self._closing.is_set():
                _shutdown_connection(reader_result.connection)
                return True
            self._active_connections.add(reader_result.connection)
        try:
            self._handle_reader_result(reader_result)
        finally:
            reader_result.connection.close()
            with self._reader_lock:
                self._pending_readers.pop(reader_result.peer.uid, None)
                self._active_connections.discard(reader_result.connection)
            self._reap_finished_readers()
        return True

    def _next_reader_result(self) -> _ReaderResult | None:
        for queue in (self._mutation_results, self._observation_results):
            try:
                return queue.get_nowait()
            except Empty:
                continue
        return None

    def _handle_reader_result(self, reader_result: _ReaderResult) -> None:
        request_id = reader_result.request_id
        try:
            if reader_result.error is not None:
                raise reader_result.error
            application_request = reader_result.request
            if application_request is None:
                raise PaperExecutorProtocolError("executor reader result is incomplete")
            self._revalidate_deadline(application_request)
            target = application_request.target
            if application_request.operation in self._mutating_operations:
                if target is None:
                    raise PaperExecutorRequestError(
                        "target_required",
                        "executor mutation target precondition is required",
                    )
                _validate_server_matches_target(self._server_metadata(), target)
            elif target is not None:
                _validate_server_matches_target(self._server_metadata(), target)
            result = self._handler(application_request)
            if not isinstance(result, Mapping):
                raise PaperExecutorProtocolError("executor handler must return an object")
            response = {
                "schema_version": IPC_SCHEMA_VERSION,
                "request_id": request_id,
                "ok": True,
                "payload": dict(result),
                "error": None,
                "server": self._server_metadata(),
            }
        except PaperExecutorRequestError as exc:
            response = _error_response(
                request_id,
                code=exc.code,
                message=exc.message,
                metadata=self._server_metadata(),
            )
        except PaperExecutorProtocolError:
            response = _error_response(
                request_id,
                code="protocol_rejected",
                message="executor request violated the bounded protocol",
                metadata=self._server_metadata(),
            )
        except Exception:
            response = _error_response(
                request_id,
                code="executor_internal_error",
                message="executor could not complete the request",
                metadata=self._server_metadata(),
            )
        try:
            reader_result.connection.sendall(_encode_frame(response, max_bytes=MAX_RESPONSE_BYTES, label="response"))
        except (OSError, PaperExecutorProtocolError):
            # The client may have timed out.  Never replay an application call
            # merely because its response could not be delivered.
            return

    def _revalidate_deadline(self, request: PaperExecutorRequest) -> None:
        monotonic_now = self._monotonic_clock()
        if not math.isfinite(monotonic_now):
            raise PaperExecutorProtocolError("executor monotonic clock is not finite")
        if request.deadline_monotonic <= monotonic_now:
            raise PaperExecutorRequestError(
                "request_expired",
                "executor request deadline expired",
            )

    def _reap_finished_readers(self) -> None:
        with self._reader_lock:
            finished = tuple(thread for thread in self._reader_threads if not thread.is_alive())
            self._reader_threads.difference_update(finished)
        for thread in finished:
            thread.join()

    def _drain_result_queues(self) -> None:
        while True:
            reader_result = self._next_reader_result()
            if reader_result is None:
                return
            _shutdown_connection(reader_result.connection)

    def _server_metadata(self) -> dict[str, Any]:
        raw = self._metadata() if callable(self._metadata) else self._metadata
        if not isinstance(raw, Mapping):
            raise PaperExecutorProtocolError("executor metadata must be an object")
        metadata = dict(raw)
        metadata.setdefault("pid", os.getpid())
        _encode_json(metadata, max_bytes=MAX_REQUEST_BYTES, label="server metadata")
        return metadata


def _validated_request(value: Any, *, now: float) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError("executor request must be an object")
    expected = {
        "schema_version",
        "request_id",
        "operation",
        "deadline_unix_ms",
        "payload",
        "target",
    }
    if set(value) != expected:
        raise PaperExecutorProtocolError("executor request fields are invalid")
    if value["schema_version"] != IPC_SCHEMA_VERSION:
        raise PaperExecutorProtocolError("executor schema version is unsupported")
    request_id = _validated_request_id(value["request_id"])
    operation = _validated_operation(value["operation"])
    deadline = value["deadline_unix_ms"]
    if type(deadline) is not int:
        raise PaperExecutorProtocolError("executor deadline must be an integer")
    if not math.isfinite(now):
        raise PaperExecutorProtocolError("executor clock is not finite")
    now_ms = int(now * 1_000)
    if deadline <= now_ms:
        raise PaperExecutorRequestError("request_expired", "executor request deadline expired")
    if deadline > now_ms + int(MAX_DEADLINE_SECONDS * 1_000):
        raise PaperExecutorProtocolError("executor request deadline is too far in the future")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise PaperExecutorProtocolError("executor request payload must be an object")
    raw_target = value["target"]
    target = None if raw_target is None else _validated_target(raw_target)
    return {
        "request_id": request_id,
        "operation": operation,
        "deadline_unix_ms": deadline,
        "payload": payload,
        "target": target,
    }


def _validated_response(value: Any, *, request_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError("executor response must be an object")
    expected = {"schema_version", "request_id", "ok", "payload", "error", "server"}
    if set(value) != expected:
        raise PaperExecutorProtocolError("executor response fields are invalid")
    if value["schema_version"] != IPC_SCHEMA_VERSION or value["request_id"] != request_id:
        raise PaperExecutorProtocolError("executor response identity does not match request")
    if type(value["ok"]) is not bool or not isinstance(value["server"], dict):
        raise PaperExecutorProtocolError("executor response status is invalid")
    if value["ok"]:
        if not isinstance(value["payload"], dict) or value["error"] is not None:
            raise PaperExecutorProtocolError("executor success response is invalid")
    else:
        error = value["error"]
        if value["payload"] is not None or not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise PaperExecutorProtocolError("executor error response is invalid")
        _validated_error_code(error["code"])
        _validated_safe_message(error["message"])
    return value


def _validated_target(value: ExecutorTarget | Mapping[str, Any]) -> ExecutorTarget:
    if isinstance(value, ExecutorTarget):
        raw: Mapping[str, Any] = {
            "account_scope_sha256": value.account_scope_sha256,
            "policy_sha256": value.policy_sha256,
            "authz_policy_sha256": value.authz_policy_sha256,
            "run_id": value.run_id,
            "fence_epoch": value.fence_epoch,
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise PaperExecutorProtocolError("executor target must be an object")
    expected = {
        "account_scope_sha256",
        "policy_sha256",
        "authz_policy_sha256",
        "run_id",
        "fence_epoch",
    }
    if set(raw) != expected:
        raise PaperExecutorProtocolError("executor target fields are invalid")
    fence_epoch = raw["fence_epoch"]
    if type(fence_epoch) is not int or fence_epoch < 1:
        raise PaperExecutorProtocolError("executor target fence is invalid")
    run_id = _validated_request_id(raw["run_id"])
    if run_id == _UNKNOWN_REQUEST_ID:
        raise PaperExecutorProtocolError("executor target run id is invalid")
    return ExecutorTarget(
        account_scope_sha256=_validated_sha256(
            raw["account_scope_sha256"],
            label="target account scope",
        ),
        policy_sha256=_validated_sha256(
            raw["policy_sha256"],
            label="target policy",
        ),
        authz_policy_sha256=_validated_sha256(
            raw["authz_policy_sha256"],
            label="target authorization policy",
        ),
        run_id=run_id,
        fence_epoch=fence_epoch,
    )


def _target_to_dict(value: ExecutorTarget) -> dict[str, Any]:
    return {
        "account_scope_sha256": value.account_scope_sha256,
        "policy_sha256": value.policy_sha256,
        "authz_policy_sha256": value.authz_policy_sha256,
        "run_id": value.run_id,
        "fence_epoch": value.fence_epoch,
    }


def _validate_server_matches_target(
    metadata: Mapping[str, Any],
    target: ExecutorTarget,
) -> None:
    try:
        observed = ExecutorTarget(
            account_scope_sha256=_validated_sha256(
                metadata["account_scope_sha256"],
                label="server account scope",
            ),
            policy_sha256=_validated_sha256(
                metadata["policy_sha256"],
                label="server policy",
            ),
            authz_policy_sha256=_validated_sha256(
                metadata["authz_policy_sha256"],
                label="server authorization policy",
            ),
            run_id=_validated_request_id(metadata["run_id"]),
            fence_epoch=metadata["fence_epoch"],
        )
    except (KeyError, TypeError) as exc:
        raise PaperExecutorProtocolError("executor server identity is incomplete") from exc
    if type(observed.fence_epoch) is not int or observed.fence_epoch < 1:
        raise PaperExecutorProtocolError("executor server fence is invalid")
    if observed != target:
        raise PaperExecutorProtocolError("executor server identity does not match mutation target")


def _error_response(
    request_id: str,
    *,
    code: str,
    message: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": IPC_SCHEMA_VERSION,
        "request_id": request_id,
        "ok": False,
        "payload": None,
        "error": {
            "code": _validated_error_code(code),
            "message": _validated_safe_message(message),
        },
        "server": dict(metadata),
    }


def _encode_frame(value: Any, *, max_bytes: int, label: str) -> bytes:
    body = _encode_json(value, max_bytes=max_bytes, label=label)
    return struct.pack("!I", len(body)) + body


def _encode_json(value: Any, *, max_bytes: int, label: str) -> bytes:
    try:
        body = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PaperExecutorProtocolError(f"{label} is not strict JSON") from exc
    if not body or len(body) > max_bytes:
        raise PaperExecutorProtocolError(f"{label} exceeds its size limit")
    return body


def _receive_frame(connection: socket.socket, *, max_bytes: int, label: str) -> Any:
    header = _receive_exact(connection, _FRAME_HEADER_BYTES, label=label)
    (size,) = struct.unpack("!I", header)
    if size < 2 or size > max_bytes:
        raise PaperExecutorProtocolError(f"{label} frame size is invalid")
    raw = _receive_exact(connection, size, label=label)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except PaperExecutorProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperExecutorProtocolError(f"{label} is invalid JSON") from exc


def _receive_exact(connection: socket.socket, size: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = connection.recv(remaining)
        except TimeoutError as exc:
            raise TimeoutError(f"{label} timed out") from exc
        if not chunk:
            raise PaperExecutorProtocolError(f"{label} ended before its frame was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PaperExecutorProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise PaperExecutorProtocolError(f"non-finite JSON constant is forbidden: {value}")


def _validated_operation(value: Any) -> str:
    if type(value) is not str or _OPERATION_RE.fullmatch(value) is None:
        raise PaperExecutorProtocolError("executor operation is invalid")
    return value


def _validated_error_code(value: Any) -> str:
    if type(value) is not str or _ERROR_CODE_RE.fullmatch(value) is None:
        raise PaperExecutorProtocolError("executor error code is invalid")
    return value


def _validated_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise PaperExecutorProtocolError(f"executor {label} is invalid") from exc
    if value.lower() != value:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    return value


def _validated_request_id(value: Any) -> str:
    if type(value) is not str or len(value) != 32:
        raise PaperExecutorProtocolError("executor request id is invalid")
    try:
        parsed = uuid.UUID(hex=value)
    except (AttributeError, ValueError) as exc:
        raise PaperExecutorProtocolError("executor request id is invalid") from exc
    if parsed.hex != value:
        raise PaperExecutorProtocolError("executor request id is not canonical")
    return value


def _validated_safe_message(value: Any) -> str:
    if type(value) is not str:
        raise PaperExecutorProtocolError("executor error message is invalid")
    clean = value.strip()
    if not clean or len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise PaperExecutorProtocolError("executor error message is invalid")
    return clean


def _validated_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperExecutorProtocolError("executor timeout must be numeric")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_DEADLINE_SECONDS:
        raise PaperExecutorProtocolError("executor timeout is outside the allowed range")
    return timeout


def _validate_unix_socket_path(path: Path) -> None:
    encoded = os.fsencode(path)
    if not path.is_absolute() or len(encoded) > 100 or b"\x00" in encoded:
        raise PaperExecutorProtocolError("executor socket path is invalid")


def _validated_socket_trust(value: ExecutorSocketTrust) -> ExecutorSocketTrust:
    if not isinstance(value, ExecutorSocketTrust):
        raise PaperExecutorProtocolError("executor socket trust is invalid")
    if type(value.server_uid) is not int or value.server_uid < 1:
        raise PaperExecutorProtocolError("executor server uid is invalid")
    if type(value.socket_gid) is not int or value.socket_gid < 1:
        raise PaperExecutorProtocolError("executor socket group is invalid")
    if (value.runtime_mode, value.socket_mode) != (0o750, 0o660):
        raise PaperExecutorProtocolError("executor socket permission policy is invalid")
    return value


def _validate_runtime_directory(path: Path, *, trust: ExecutorSocketTrust) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PaperExecutorProtocolError("executor runtime directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise PaperExecutorProtocolError("executor runtime directory must be a real directory")
    if (
        metadata.st_uid != trust.server_uid
        or metadata.st_gid != trust.socket_gid
        or stat.S_IMODE(metadata.st_mode) != trust.runtime_mode
    ):
        raise PaperExecutorProtocolError("executor runtime directory permissions are unsafe")


def _require_socket_path_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PaperExecutorProtocolError("executor socket path cannot be inspected") from exc
    raise PaperExecutorAlreadyRunningError("paper executor socket path already exists")


def _socket_path_identity(path: Path) -> SocketIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PaperExecutorProtocolError("bound executor socket cannot be inspected") from exc
    if not stat.S_ISSOCK(metadata.st_mode) or path.is_symlink():
        raise PaperExecutorProtocolError("bound executor path is not a Unix socket")
    return SocketIdentity(device=int(metadata.st_dev), inode=int(metadata.st_ino))


def _validated_socket_identity(
    path: Path,
    *,
    trust: ExecutorSocketTrust,
) -> SocketIdentity:
    _validate_runtime_directory(path.parent, trust=trust)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PaperExecutorUnavailableError("paper executor socket is unavailable") from exc
    if not stat.S_ISSOCK(metadata.st_mode) or path.is_symlink():
        raise PaperExecutorProtocolError("paper executor path is not a Unix socket")
    if (
        metadata.st_uid != trust.server_uid
        or metadata.st_gid != trust.socket_gid
        or stat.S_IMODE(metadata.st_mode) != trust.socket_mode
    ):
        raise PaperExecutorProtocolError("paper executor socket permissions are unsafe")
    return SocketIdentity(device=int(metadata.st_dev), inode=int(metadata.st_ino))


def _unlink_owned_socket(path: Path, *, expected: SocketIdentity | None) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    identity = SocketIdentity(device=int(metadata.st_dev), inode=int(metadata.st_ino))
    if (
        stat.S_ISSOCK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and (expected is None or identity == expected)
    ):
        try:
            path.unlink()
        except OSError:
            return


def _shutdown_connection(connection: socket.socket) -> None:
    with suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)
    connection.close()


def _peer_credentials(connection: socket.socket) -> PeerCredentials:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise PaperExecutorProtocolError("peer credentials are unavailable on this platform")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise PaperExecutorProtocolError("executor peer credentials cannot be verified") from exc
    if pid < 1 or uid < 0 or gid < 0:
        raise PaperExecutorProtocolError("executor peer credentials are invalid")
    return PeerCredentials(pid=pid, uid=uid, gid=gid)
