"""Real-kernel UID/GID probe for the paper-executor Unix socket boundary.

This helper runs only inside a disposable user and network namespace created by
``test_paper_executor_multi_uid``.  It never loads broker credentials, connects
to a network, or invokes the financial executor application.  Its sole purpose
is to exercise AF_UNIX DAC, SO_PEERCRED, and the static UID capability policy
with distinct kernel identities and no credential mocks.
"""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType
from typing import Any

DAEMON_UID = 1
MONITOR_UID = 2
SAFETY_UID = 3
UNKNOWN_GROUP_MEMBER_UID = 4
OUTSIDER_UID = 5
IPC_GID = 10
CHILD_TIMEOUT_SECONDS = 5.0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("usage: paper_executor_multi_uid_probe.py REPOSITORY_ROOT")
    repository_root = Path(arguments[0]).resolve()
    source_root = repository_root / "src"
    if not source_root.is_dir():
        raise RuntimeError("repository source root is unavailable")
    sys.path.insert(0, str(source_root))

    # Imports happen while inner UID 0 still maps to the invoking host user.
    # The forked roles drop to subordinate kernel UIDs before opening sockets.
    from trading_ai.execution.paper_executor_authz import (  # noqa: PLC0415
        CAPABILITY_HEALTH,
        CAPABILITY_KILL,
        ExecutorAuthorizationDenied,
        ExecutorAuthorizationPolicy,
        ExecutorPrincipal,
    )
    from trading_ai.execution.paper_executor_ipc import (  # noqa: PLC0415
        ExecutorSocketTrust,
        ExecutorTarget,
        PaperExecutorClient,
        PaperExecutorRemoteError,
        PaperExecutorRequestError,
        PaperExecutorServer,
    )

    principals = (
        ExecutorPrincipal(
            name="trading-ai-monitor",
            uid=MONITOR_UID,
            capabilities=frozenset({CAPABILITY_HEALTH}),
        ),
        ExecutorPrincipal(
            name="trading-ai-safety",
            uid=SAFETY_UID,
            capabilities=frozenset({CAPABILITY_HEALTH, CAPABILITY_KILL}),
        ),
    )
    policy = ExecutorAuthorizationPolicy(
        socket_group="trading-ai-paper-ipc",
        socket_gid=IPC_GID,
        policy_sha256="e" * 64,
        principals=principals,
        _by_uid=MappingProxyType({principal.uid: principal for principal in principals}),
    )
    run_id = uuid.UUID(int=9).hex
    metadata = {
        "account_scope_sha256": "a" * 64,
        "policy_sha256": "b" * 64,
        "authz_policy_sha256": policy.policy_sha256,
        "run_id": run_id,
        "fence_epoch": 1,
        "status": "ready",
    }
    target = ExecutorTarget(
        account_scope_sha256="a" * 64,
        policy_sha256="b" * 64,
        authz_policy_sha256=policy.policy_sha256,
        run_id=run_id,
        fence_epoch=1,
    )

    runtime_directory = Path(tempfile.mkdtemp(prefix="paper-executor-multiuid-", dir="/tmp"))
    os.chown(runtime_directory, DAEMON_UID, IPC_GID)
    os.chmod(runtime_directory, 0o750)  # noqa: S103 - exact executor group traversal contract
    socket_path = runtime_directory / "executor.sock"
    ready_read, ready_write = os.pipe()
    audit_read, audit_write = os.pipe()
    server_pid = os.fork()
    if server_pid == 0:
        os.close(ready_read)
        os.close(audit_read)
        _run_server_child(
            socket_path=socket_path,
            policy=policy,
            metadata=metadata,
            ready_fd=ready_write,
            audit_fd=audit_write,
            capability_health=CAPABILITY_HEALTH,
            capability_kill=CAPABILITY_KILL,
            authorization_denied_type=ExecutorAuthorizationDenied,
            request_error_type=PaperExecutorRequestError,
            server_type=PaperExecutorServer,
        )

    os.close(ready_write)
    os.close(audit_write)
    server_status: int | None = None
    cleanup_complete = False
    try:
        ready = _read_json_fd(ready_read, timeout=CHILD_TIMEOUT_SECONDS)
        if ready.get("ok") is not True:
            raise RuntimeError(f"multi-UID server failed before readiness: {ready.get('error_type', 'unknown')}")
        runtime_stat = runtime_directory.lstat()
        socket_stat = socket_path.lstat()
        _require(stat.S_ISDIR(runtime_stat.st_mode), "runtime path is not a directory")
        _require(stat.S_ISSOCK(socket_stat.st_mode), "executor path is not a socket")
        _require(
            (runtime_stat.st_uid, runtime_stat.st_gid, stat.S_IMODE(runtime_stat.st_mode))
            == (DAEMON_UID, IPC_GID, 0o750),
            "runtime ownership or mode is incorrect",
        )
        _require(
            (socket_stat.st_uid, socket_stat.st_gid, stat.S_IMODE(socket_stat.st_mode)) == (DAEMON_UID, IPC_GID, 0o660),
            "socket ownership or mode is incorrect",
        )

        # This marker is the skip boundary consumed by the wrapper. Any failure
        # after it is a real security regression, never an unavailable-prereq skip.
        print("READY", flush=True)

        client_parameters = {
            "socket_path": socket_path,
            "socket_trust": ExecutorSocketTrust(server_uid=DAEMON_UID, socket_gid=IPC_GID),
            "timeout_seconds": 2.0,
        }
        results = {
            "monitor_health": _invoke_client(
                uid=MONITOR_UID,
                groups=(IPC_GID,),
                operation="health",
                target=None,
                client_parameters=client_parameters,
                client_type=PaperExecutorClient,
                remote_error_type=PaperExecutorRemoteError,
            ),
            "monitor_kill": _invoke_client(
                uid=MONITOR_UID,
                groups=(IPC_GID,),
                operation="latch_kill_switch",
                target=target,
                client_parameters=client_parameters,
                client_type=PaperExecutorClient,
                remote_error_type=PaperExecutorRemoteError,
            ),
            "safety_kill": _invoke_client(
                uid=SAFETY_UID,
                groups=(IPC_GID,),
                operation="latch_kill_switch",
                target=target,
                client_parameters=client_parameters,
                client_type=PaperExecutorClient,
                remote_error_type=PaperExecutorRemoteError,
            ),
            "unknown_group_member": _invoke_client(
                uid=UNKNOWN_GROUP_MEMBER_UID,
                groups=(IPC_GID,),
                operation="health",
                target=None,
                client_parameters=client_parameters,
                client_type=PaperExecutorClient,
                remote_error_type=PaperExecutorRemoteError,
            ),
            "outsider": _invoke_outsider(socket_path),
        }

        _require(
            results["monitor_health"] == {"kind": "ok", "value": {"operation": "health", "peer_uid": MONITOR_UID}},
            "monitor health did not preserve its kernel UID",
        )
        _require(
            results["monitor_kill"] == {"code": "authorization_denied", "kind": "PaperExecutorRemoteError"},
            "monitor mutation was not denied by capability",
        )
        _require(
            results["safety_kill"]
            == {
                "kind": "ok",
                "value": {"operation": "latch_kill_switch", "peer_uid": SAFETY_UID},
            },
            "safety capability did not preserve its kernel UID",
        )
        _require(
            results["unknown_group_member"].get("kind") != "ok",
            "an unknown UID was admitted merely because it belonged to the IPC group",
        )
        _require(
            results["outsider"] == {"errno": errno.EACCES, "kind": "OSError"},
            "a process outside the IPC group crossed directory DAC",
        )
    finally:
        os.close(ready_read)
        server_status = _terminate_and_wait(server_pid)
        audit_payload = _read_all(audit_read)
        os.close(audit_read)
        with suppress(FileNotFoundError):
            socket_path.unlink()
        with suppress(OSError):
            runtime_directory.rmdir()
        cleanup_complete = not socket_path.exists() and not runtime_directory.exists()

    _require(server_status == 0, "multi-UID server did not stop cleanly")
    audit = tuple(line for line in audit_payload.decode("utf-8").splitlines() if line)
    _require(
        audit
        == (
            f"{MONITOR_UID}:health",
            f"{MONITOR_UID}:latch_kill_switch",
            f"{SAFETY_UID}:latch_kill_switch",
        ),
        "unknown or outside UID reached the executor handler",
    )
    _require(cleanup_complete, "multi-UID probe did not clean its runtime socket")
    output = {
        "schema_version": 1,
        "runtime": {"uid": DAEMON_UID, "gid": IPC_GID, "mode": "0750"},
        "socket": {"uid": DAEMON_UID, "gid": IPC_GID, "mode": "0660"},
        "results": results,
        "handler_audit": list(audit),
        "server_status": server_status,
        "cleanup_complete": cleanup_complete,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


def _run_server_child(
    *,
    socket_path: Path,
    policy: Any,
    metadata: dict[str, object],
    ready_fd: int,
    audit_fd: int,
    capability_health: str,
    capability_kill: str,
    authorization_denied_type: type[Exception],
    request_error_type: type[Exception],
    server_type: type[Any],
) -> None:
    exit_code = 0
    ready_sent = False
    server: Any | None = None
    try:
        _drop_identity(DAEMON_UID, IPC_GID, (IPC_GID,))
        stop_event = threading.Event()
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())

        def handler(request: Any) -> dict[str, object]:
            os.write(audit_fd, f"{request.peer.uid}:{request.operation}\n".encode("ascii"))
            capability = capability_health if request.operation == "health" else capability_kill
            try:
                policy.require(request.peer, capability)
            except authorization_denied_type as exc:
                raise request_error_type("authorization_denied", "executor peer is not authorized") from exc
            return {"peer_uid": request.peer.uid, "operation": request.operation}

        server = server_type(
            handler=handler,
            peer_admitter=policy.admit,
            socket_gid=IPC_GID,
            socket_path=socket_path,
            metadata=metadata,
        )
        server.start()
        socket_stat = socket_path.lstat()
        _write_json_fd(
            ready_fd,
            {
                "ok": True,
                "uid": os.getuid(),
                "gid": os.getgid(),
                "socket_uid": socket_stat.st_uid,
                "socket_gid": socket_stat.st_gid,
                "socket_mode": stat.S_IMODE(socket_stat.st_mode),
            },
        )
        ready_sent = True
        os.close(ready_fd)
        server.serve_forever(stop_event)
    except BaseException as exc:  # isolated child reports only the exception type
        exit_code = 70
        if not ready_sent:
            with suppress(OSError):
                _write_json_fd(ready_fd, {"ok": False, "error_type": type(exc).__name__})
                os.close(ready_fd)
    finally:
        if server is not None:
            with suppress(Exception):
                server.close()
        with suppress(OSError):
            os.close(audit_fd)
    os._exit(exit_code)


