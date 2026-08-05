from __future__ import annotations

import json
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from trading_ai.execution.paper_executor_ipc import (
    IPC_SCHEMA_VERSION,
    MAX_REQUEST_BYTES,
    ExecutorSocketTrust,
    ExecutorTarget,
    PaperExecutorAlreadyRunningError,
    PaperExecutorClient,
    PaperExecutorOutcomeUnknownError,
    PaperExecutorProtocolError,
    PaperExecutorRemoteError,
    PaperExecutorRequest,
    PaperExecutorRequestError,
    PaperExecutorServer,
    PaperExecutorUnavailableError,
    PeerCredentials,
)

ACCOUNT_SCOPE_SHA256 = "a" * 64
POLICY_SHA256 = "b" * 64
AUTHZ_POLICY_SHA256 = "e" * 64
RUN_ID = uuid.UUID(int=700).hex
FENCE_EPOCH = 7


class RunningServer:
    def __init__(self, server: PaperExecutorServer) -> None:
        self.server = server
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            args=(self.stop,),
            daemon=True,
        )

    def __enter__(self) -> RunningServer:
        self.server.start()
        self.thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.stop.set()
        self.server.close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("paper executor test server did not stop")


class RawResponseServer:
    """One-request AF_UNIX peer used to inject invalid post-dispatch replies."""

    def __init__(
        self,
        path: Path,
        responder: Callable[[socket.socket, dict[str, Any]], None],
    ) -> None:
        self.path = path
        self._responder = responder
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def __enter__(self) -> RawResponseServer:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o660)
        listener.listen(1)
        listener.settimeout(1.0)
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        listener = self._listener
        if listener is not None:
            listener.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise AssertionError("raw executor test server did not stop")
        if self.path.exists():
            self.path.unlink()
        if exc_type is None and self._error is not None:
            raise self._error

    def _serve(self) -> None:
        listener = self._listener
        assert listener is not None
        try:
            connection, _address = listener.accept()
            with connection:
                size = struct.unpack("!I", receive_exact(connection, 4))[0]
                request = json.loads(receive_exact(connection, size))
                if not isinstance(request, dict):
                    raise AssertionError("raw executor request is not an object")
                self._responder(connection, request)
        except BaseException as exc:  # surfaced synchronously by __exit__
            self._error = exc


class PaperExecutorIpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.runtime.mkdir(mode=0o750)
        self.socket_path = self.runtime / "executor.sock"
        self.socket_trust = ExecutorSocketTrust(
            server_uid=os.getuid(),
            socket_gid=os.getgid(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _server(self, handler, **kwargs: object) -> PaperExecutorServer:
        kwargs.setdefault("metadata", self._metadata())
        kwargs.setdefault("peer_admitter", lambda peer: peer.uid == os.getuid())
        kwargs.setdefault("socket_gid", os.getgid())
        kwargs.setdefault("request_timeout_seconds", 1.0)
        return PaperExecutorServer(
            handler=handler,
            socket_path=self.socket_path,
            **kwargs,
        )

    @staticmethod
    def _target(
        *,
        account_scope_sha256: str = ACCOUNT_SCOPE_SHA256,
        policy_sha256: str = POLICY_SHA256,
        authz_policy_sha256: str = AUTHZ_POLICY_SHA256,
        run_id: str = RUN_ID,
        fence_epoch: int = FENCE_EPOCH,
    ) -> ExecutorTarget:
        return ExecutorTarget(
            account_scope_sha256=account_scope_sha256,
            policy_sha256=policy_sha256,
            authz_policy_sha256=authz_policy_sha256,
            run_id=run_id,
            fence_epoch=fence_epoch,
        )

    @staticmethod
    def _metadata(**overrides: object) -> dict[str, object]:
        metadata: dict[str, object] = {
            "account_scope_sha256": ACCOUNT_SCOPE_SHA256,
            "policy_sha256": POLICY_SHA256,
            "authz_policy_sha256": AUTHZ_POLICY_SHA256,
            "run_id": RUN_ID,
            "fence_epoch": FENCE_EPOCH,
            "status": "ready",
        }
        metadata.update(overrides)
        return metadata

    def _client(self, **kwargs: object) -> PaperExecutorClient:
        return PaperExecutorClient(
            socket_path=self.socket_path,
            socket_trust=self.socket_trust,
            timeout_seconds=1.0,
            **kwargs,
        )

    def test_round_trip_is_json_only_and_socket_is_group_scoped(self) -> None:
        observed: list[tuple[str, dict[str, object]]] = []

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            observed.append((request.operation, dict(request.payload)))
            self.assertEqual(request.peer.uid, os.getuid())
            return {"echo": dict(request.payload)}

        server = self._server(handler)
        with RunningServer(server):
            mode = self.socket_path.lstat().st_mode
            self.assertTrue(stat_is_socket(mode))
            self.assertEqual(mode & 0o777, 0o660)
            self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o750)
            result = self._client().request("health", {"probe": "ok"})

        self.assertEqual(result, {"echo": {"probe": "ok"}})
        self.assertEqual(observed, [("health", {"probe": "ok"})])
        self.assertFalse(self.socket_path.exists())

    def test_unknown_peer_is_closed_before_frame_or_handler(self) -> None:
        calls = 0

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        server = self._server(handler, peer_admitter=lambda _peer: False)
        with RunningServer(server):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(1.0)
            try:
                raw.connect(str(self.socket_path))
                self.assertEqual(raw.recv(1), b"")
            finally:
                raw.close()

        self.assertEqual(calls, 0)

    def test_non_boolean_or_failing_peer_admission_fails_closed(self) -> None:
        def raises(_peer: PeerCredentials) -> bool:
            raise RuntimeError("synthetic admitter failure")

        for label, admitter in (
            ("truthy", lambda _peer: "yes"),
            ("exception", raises),
        ):
            with self.subTest(label=label):
                calls = 0

                def handler(_request: PaperExecutorRequest) -> dict[str, object]:
                    nonlocal calls
                    calls += 1
                    return {}

                with RunningServer(self._server(handler, peer_admitter=admitter)):
                    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    raw.settimeout(1.0)
                    try:
                        raw.connect(str(self.socket_path))
                        self.assertEqual(raw.recv(1), b"")
                    finally:
                        raw.close()

                self.assertEqual(calls, 0)

    def test_stalled_observer_does_not_block_other_uid_mutation(self) -> None:
        observer_uid = os.getuid() + 101
        safety_uid = os.getuid() + 102
        observer_admitted = threading.Event()
        mutation_handled = threading.Event()
        handled: list[tuple[str, int, int]] = []

        def admit(peer: PeerCredentials) -> bool:
            if peer.uid == observer_uid:
                observer_admitted.set()
            return peer.uid in {observer_uid, safety_uid}

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            handled.append((request.operation, request.peer.uid, threading.get_ident()))
            mutation_handled.set()
            return {"accepted": True}

        server = self._server(
            handler,
            peer_admitter=admit,
            request_timeout_seconds=2.0,
        )
        observer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        safety = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        safety.settimeout(1.0)
        peer_credentials = iter(
            (
                PeerCredentials(pid=101, uid=observer_uid, gid=os.getgid()),
                PeerCredentials(pid=102, uid=safety_uid, gid=os.getgid()),
            )
        )
        reader_threads: tuple[threading.Thread, ...] = ()
        dispatch_thread_id: int | None = None
        started = 0.0
        handled_after = 0.0
        response: dict[str, Any] = {}
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    side_effect=lambda _connection: next(peer_credentials),
                ),
                RunningServer(server) as running,
            ):
                dispatch_thread_id = running.thread.ident
                observer.connect(str(self.socket_path))
                self.assertTrue(observer_admitted.wait(timeout=1.0))
                self.assertTrue(
                    wait_until(
                        lambda: observer_uid in server._pending_readers,
                        timeout=1.0,
                    )
                )
                with server._reader_lock:
                    reader_threads = tuple(server._reader_threads)
                started = time.monotonic()

                safety.connect(str(self.socket_path))
                send_json_frame(
                    safety,
                    executor_request(
                        "latch_kill_switch",
                        request_id=uuid.UUID(int=80).hex,
                        target=self._target(),
                    ),
                )
                self.assertTrue(mutation_handled.wait(timeout=1.0))
                handled_after = time.monotonic() - started
                self.assertTrue(reader_threads)
                self.assertTrue(all(thread.is_alive() for thread in reader_threads))
                size = struct.unpack("!I", receive_exact(safety, 4))[0]
                response = json.loads(receive_exact(safety, size))
        finally:
            observer.close()
            safety.close()

        self.assertLess(handled_after, 1.0)
        self.assertTrue(response["ok"])
        self.assertEqual(
            handled,
            [("latch_kill_switch", safety_uid, dispatch_thread_id)],
        )
        self.assertTrue(reader_threads)
        self.assertTrue(all(not thread.is_alive() for thread in reader_threads))

    def test_second_connection_for_uid_is_rejected_while_result_is_queued(self) -> None:
        peer_uid = os.getuid() + 201
        peer = PeerCredentials(pid=201, uid=peer_uid, gid=os.getgid())
        calls = 0

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        server = self._server(handler, peer_admitter=lambda candidate: candidate.uid == peer_uid)
        first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second.settimeout(1.0)
        server.start()
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    return_value=peer,
                ),
                patch.object(server, "_dispatch_next_result", return_value=False),
            ):
                first.connect(str(self.socket_path))
                send_json_frame(
                    first,
                    executor_request("health", request_id=uuid.UUID(int=81).hex),
                )
                server.serve_once()
                self.assertTrue(
                    wait_until(
                        lambda: server._observation_results.qsize() == 1,
                        timeout=1.0,
                    )
                )
                with server._reader_lock:
                    self.assertIn(peer_uid, server._pending_readers)

                second.connect(str(self.socket_path))
                server.serve_once()
                self.assertEqual(second.recv(1), b"")
                with server._reader_lock:
                    self.assertIn(peer_uid, server._pending_readers)
                self.assertEqual(server._observation_results.qsize(), 1)
        finally:
            first.close()
            second.close()
            server.close()

        self.assertEqual(calls, 0)

    def test_ready_mutation_is_dispatched_before_ready_observation(self) -> None:
        observer_uid = os.getuid() + 301
        safety_uid = os.getuid() + 302
        peer_credentials = iter(
            (
                PeerCredentials(pid=301, uid=observer_uid, gid=os.getgid()),
                PeerCredentials(pid=302, uid=safety_uid, gid=os.getgid()),
            )
        )
        handled: list[tuple[str, int]] = []
        dispatch_thread = threading.get_ident()

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            handled.append((request.operation, threading.get_ident()))
            return {"operation": request.operation}

        server = self._server(handler, peer_admitter=lambda _peer: True)
        observer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        safety = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        observer.settimeout(1.0)
        safety.settimeout(1.0)
        server.start()
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    side_effect=lambda _connection: next(peer_credentials),
                ),
                patch.object(server, "_dispatch_next_result", return_value=False),
            ):
                observer.connect(str(self.socket_path))
                send_json_frame(
                    observer,
                    executor_request("health", request_id=uuid.UUID(int=82).hex),
                )
                server.serve_once()
                self.assertTrue(
                    wait_until(
                        lambda: server._observation_results.qsize() == 1,
                        timeout=1.0,
                    )
                )

                safety.connect(str(self.socket_path))
                send_json_frame(
                    safety,
                    executor_request(
                        "cancel_order",
                        request_id=uuid.UUID(int=83).hex,
                        target=self._target(),
                    ),
                )
                server.serve_once()
                self.assertTrue(
                    wait_until(
                        lambda: server._mutation_results.qsize() == 1,
                        timeout=1.0,
                    )
                )

            server.serve_once()
            server.serve_once()
            for connection in (safety, observer):
                size = struct.unpack("!I", receive_exact(connection, 4))[0]
                response = json.loads(receive_exact(connection, size))
                self.assertTrue(response["ok"])
        finally:
            observer.close()
            safety.close()
            server.close()

        self.assertEqual(
            handled,
            [
                ("cancel_order", dispatch_thread),
                ("health", dispatch_thread),
            ],
        )

    def test_queued_request_deadline_is_revalidated_before_handler(self) -> None:
        peer_uid = os.getuid() + 401
        peer = PeerCredentials(pid=401, uid=peer_uid, gid=os.getgid())
        calls = 0
        monotonic_times = iter((500.0, 501.0))

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        server = self._server(
            handler,
            peer_admitter=lambda candidate: candidate.uid == peer_uid,
            clock=lambda: 100.0,
            monotonic_clock=lambda: next(monotonic_times),
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        server.start()
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    return_value=peer,
                ),
                patch.object(server, "_dispatch_next_result", return_value=False),
            ):
                client.connect(str(self.socket_path))
                request = executor_request(
                    "health",
                    request_id=uuid.UUID(int=84).hex,
                )
                request["deadline_unix_ms"] = 100_500
                send_json_frame(client, request)
                server.serve_once()
                self.assertTrue(
                    wait_until(
                        lambda: server._observation_results.qsize() == 1,
                        timeout=1.0,
                    )
                )

            server.serve_once()
            size = struct.unpack("!I", receive_exact(client, 4))[0]
            response = json.loads(receive_exact(client, size))
        finally:
            client.close()
            server.close()

        self.assertEqual(calls, 0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "request_expired")

    def test_client_rejects_untrusted_server_uid_before_sending_request(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o660)
        listener.listen(1)
        received: list[bytes] = []

        def observe_connection() -> None:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(1.0)
                received.append(connection.recv(1))

        observer = threading.Thread(target=observe_connection, daemon=True)
        observer.start()
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    return_value=PeerCredentials(
                        pid=1,
                        uid=os.getuid() + 1,
                        gid=os.getgid(),
                    ),
                ),
                self.assertRaisesRegex(PaperExecutorProtocolError, "uid is not trusted"),
            ):
                self._client().request("health")
            observer.join(timeout=2)
            self.assertFalse(observer.is_alive())
            self.assertEqual(received, [b""])
        finally:
            listener.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    def test_explicit_socket_requires_explicit_trust(self) -> None:
        with self.assertRaisesRegex(PaperExecutorProtocolError, "explicit socket trust"):
            PaperExecutorClient(socket_path=self.socket_path)

    def test_client_rejects_socket_mode_mismatch_before_connect(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            with self.assertRaisesRegex(PaperExecutorProtocolError, "permissions"):
                self._client().request("health")
        finally:
            stale.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    def test_server_requires_explicit_peer_admitter(self) -> None:
        with self.assertRaisesRegex(PaperExecutorProtocolError, "peer admitter"):
            PaperExecutorServer(
                handler=lambda _request: {},
                peer_admitter=None,  # type: ignore[arg-type]
                socket_gid=os.getgid(),
                socket_path=self.socket_path,
            )

    def test_application_rejection_is_classified_without_traceback(self) -> None:
        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            raise PaperExecutorRequestError("policy_mismatch", "executor policy hash differs")

        with (
            RunningServer(self._server(handler)),
            self.assertRaises(PaperExecutorRemoteError) as raised,
        ):
            self._client().request("submit_order", target=self._target())

        self.assertEqual(raised.exception.code, "policy_mismatch")
        self.assertNotIn("Traceback", str(raised.exception))

    def test_unexpected_handler_error_is_redacted(self) -> None:
        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            raise RuntimeError("sensitive internal detail")

        with (
            RunningServer(self._server(handler)),
            self.assertRaises(PaperExecutorRemoteError) as raised,
        ):
            self._client().request("health")

        self.assertEqual(raised.exception.code, "executor_internal_error")
        self.assertNotIn("sensitive", str(raised.exception))

    def test_expired_request_never_reaches_handler(self) -> None:
        calls = 0

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        server = self._server(handler, clock=lambda: 200.0)
        with RunningServer(server):
            client = self._client(clock=lambda: 100.0)
            with (
                self.assertRaises(PaperExecutorRemoteError) as raised,
            ):
                client.request("health")

        self.assertEqual(raised.exception.code, "request_expired")
        self.assertEqual(calls, 0)

    def test_server_converts_wire_deadline_to_one_monotonic_budget(self) -> None:
        observed: list[tuple[int, float]] = []

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            observed.append((request.deadline_unix_ms, request.deadline_monotonic))
            return {}

        server = self._server(
            handler,
            clock=lambda: 100.0,
            monotonic_clock=lambda: 500.0,
        )
        with RunningServer(server):
            self._client(clock=lambda: 100.0).request("health")

        self.assertEqual(observed, [(101_000, 501.0)])

    def test_connections_are_processed_serially(self) -> None:
        active = 0
        maximum_active = 0
        lock = threading.Lock()
        peer_credentials = iter(
            (
                PeerCredentials(pid=501, uid=os.getuid() + 501, gid=os.getgid()),
                PeerCredentials(pid=502, uid=os.getuid() + 502, gid=os.getgid()),
            )
        )

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"value": request.payload["value"]}

        connections = [
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
        ]
        for connection in connections:
            connection.settimeout(1.0)
        results: list[int] = []
        try:
            with (
                patch(
                    "trading_ai.execution.paper_executor_ipc._peer_credentials",
                    side_effect=lambda _connection: next(peer_credentials),
                ),
                RunningServer(self._server(handler, peer_admitter=lambda _peer: True)),
            ):
                for value, connection in zip((1, 2), connections, strict=True):
                    connection.connect(str(self.socket_path))
                    send_json_frame(
                        connection,
                        executor_request(
                            "health",
                            request_id=uuid.UUID(int=90 + value).hex,
                            payload={"value": value},
                        ),
                    )
                for connection in connections:
                    size = struct.unpack("!I", receive_exact(connection, 4))[0]
                    response = json.loads(receive_exact(connection, size))
                    results.append(int(response["payload"]["value"]))
        finally:
            for connection in connections:
                connection.close()

        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual(maximum_active, 1)

    def test_mutation_timeouts_are_unknown_with_durable_identity_and_no_retry(self) -> None:
        for index, operation in enumerate(
            (
                "submit_order",
                "cancel_order",
                "latch_kill_switch",
                "start_safe_flatten",
            ),
            start=1,
        ):
            with self.subTest(operation=operation):
                calls = 0
                handler_entered = threading.Event()
                handler_finished = threading.Event()

                def handler(
                    _request: PaperExecutorRequest,
                    entered: threading.Event = handler_entered,
                    finished: threading.Event = handler_finished,
                ) -> dict[str, object]:
                    nonlocal calls
                    calls += 1
                    entered.set()
                    time.sleep(1.0)
                    finished.set()
                    return {"accepted": True}

                request_id = uuid.UUID(int=100 + index).hex
                with RunningServer(self._server(handler)):
                    client = PaperExecutorClient(
                        socket_path=self.socket_path,
                        socket_trust=self.socket_trust,
                        timeout_seconds=0.75,
                    )
                    with self.assertRaises(PaperExecutorOutcomeUnknownError) as raised:
                        client.request(
                            operation,
                            request_id=request_id,
                            target=self._target(),
                        )
                    self.assertTrue(handler_entered.wait(timeout=0.25))
                    self.assertTrue(handler_finished.wait(timeout=1.0))

                self.assertEqual(calls, 1)
                self.assertEqual(raised.exception.request_id, request_id)
                self.assertEqual(raised.exception.operation, operation)
                self.assertEqual(raised.exception.phase, "receive")
                self.assertEqual(raised.exception.target, self._target())

    def test_mutation_target_matches_before_handler_and_is_delivered(self) -> None:
        observed: list[ExecutorTarget | None] = []

        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            observed.append(request.target)
            return {"accepted": True}

        with RunningServer(self._server(handler)):
            result = self._client().request(
                "submit_order",
                request_id=uuid.UUID(int=200).hex,
                target=self._target(),
            )

        self.assertEqual(result, {"accepted": True})
        self.assertEqual(observed, [self._target()])

    def test_mutation_target_mismatches_never_reach_handler(self) -> None:
        mismatches = {
            "account": self._target(account_scope_sha256="c" * 64),
            "policy": self._target(policy_sha256="d" * 64),
            "authz_policy": self._target(authz_policy_sha256="e" * 63 + "f"),
            "run": self._target(run_id=uuid.UUID(int=701).hex),
            "fence": self._target(fence_epoch=FENCE_EPOCH + 1),
        }
        for index, (label, target) in enumerate(mismatches.items(), start=1):
            with self.subTest(field=label):
                calls = 0

                def handler(_request: PaperExecutorRequest) -> dict[str, object]:
                    nonlocal calls
                    calls += 1
                    return {"accepted": True}

                request_id = uuid.UUID(int=300 + index).hex
                with (
                    RunningServer(self._server(handler)),
                    self.assertRaises(PaperExecutorOutcomeUnknownError) as raised,
                ):
                    self._client().request(
                        "submit_order",
                        request_id=request_id,
                        target=target,
                    )

                self.assertEqual(calls, 0)
                self.assertEqual(raised.exception.request_id, request_id)
                self.assertEqual(raised.exception.operation, "submit_order")
                self.assertEqual(raised.exception.phase, "validate")

    def test_mutation_without_target_is_rejected_before_connect(self) -> None:
        with self.assertRaisesRegex(PaperExecutorProtocolError, "requires an exact"):
            self._client().request(
                "submit_order",
                request_id=uuid.UUID(int=400).hex,
            )

    def test_server_rejects_raw_mutation_without_target_before_handler(self) -> None:
        calls = 0

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"accepted": True}

        request = {
            "schema_version": IPC_SCHEMA_VERSION,
            "request_id": uuid.UUID(int=401).hex,
            "operation": "submit_order",
            "deadline_unix_ms": int((time.time() + 1.0) * 1_000),
            "payload": {},
            "target": None,
        }
        with RunningServer(self._server(handler)):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(1.0)
            try:
                raw.connect(str(self.socket_path))
                send_json_frame(raw, request)
                size = struct.unpack("!I", receive_exact(raw, 4))[0]
                response = json.loads(receive_exact(raw, size))
            finally:
                raw.close()

        self.assertEqual(calls, 0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "target_required")

    def test_invalid_post_dispatch_responses_are_outcome_unknown(self) -> None:
        cases = {
            "truncated": "receive",
            "invalid_json": "receive",
            "wrong_request_id": "validate",
        }
        for index, (case, expected_phase) in enumerate(cases.items(), start=1):
            with self.subTest(case=case):
                observed_operations: list[str] = []

                def responder(
                    connection: socket.socket,
                    request: dict[str, Any],
                    response_case: str = case,
                    observed: list[str] = observed_operations,
                ) -> None:
                    observed.append(str(request["operation"]))
                    if response_case == "truncated":
                        partial = b'{"schema_version":'
                        connection.sendall(struct.pack("!I", len(partial) + 10) + partial)
                        return
                    if response_case == "invalid_json":
                        connection.sendall(struct.pack("!I", 2) + b"{]")
                        return
                    response = {
                        "schema_version": IPC_SCHEMA_VERSION,
                        "request_id": uuid.UUID(int=999).hex,
                        "ok": True,
                        "payload": {"accepted": True},
                        "error": None,
                        "server": self._metadata(),
                    }
                    send_json_frame(connection, response)

                request_id = uuid.UUID(int=500 + index).hex
                with (
                    RawResponseServer(self.socket_path, responder),
                    self.assertRaises(PaperExecutorOutcomeUnknownError) as raised,
                ):
                    self._client().request(
                        "submit_order",
                        request_id=request_id,
                        target=self._target(),
                    )

                self.assertEqual(observed_operations, ["submit_order"])
                self.assertEqual(raised.exception.request_id, request_id)
                self.assertEqual(raised.exception.operation, "submit_order")
                self.assertEqual(raised.exception.phase, expected_phase)

    def test_remote_unknown_mutation_codes_are_outcome_unknown(self) -> None:
        def handler(request: PaperExecutorRequest) -> dict[str, object]:
            code = str(request.payload["code"])
            if code == "executor_internal_error":
                raise RuntimeError("sensitive broker failure")
            raise PaperExecutorRequestError(
                "command_outcome_unknown",
                "executor command requires broker-first reconciliation",
            )

        with RunningServer(self._server(handler)):
            for index, code in enumerate(
                ("executor_internal_error", "command_outcome_unknown"),
                start=1,
            ):
                with self.subTest(code=code):
                    request_id = uuid.UUID(int=600 + index).hex
                    with self.assertRaises(PaperExecutorOutcomeUnknownError) as raised:
                        self._client().request(
                            "submit_order",
                            {"code": code},
                            request_id=request_id,
                            target=self._target(),
                        )
                    self.assertEqual(raised.exception.request_id, request_id)
                    self.assertEqual(raised.exception.operation, "submit_order")
                    self.assertEqual(raised.exception.phase, "remote_result")

    def test_nonmutating_timeout_is_unavailable(self) -> None:
        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            time.sleep(0.15)
            return {}

        with RunningServer(self._server(handler)):
            client = PaperExecutorClient(
                socket_path=self.socket_path,
                socket_trust=self.socket_trust,
                timeout_seconds=0.05,
            )
            with self.assertRaises(PaperExecutorUnavailableError):
                client.request("health")
            time.sleep(0.2)

    def test_oversized_request_is_rejected_before_connect(self) -> None:
        with self.assertRaises(PaperExecutorProtocolError):
            self._client().request("health", {"value": "x" * MAX_REQUEST_BYTES})

    def test_duplicate_json_fields_are_rejected_without_calling_handler(self) -> None:
        calls = 0

        def handler(_request: PaperExecutorRequest) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        with RunningServer(self._server(handler)):
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.settimeout(1)
            raw.connect(str(self.socket_path))
            body = (
                b'{"schema_version":4,"schema_version":4,"request_id":"'
                + b"1" * 32
                + b'","operation":"health","deadline_unix_ms":9999999999999,'
                + b'"payload":{},"target":null}'
            )
            raw.sendall(struct.pack("!I", len(body)) + body)
            size = struct.unpack("!I", receive_exact(raw, 4))[0]
            response = json.loads(receive_exact(raw, size))
            raw.close()

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "protocol_rejected")
        self.assertEqual(calls, 0)

    def test_existing_socket_always_fails_closed(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.close()
        os.chmod(self.socket_path, 0o660)

        with self.assertRaises(PaperExecutorAlreadyRunningError):
            self._server(lambda _request: {}).start()

    def test_listening_socket_is_never_removed(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o660)
        listener.listen(1)
        try:
            with self.assertRaisesRegex(
                PaperExecutorAlreadyRunningError,
                "already exists",
            ):
                self._server(lambda _request: {}).start()

            self.assertTrue(self.socket_path.exists())
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.socket_path))
            finally:
                probe.close()
        finally:
            listener.close()
            if self.socket_path.exists():
                self.socket_path.unlink()

    def test_runtime_directory_must_be_private(self) -> None:
        self.runtime.chmod(0o755)
        with self.assertRaisesRegex(PaperExecutorProtocolError, "permissions"):
            self._server(lambda _request: {}).start()


def executor_request(
    operation: str,
    *,
    request_id: str,
    target: ExecutorTarget | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_target = None
    if target is not None:
        raw_target = {
            "account_scope_sha256": target.account_scope_sha256,
            "policy_sha256": target.policy_sha256,
            "authz_policy_sha256": target.authz_policy_sha256,
            "run_id": target.run_id,
            "fence_epoch": target.fence_epoch,
        }
    return {
        "schema_version": IPC_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": operation,
        "deadline_unix_ms": int((time.time() + 2.0) * 1_000),
        "payload": dict(payload or {}),
        "target": raw_target,
    }


def wait_until(predicate: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def receive_exact(connection: socket.socket, size: int) -> bytes:
    value = b""
    while len(value) < size:
        chunk = connection.recv(size - len(value))
        if not chunk:
            raise AssertionError("socket response ended early")
        value += chunk
    return value


def send_json_frame(connection: socket.socket, value: Mapping[str, Any]) -> None:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    connection.sendall(struct.pack("!I", len(body)) + body)


def stat_is_socket(mode: int) -> bool:
    return stat.S_ISSOCK(mode)


if __name__ == "__main__":
    unittest.main()
