"""Credential-owning daemon for the single Alpaca paper-account executor.

The daemon is deliberately paper-only and starts in reduce-only mode until a
durable server-side risk provider is supplied.  It reads credentials from
systemd's ``CREDENTIALS_DIRECTORY`` only; it never sources Fish, zsh, ``.env``
files, or caller-selected shell code.  MiniMax and all other LLM providers are
outside this process.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import logging
import os
import pwd
import signal
import socket
import stat
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from trading_ai.config import load_risk_config_bytes, load_universe_config_bytes
from trading_ai.execution.account_supervisor import account_scope_sha256
from trading_ai.execution.alpaca_connection import (
    ALPACA_PAPER_API_KEY_ENV,
    ALPACA_PAPER_SECRET_KEY_ENV,
    build_alpaca_crypto_market_data_client,
    build_alpaca_market_data_client,
)
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker
from trading_ai.execution.paper_account_executor import (
    SupervisedAlpacaPaperClient,
    build_exclusive_alpaca_paper_client,
)
from trading_ai.execution.paper_executor_authz import (
    load_executor_authorization_policy_bytes,
)
from trading_ai.execution.paper_executor_ipc import (
    DEFAULT_EXECUTOR_IPC_GROUP,
    DEFAULT_EXECUTOR_RUNTIME_DIRECTORY,
    DEFAULT_EXECUTOR_SERVICE_USER,
    IPC_SCHEMA_VERSION,
    PaperExecutorServer,
)
from trading_ai.execution.paper_executor_journal import (
    DurableExecutorCommandJournal,
    ExecutorCommandJournalError,
)
from trading_ai.execution.paper_executor_service import PaperExecutorApplication

_LOG = logging.getLogger(__name__)
_API_KEY_CREDENTIAL = "alpaca-paper-api-key"
_SECRET_KEY_CREDENTIAL = "alpaca-paper-secret-key"  # noqa: S105
_MAX_CREDENTIAL_BYTES = 4 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_RECOVERY_INITIAL_SECONDS = 1.0
_RECOVERY_MAX_SECONDS = 60.0
DEFAULT_EXECUTOR_STATE_DIRECTORY = Path("/var/lib/trading-ai-paper")


class PaperExecutorDaemonError(RuntimeError):
    """Fail-closed daemon configuration or lifecycle error."""


@dataclass(frozen=True)
class DaemonCredentials:
    api_key: str
    secret_key: str


@dataclass(frozen=True)
class DaemonRuntime:
    application: PaperExecutorApplication
    server: PaperExecutorServer


def load_systemd_credentials(
    env: Mapping[str, str] | None = None,
) -> DaemonCredentials:
    values = os.environ if env is None else env
    raw_directory = values.get("CREDENTIALS_DIRECTORY", "")
    directory = Path(raw_directory)
    if not raw_directory or not directory.is_absolute():
        raise PaperExecutorDaemonError("systemd CREDENTIALS_DIRECTORY is required for the paper executor")
    _validate_real_directory(directory, label="credential directory")
    return DaemonCredentials(
        api_key=_read_private_value(directory / _API_KEY_CREDENTIAL),
        secret_key=_read_private_value(directory / _SECRET_KEY_CREDENTIAL),
    )


def build_daemon_runtime(
    *,
    risk_config: str | Path,
    universe_configs: Sequence[str | Path],
    authz_config: str | Path,
    socket_path: str | Path,
    supervisor_root: str | Path,
    env: Mapping[str, str] | None = None,
) -> DaemonRuntime:
    _validate_service_identity()
    if not universe_configs:
        raise PaperExecutorDaemonError("at least one universe config is required")
    config_paths = (Path(risk_config), *(Path(item) for item in universe_configs))
    policy_payloads = tuple(_read_immutable_config(path) for path in config_paths)
    authz_payload = _read_immutable_config(Path(authz_config))
    policy_sha256 = _policy_bundle_sha256(
        config_paths,
        payloads=policy_payloads,
    )
    risk_limits = load_risk_config_bytes(policy_payloads[0])
    universes = tuple(load_universe_config_bytes(payload) for payload in policy_payloads[1:])
    authorization_policy = load_executor_authorization_policy_bytes(
        authz_payload,
        daemon_uid=os.getuid(),
        allow_open=False,
    )
    symbols = tuple(dict.fromkeys(symbol for universe in universes for symbol in universe.symbols))
    credentials = load_systemd_credentials(env)
    credential_values = {
        ALPACA_PAPER_API_KEY_ENV: credentials.api_key,
        ALPACA_PAPER_SECRET_KEY_ENV: credentials.secret_key,
    }
    authority: SupervisedAlpacaPaperClient | None = None
    application: PaperExecutorApplication | None = None
    try:
        authority = build_exclusive_alpaca_paper_client(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            supervisor_root=supervisor_root,
        )
        broker = AlpacaPaperBroker(
            client=authority,
            allowlist=symbols,
            risk_limits=risk_limits,
            dry_run=False,
            market_data=build_alpaca_market_data_client(env=credential_values),
            crypto_market_data=build_alpaca_crypto_market_data_client(env=credential_values),
        )
        scope = account_scope_sha256(
            broker="alpaca",
            environment="paper",
            account_id=authority.account_id,
        )
        journal = DurableExecutorCommandJournal(
            authority.executor_journal_path,
            account_scope_sha256=scope,
        )
        application = PaperExecutorApplication(
            broker=broker,
            authority=authority,
            journal=journal,
            policy_sha256=policy_sha256,
            authorization_policy=authorization_policy,
            risk_context_provider=None,
        )
        application.start()
        server = PaperExecutorServer(
            handler=application.handle,
            peer_admitter=authorization_policy.admit,
            socket_gid=authorization_policy.socket_gid,
            socket_path=socket_path,
            metadata=application.metadata,
        )
        return DaemonRuntime(application=application, server=server)
    except BaseException:
        if application is not None:
            with suppress(Exception):
                application.close()
        elif authority is not None:
            authority.close()
        raise


def run_daemon(runtime: DaemonRuntime, *, stop_event: Event | None = None) -> int:
    stopped = stop_event or Event()
    server = runtime.server
    application = runtime.application
    backoff = _RECOVERY_INITIAL_SECONDS
    next_recovery = time.monotonic()
    try:
        server.start()
        _systemd_notify("READY=1\nSTATUS=paper executor serving")
        while not stopped.is_set():
            now = time.monotonic()
            if application.run_state is not None and application.run_state.value == "blocked" and now >= next_recovery:
                report = application.recover_once()
                if report.pending:
                    backoff = min(backoff * 2.0, _RECOVERY_MAX_SECONDS)
                    next_recovery = now + _jittered_delay(backoff)
                else:
                    backoff = _RECOVERY_INITIAL_SECONDS
                    next_recovery = now
                    _systemd_notify("STATUS=paper executor recovery complete")
            if application.run_state is not None and application.run_state.value == "ready":
                application.advance_safe_flatten_once()
            try:
                server.serve_once()
            except TimeoutError:
                continue
        return 0
    except ExecutorCommandJournalError:
        _LOG.critical("paper executor journal integrity failure")
        return 2
    finally:
        _systemd_notify("STOPPING=1\nSTATUS=paper executor stopping")
        try:
            server.close()
        finally:
            application.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single Alpaca paper executor daemon")
    parser.add_argument("--risk-config", required=True)
    parser.add_argument("--authz-config", required=True)
    parser.add_argument(
        "--universe-config",
        action="append",
        required=True,
        dest="universe_configs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        environment = os.environ
        socket_path = _runtime_socket_path(environment)
        supervisor_root = _supervisor_root(environment)
        stop_event = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        runtime = build_daemon_runtime(
            risk_config=args.risk_config,
            universe_configs=args.universe_configs,
            authz_config=args.authz_config,
            socket_path=socket_path,
            supervisor_root=supervisor_root,
            env=environment,
        )
        return run_daemon(runtime, stop_event=stop_event)
    except Exception as exc:  # noqa: BLE001 - redact every startup boundary failure
        _LOG.critical("paper executor startup rejected: %s", type(exc).__name__)
        return 2


def _runtime_socket_path(env: Mapping[str, str]) -> Path:
    raw = env.get("RUNTIME_DIRECTORY", "")
    if not raw:
        raise PaperExecutorDaemonError("one systemd RUNTIME_DIRECTORY is required")
    if ":" in raw:
        raise PaperExecutorDaemonError("multiple runtime directories are not supported")
    directory = Path(raw)
    if not directory.is_absolute():
        raise PaperExecutorDaemonError("systemd runtime directory must be absolute")
    if directory != DEFAULT_EXECUTOR_RUNTIME_DIRECTORY:
        raise PaperExecutorDaemonError("systemd runtime directory does not match the fixed executor path")
    return directory / "alpaca-paper-executor.sock"


def _supervisor_root(env: Mapping[str, str]) -> Path:
    raw = env.get("STATE_DIRECTORY", "")
    if not raw or ":" in raw:
        raise PaperExecutorDaemonError("one systemd STATE_DIRECTORY is required")
    directory = Path(raw)
    if not directory.is_absolute():
        raise PaperExecutorDaemonError("systemd state directory must be absolute")
    if directory != DEFAULT_EXECUTOR_STATE_DIRECTORY:
        raise PaperExecutorDaemonError("systemd state directory does not match the fixed executor path")
    return directory / "account-supervisor"


def _validate_service_identity() -> None:
    real_uid = os.getuid()
    effective_uid = os.geteuid()
    real_gid = os.getgid()
    effective_gid = os.getegid()
    if real_uid < 1 or real_uid != effective_uid:
        raise PaperExecutorDaemonError("paper executor requires one non-root real/effective uid")
    if real_gid < 1 or real_gid != effective_gid:
        raise PaperExecutorDaemonError("paper executor requires one non-root real/effective gid")
    try:
        user = pwd.getpwnam(DEFAULT_EXECUTOR_SERVICE_USER)
        group = grp.getgrnam(DEFAULT_EXECUTOR_IPC_GROUP)
    except (KeyError, TypeError, AttributeError) as exc:
        raise PaperExecutorDaemonError("paper executor service identities are not provisioned") from exc
    if user.pw_name != DEFAULT_EXECUTOR_SERVICE_USER or user.pw_uid != effective_uid:
        raise PaperExecutorDaemonError("paper executor uid does not match its fixed service account")
    if group.gr_name != DEFAULT_EXECUTOR_IPC_GROUP or group.gr_gid != effective_gid:
        raise PaperExecutorDaemonError("paper executor gid does not match its fixed IPC group")


def _policy_bundle_sha256(
    paths: Sequence[Path],
    *,
    payloads: Sequence[bytes] | None = None,
) -> str:
    sealed_payloads = tuple(_read_immutable_config(path) for path in paths) if payloads is None else tuple(payloads)
    if len(sealed_payloads) != len(paths):
        raise PaperExecutorDaemonError("executor policy bundle is incomplete")
    digest = hashlib.sha256()
    digest.update(f"paper-executor-policy-v1\0ipc={IPC_SCHEMA_VERSION}\0".encode())
    for index, (path, payload) in enumerate(zip(paths, sealed_payloads, strict=True)):
        if type(payload) is not bytes or not payload:
            raise PaperExecutorDaemonError("executor policy payload is invalid")
        digest.update(f"{index}:{path.name}:{len(payload)}\0".encode())
        digest.update(payload)
    return digest.hexdigest()


def _read_immutable_config(path: Path) -> bytes:
    path = Path(os.path.abspath(path))
    _validate_immutable_parent_chain(path.parent)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PaperExecutorDaemonError("executor policy file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        pathname = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (pathname.st_dev, pathname.st_ino)
            or _service_can_replace_or_write(path, metadata)
            or metadata.st_size < 1
            or metadata.st_size > _MAX_CONFIG_BYTES
        ):
            raise PaperExecutorDaemonError("executor policy file is not immutable and bounded")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        observed = os.fstat(descriptor)
    except OSError as exc:
        raise PaperExecutorDaemonError("executor policy file cannot be read") from exc
    finally:
        os.close(descriptor)
    if (
        len(payload) != metadata.st_size
        or observed.st_size != metadata.st_size
        or observed.st_mtime_ns != metadata.st_mtime_ns
        or observed.st_ctime_ns != metadata.st_ctime_ns
    ):
        raise PaperExecutorDaemonError("executor policy file changed while reading")
    return payload


def _validate_immutable_parent_chain(path: Path) -> None:
    current = path
    while True:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PaperExecutorDaemonError("executor policy directory is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.is_symlink()
            or _service_can_replace_or_write(current, metadata)
        ):
            raise PaperExecutorDaemonError(
                "executor policy directory is not root-owned and immutable"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _service_can_replace_or_write(path: Path, metadata: os.stat_result) -> bool:
    mode = stat.S_IMODE(metadata.st_mode)
    return bool(
        metadata.st_uid != 0
        or mode & 0o022
        or (metadata.st_uid == os.geteuid() and mode & 0o200)
        or os.access(path, os.W_OK, effective_ids=True)
    )


def _read_private_value(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PaperExecutorDaemonError("required systemd credential is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        pathname = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (pathname.st_dev, pathname.st_ino)
            or metadata.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or os.access(path, os.W_OK, effective_ids=True)
            or metadata.st_size < 1
            or metadata.st_size > _MAX_CREDENTIAL_BYTES
        ):
            raise PaperExecutorDaemonError("systemd credential permissions are unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        observed = os.fstat(descriptor)
    except OSError as exc:
        raise PaperExecutorDaemonError("systemd credential cannot be read") from exc
    finally:
        os.close(descriptor)
    if (
        len(raw) != metadata.st_size
        or observed.st_size != metadata.st_size
        or observed.st_mtime_ns != metadata.st_mtime_ns
        or observed.st_ctime_ns != metadata.st_ctime_ns
    ):
        raise PaperExecutorDaemonError("systemd credential changed while reading")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PaperExecutorDaemonError("systemd credential is not UTF-8") from exc
    if not value or len(value) > 512 or any(character.isspace() or ord(character) < 33 for character in value):
        raise PaperExecutorDaemonError("systemd credential value is invalid")
    return value


def _validate_real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PaperExecutorDaemonError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or os.access(path, os.W_OK, effective_ids=True)
    ):
        raise PaperExecutorDaemonError(f"{label} must be a private real directory")


def _jittered_delay(delay: float) -> float:
    # Stable per-process jitter avoids synchronized restarts without importing
    # any external entropy or changing financial decisions.
    offset = ((os.getpid() % 17) - 8) / 100.0
    return max(_RECOVERY_INITIAL_SECONDS, delay * (1.0 + offset))


def _systemd_notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):  # abstract Unix namespace used by systemd
        address = "\0" + address[1:]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
    try:
        client.connect(address)
        client.sendall(message.encode("utf-8"))
    except OSError:
        return
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
