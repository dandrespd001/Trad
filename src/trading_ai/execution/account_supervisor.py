"""Single-host paper-account mutation lease with a durable fencing epoch.

The lease is deliberately narrower than a distributed execution service.  It
prevents two processes owned by the same operating-system user on one host
from mutating the same paper account at the same time.  Live accounts are not
accepted.  A future live executor needs an external fencing authority that the
broker-facing side effect can verify independently.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pwd
import stat
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEASE_SCHEMA_VERSION = 1
MAX_LEASE_BYTES = 16 * 1024
_ALLOWED_ENVIRONMENTS = frozenset({"paper"})
_ALLOWED_BROKERS = frozenset({"alpaca"})


class AccountSupervisorError(RuntimeError):
    """Base error for account mutation supervision."""


class InvalidAccountScopeError(AccountSupervisorError):
    """Raised when a broker/environment/account scope is not trustworthy."""


class AccountLeaseBusyError(AccountSupervisorError):
    """Raised when another writer already owns the account lease."""


class AccountLeaseIntegrityError(AccountSupervisorError):
    """Raised when lease storage or sealed metadata cannot be trusted."""


class AccountLeaseNotActiveError(AccountSupervisorError):
    """Raised when a released, forked, or otherwise inactive lease is used."""


@dataclass(frozen=True)
class AccountMutationFence:
    """Evidence identifying one active account-writer generation."""

    scope_sha256: str
    epoch: int
    owner_id: str
    pid: int
    acquired_at: str


def default_account_supervisor_root() -> Path:
    """Return a user-stable root without trusting ``HOME`` or XDG overrides."""

    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return user_home / ".local" / "state" / "trading-ai" / "account-supervisor"


def account_scope_sha256(*, broker: str, environment: str, account_id: str) -> str:
    """Hash the canonical account scope used for lock and journal identity."""

    clean_broker = _validated_component(broker, label="broker").lower()
    clean_environment = _validated_component(environment, label="environment").lower()
    clean_account_id = _validated_component(account_id, label="account_id")
    if clean_broker not in _ALLOWED_BROKERS:
        raise InvalidAccountScopeError(f"unsupported broker scope: {clean_broker}")
    if clean_environment not in _ALLOWED_ENVIRONMENTS:
        raise InvalidAccountScopeError("only paper account mutation leases are supported")
    canonical = json.dumps(
        {
            "account_id": clean_account_id,
            "broker": clean_broker,
            "environment": clean_environment,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_account_journal_path(
    *,
    broker: str,
    environment: str,
    account_id: str,
    root: str | Path | None = None,
) -> Path:
    """Prepare the canonical private root and return its account journal path."""

    scope_sha256 = account_scope_sha256(
        broker=broker,
        environment=environment,
        account_id=account_id,
    )
    resolved_root = Path(root) if root is not None else default_account_supervisor_root()
    _ensure_private_directory(resolved_root)
    return resolved_root / f"{scope_sha256}.orders.sqlite3"


def canonical_account_executor_journal_path(
    *,
    broker: str,
    environment: str,
    account_id: str,
    root: str | Path | None = None,
) -> Path:
    """Return the command/run ledger paired with the canonical order journal."""

    scope_sha256 = account_scope_sha256(
        broker=broker,
        environment=environment,
        account_id=account_id,
    )
    resolved_root = Path(root) if root is not None else default_account_supervisor_root()
    _ensure_private_directory(resolved_root)
    return resolved_root / f"{scope_sha256}.executor.sqlite3"


class AccountMutationLease:
    """Exclusive process lease for one paper broker account.

    The underlying ``flock`` has no TTL.  An observer must never delete the
    file because it looks old: the kernel lock, not mtime, is the authority.
    Each successful acquisition increments and fsyncs a durable epoch.  The
    epoch is useful audit evidence locally; it is not broker-enforced fencing.
    """

    def __init__(
        self,
        *,
        root: Path,
        lock_path: Path,
        journal_path: Path,
        fd: int,
        scope_sha256: str,
        epoch: int,
        owner_id: str,
        pid: int,
        acquired_at: str,
        clock: Callable[[], datetime],
    ) -> None:
        self.root = root
        self.lock_path = lock_path
        self.journal_path = journal_path
        self._fd = fd
        self._scope_sha256 = scope_sha256
        self._epoch = epoch
        self._owner_id = owner_id
        self._pid = pid
        self._acquired_at = acquired_at
        self._clock = clock
        self._active = True
        self._operation_lock = threading.Lock()

    @classmethod
    def acquire(
        cls,
        *,
        broker: str,
        environment: str,
        account_id: str,
        root: str | Path | None = None,
        owner_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> AccountMutationLease:
        """Acquire the unique local writer lease or fail without waiting."""

        scope_sha256 = account_scope_sha256(
            broker=broker,
            environment=environment,
            account_id=account_id,
        )
        resolved_root = Path(root) if root is not None else default_account_supervisor_root()
        _ensure_private_directory(resolved_root)
        lock_path = resolved_root / f"{scope_sha256}.lease"
        journal_path = resolved_root / f"{scope_sha256}.orders.sqlite3"
        fd, created = _open_private_lock_file(lock_path)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise AccountLeaseBusyError(
                        "another process owns the paper account mutation lease"
                    ) from exc
                raise AccountLeaseIntegrityError("cannot acquire account lease") from exc

            previous = _read_lease_payload(fd, allow_empty=created)
            previous_epoch = _validated_previous_payload(previous, scope_sha256=scope_sha256)
            resolved_owner = owner_id or uuid.uuid4().hex
            clean_owner = _validated_component(resolved_owner, label="owner_id")
            resolved_clock = clock or (lambda: datetime.now(UTC))
            acquired_at = _timestamp(resolved_clock())
            pid = os.getpid()
            epoch = previous_epoch + 1
            payload = {
                "schema_version": LEASE_SCHEMA_VERSION,
                "scope_sha256": scope_sha256,
                "epoch": epoch,
                "state": "ACTIVE",
                "owner_id": clean_owner,
                "pid": pid,
                "acquired_at": acquired_at,
                "released_at": None,
                "unclean_predecessor": bool(previous and previous.get("state") == "ACTIVE"),
            }
            _write_lease_payload(fd, payload)
            return cls(
                root=resolved_root,
                lock_path=lock_path,
                journal_path=journal_path,
                fd=fd,
                scope_sha256=scope_sha256,
                epoch=epoch,
                owner_id=clean_owner,
                pid=pid,
                acquired_at=acquired_at,
                clock=resolved_clock,
            )
        except Exception:
            os.close(fd)
            raise

    @property
    def fence(self) -> AccountMutationFence:
        return AccountMutationFence(
            scope_sha256=self._scope_sha256,
            epoch=self._epoch,
            owner_id=self._owner_id,
            pid=self._pid,
            acquired_at=self._acquired_at,
        )

    @property
    def active(self) -> bool:
        return self._active and self._pid == os.getpid()

    def validate(self, *, broker: str, environment: str, account_id: str) -> AccountMutationFence:
        """Revalidate process, scope, inode, permissions, and sealed payload."""

        if not self._active or self._fd < 0:
            raise AccountLeaseNotActiveError("account mutation lease is not active")
        if self._pid != os.getpid():
            raise AccountLeaseNotActiveError("account mutation lease cannot cross a fork")
        expected_scope = account_scope_sha256(
            broker=broker,
            environment=environment,
            account_id=account_id,
        )
        if expected_scope != self._scope_sha256:
            raise InvalidAccountScopeError("account mutation lease scope mismatch")
        _validate_open_lock_file(self._fd, self.lock_path)
        payload = _read_lease_payload(self._fd)
        expected = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "scope_sha256": self._scope_sha256,
            "epoch": self._epoch,
            "state": "ACTIVE",
            "owner_id": self._owner_id,
            "pid": self._pid,
            "acquired_at": self._acquired_at,
        }
        if payload is None or any(payload.get(key) != value for key, value in expected.items()):
            raise AccountLeaseIntegrityError("account lease metadata no longer matches its owner")
        return self.fence

    @contextmanager
    def mutation(
        self,
        *,
        broker: str,
        environment: str,
        account_id: str,
        operation: str,
        mutation_id: str,
    ) -> Iterator[AccountMutationFence]:
        """Serialize one mutation inside the already-exclusive process lease."""

        _validated_component(operation, label="operation")
        _validated_component(mutation_id, label="mutation_id")
        if not self._operation_lock.acquire(blocking=False):
            raise AccountLeaseBusyError("another account mutation is active in this writer")
        try:
            yield self.validate(
                broker=broker,
                environment=environment,
                account_id=account_id,
            )
        finally:
            self._operation_lock.release()

    def release(self) -> None:
        """Seal a release record and return the kernel lock."""

        if not self._active:
            return
        if not self._operation_lock.acquire(blocking=False):
            raise AccountLeaseBusyError("cannot release while an account mutation is active")
        error: Exception | None = None
        try:
            if self._pid != os.getpid():
                error = AccountLeaseNotActiveError(
                    "account mutation lease cannot be released from a fork"
                )
            else:
                _validate_open_lock_file(self._fd, self.lock_path)
                payload = _read_lease_payload(self._fd)
                if payload is None or any(
                    payload.get(key) != value
                    for key, value in {
                        "schema_version": LEASE_SCHEMA_VERSION,
                        "scope_sha256": self._scope_sha256,
                        "epoch": self._epoch,
                        "state": "ACTIVE",
                        "owner_id": self._owner_id,
                        "pid": self._pid,
                        "acquired_at": self._acquired_at,
                    }.items()
                ):
                    error = error or AccountLeaseIntegrityError(
                        "account lease metadata cannot be trusted during release"
                    )
                else:
                    payload["state"] = "RELEASED"
                    payload["released_at"] = _timestamp(self._clock())
                    _write_lease_payload(self._fd, payload)
        except Exception as exc:  # preserve integrity failure after unlocking
            error = error or exc
        finally:
            try:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
            finally:
                self._fd = -1
                self._active = False
                self._operation_lock.release()
        if error is not None:
            raise error

    def __enter__(self) -> AccountMutationLease:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


def _validated_component(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise InvalidAccountScopeError(f"{label} must be a string")
    clean = value.strip()
    if not clean or len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise InvalidAccountScopeError(f"{label} is invalid")
    return clean


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise AccountLeaseIntegrityError("cannot prepare account supervisor directory") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise AccountLeaseIntegrityError("account supervisor root must be a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AccountLeaseIntegrityError("account supervisor directory permissions are unsafe")


def _open_private_lock_file(path: Path) -> tuple[int, bool]:
    flags = os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, flags)
    except OSError as exc:
        raise AccountLeaseIntegrityError("cannot open account lease file") from exc
    try:
        _validate_open_lock_file(fd, path)
        if created:
            _fsync_directory(path.parent)
    except Exception:
        os.close(fd)
        raise
    return fd, created


def _validate_open_lock_file(fd: int, path: Path) -> None:
    try:
        descriptor = os.fstat(fd)
        pathname = path.lstat()
    except OSError as exc:
        raise AccountLeaseIntegrityError("cannot stat account lease file") from exc
    if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_nlink != 1:
        raise AccountLeaseIntegrityError("account lease must be one regular file")
    if descriptor.st_uid != os.getuid() or stat.S_IMODE(descriptor.st_mode) & 0o077:
        raise AccountLeaseIntegrityError("account lease file permissions are unsafe")
    if (descriptor.st_dev, descriptor.st_ino) != (pathname.st_dev, pathname.st_ino):
        raise AccountLeaseIntegrityError("account lease pathname changed after open")


def _read_lease_payload(fd: int, *, allow_empty: bool = False) -> dict[str, Any] | None:
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            if allow_empty:
                return None
            raise AccountLeaseIntegrityError(
                "existing account lease metadata is empty and cannot be treated as genesis"
            )
        if size < 0 or size > MAX_LEASE_BYTES:
            raise AccountLeaseIntegrityError("account lease metadata size is invalid")
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, size)
        if len(raw) != size:
            raise AccountLeaseIntegrityError("account lease metadata read is incomplete")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except AccountSupervisorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountLeaseIntegrityError("account lease metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise AccountLeaseIntegrityError("account lease metadata must be an object")
    return payload


def _validated_previous_payload(payload: dict[str, Any] | None, *, scope_sha256: str) -> int:
    if payload is None:
        return 0
    if payload.get("schema_version") != LEASE_SCHEMA_VERSION:
        raise AccountLeaseIntegrityError("account lease schema is unsupported")
    if payload.get("scope_sha256") != scope_sha256:
        raise AccountLeaseIntegrityError("account lease scope hash changed")
    epoch = payload.get("epoch")
    if type(epoch) is not int or epoch < 1:
        raise AccountLeaseIntegrityError("account lease epoch is invalid")
    if payload.get("state") not in {"ACTIVE", "RELEASED"}:
        raise AccountLeaseIntegrityError("account lease state is invalid")
    try:
        _validated_component(payload.get("owner_id"), label="owner_id")
    except InvalidAccountScopeError as exc:
        raise AccountLeaseIntegrityError("account lease owner_id is invalid") from exc
    pid = payload.get("pid")
    if type(pid) is not int or pid < 1:
        raise AccountLeaseIntegrityError("account lease pid is invalid")
    acquired_at = payload.get("acquired_at")
    if type(acquired_at) is not str or not _is_aware_timestamp(acquired_at):
        raise AccountLeaseIntegrityError("account lease acquired_at is invalid")
    released_at = payload.get("released_at")
    if payload.get("state") == "RELEASED" and (
        type(released_at) is not str or not _is_aware_timestamp(released_at)
    ):
        raise AccountLeaseIntegrityError("released account lease lacks a valid timestamp")
    return epoch


def _write_lease_payload(fd: int, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_LEASE_BYTES:
        raise AccountLeaseIntegrityError("account lease metadata exceeds its size limit")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise AccountLeaseIntegrityError("account lease metadata write is incomplete")
        os.fsync(fd)
    except AccountSupervisorError:
        raise
    except OSError as exc:
        raise AccountLeaseIntegrityError("cannot persist account lease metadata") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AccountLeaseIntegrityError("cannot persist account lease directory entry") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AccountLeaseIntegrityError(f"duplicate account lease key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise AccountLeaseIntegrityError(f"non-finite account lease constant: {value}")


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AccountLeaseIntegrityError("account supervisor clock must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _is_aware_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