def _invoke_client(
    *,
    uid: int,
    groups: tuple[int, ...],
    operation: str,
    target: Any | None,
    client_parameters: dict[str, object],
    client_type: type[Any],
    remote_error_type: type[Exception],
) -> dict[str, Any]:
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            _drop_identity(uid, uid, groups)
            client = client_type(**client_parameters)
            request_options = {} if target is None else {"target": target}
            result = {
                "kind": "ok",
                "value": client.request(operation, {}, **request_options),
            }
        except remote_error_type as exc:
            result = {"kind": type(exc).__name__, "code": getattr(exc, "code", None)}
        except BaseException as exc:
            result = {"kind": type(exc).__name__, "code": getattr(exc, "code", None)}
        with suppress(OSError):
            _write_json_fd(write_fd, result)
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        result = _read_json_fd(read_fd, timeout=CHILD_TIMEOUT_SECONDS)
    except BaseException:
        _terminate_and_wait(child_pid)
        raise
    finally:
        os.close(read_fd)
    status = _wait_with_deadline(child_pid)
    _require(status == 0, "client child failed")
    return result


def _invoke_outsider(socket_path: Path) -> dict[str, Any]:
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            _drop_identity(OUTSIDER_UID, OUTSIDER_UID, ())
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(str(socket_path))
            finally:
                connection.close()
            result: dict[str, Any] = {"kind": "unexpected_connected", "errno": None}
        except OSError as exc:
            result = {"kind": "OSError", "errno": exc.errno}
        except BaseException as exc:
            result = {"kind": type(exc).__name__, "errno": None}
        with suppress(OSError):
            _write_json_fd(write_fd, result)
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        result = _read_json_fd(read_fd, timeout=CHILD_TIMEOUT_SECONDS)
    except BaseException:
        _terminate_and_wait(child_pid)
        raise
    finally:
        os.close(read_fd)
    status = _wait_with_deadline(child_pid)
    _require(status == 0, "outsider child failed")
    return result


def _drop_identity(uid: int, gid: int, supplementary_groups: tuple[int, ...]) -> None:
    os.setgroups(list(supplementary_groups))
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid or os.getegid() != gid:
        raise RuntimeError("child identity transition failed")


def _write_json_fd(descriptor: int, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > 4096:
        raise RuntimeError("probe result exceeded its atomic pipe frame")
    os.write(descriptor, payload)


def _read_json_fd(descriptor: int, *, timeout: float) -> dict[str, Any]:
    ready, _write_ready, _errors = select.select((descriptor,), (), (), timeout)
    if not ready:
        raise TimeoutError("multi-UID child did not respond before its deadline")
    payload = os.read(descriptor, 4096)
    if not payload.endswith(b"\n"):
        raise RuntimeError("multi-UID child returned an incomplete result")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("multi-UID child result is not an object")
    return parsed


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _terminate_and_wait(process_id: int) -> int:
    with suppress(ProcessLookupError):
        os.kill(process_id, signal.SIGTERM)
    return _wait_with_deadline(process_id)


def _wait_with_deadline(process_id: int) -> int:
    deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(process_id, os.WNOHANG)
        if waited_pid == process_id:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.kill(process_id, signal.SIGKILL)
    _waited_pid, status = os.waitpid(process_id, 0)
    return os.waitstatus_to_exitcode(status)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
