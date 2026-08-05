#!/usr/bin/python3 -I
"""Verify a frozen MiniMax patch without touching the source checkout.

This is a Codex-side verifier, not a model tool.  It asks the adjacent audited
API worker to validate and export one sealed PATCH_READY artifact, rebuilds a
private source projection from Git objects plus the exact selected working-tree
snapshot, applies the patch only there, and runs direct argv commands inside a
network-denied Bubblewrap sandbox.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import resource
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "0.3.1"
BRIDGE_VERSION = "0.3.5"
EXPECTED_RUNNER_VERSION = "0.3.5"
EXPECTED_WORKER_CONTRACT = "codex-minimax-patch-v1"
EXPECTED_ENDPOINT = "https://api.minimax.io/v1"
EXPECTED_MODEL = "MiniMax-M3"
EXPECTED_WORKER_SHA256 = "5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d"
FIXED_TEMP_PARENT = Path("/tmp")  # nosec B108  # noqa: S108 - fixed private boundary
WORKER_NAME = "minimax_api_worker.py"
GIT = Path("/usr/bin/git")
PYTHON = Path("/usr/bin/python3")
BWRAP = Path("/usr/bin/bwrap")
LIBSECCOMP = Path("/usr/lib/libseccomp.so.2")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{20}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_WORKER_OUTPUT_BYTES = 4_000_000
MAX_WORKER_BYTES = 4_000_000
MAX_PROJECTION_FILES = 10_000
MAX_PROJECTION_BYTES = 512 * 1024 * 1024
MAX_PROJECTION_FILE_BYTES = 32 * 1024 * 1024
MAX_COMMAND_ARGUMENTS = 128
MAX_COMMANDS_PER_PHASE = 20
MAX_COMMAND_JSON_BYTES = 32_768
MAX_ARGUMENT_BYTES = 8_192
MAX_OUTPUT_BYTES = 2_000_000
MAX_OUTPUT_TAIL_BYTES = 32_768
GIT_BATCH_TIMEOUT_SECONDS = 120.0
MAX_TMPFS_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TMPFS_BYTES = 512 * 1024 * 1024
MAX_SANDBOX_USER_TASKS = 4096
SANDBOX_USER_TASK_MARGIN = 256
MAX_SANDBOX_ADDRESS_SPACE_BYTES = 4 * 1024 * 1024 * 1024
MIN_SANDBOX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
MAX_SANDBOX_OPEN_FILES = 1024
MIN_SANDBOX_OPEN_FILES = 64
MAX_VENV_SITE_ENTRIES = 100_000
MAX_VENV_SITE_BYTES = 16 * 1024 * 1024 * 1024
STARTUP_HOOK_NAMES = {"sitecustomize.py", "usercustomize.py"}
PYTHON_EXECUTABLE_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+)?)$")
PYTHON_CONSOLE_LAUNCHERS = {
    "black",
    "coverage",
    "coverage3",
    "django-admin",
    "flask",
    "gunicorn",
    "mypy",
    "nox",
    "pip",
    "pip3",
    "py.test",
    "pytest",
    "ruff",
    "tox",
    "uvicorn",
}
PYTHON_CONSOLE_LAUNCHER_RE = re.compile(
    r"^(?:black|coverage|django-admin|flask|gunicorn|mypy|nox|pip|py\.test|pytest|ruff|tox|uvicorn)"
    r"(?:[-.]?\d+(?:\.\d+)*)?$"
)
DENIED_COMMAND_INTERPRETERS = {"env", "sh", "bash", "dash", "zsh", "fish"}
ALLOWED_SYSTEM_PYTHON = "/usr/bin/python3"
ALLOWED_VENV_PYTHON = "/runtime/venv/bin/python"
SECCOMP_POLICY_VERSION = "minimax-verifier-seccomp-v1"
SECCOMP_DENIED_SYSCALLS = (
    "add_key",
    "request_key",
    "keyctl",
    "bpf",
    "perf_event_open",
    "ptrace",
    "io_uring_setup",
    "io_uring_register",
    "io_uring_enter",
    "userfaultfd",
    "open_by_handle_at",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "reboot",
    "swapon",
    "swapoff",
    "process_vm_readv",
    "process_vm_writev",
    "process_madvise",
    "pidfd_getfd",
    "mount",
    "umount2",
    "pivot_root",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    "setns",
    "unshare",
    "name_to_handle_at",
)
SECCOMP_ALLOWED_SOCKET_FAMILIES = (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6)
MASKED_PROC_FILES = (
    "/proc/keys",
    "/proc/key-users",
)
SECCOMP_POLICY_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "version": SECCOMP_POLICY_VERSION,
            "deny_syscalls": SECCOMP_DENIED_SYSCALLS,
            "allow_socket_families": SECCOMP_ALLOWED_SOCKET_FAMILIES,
            "allow_socketpair_families": (socket.AF_UNIX,),
            "default": "allow",
            "denied_action": "errno:EPERM",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


class VerifyError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class FrozenCandidate:
    job_id: str
    patch_path: Path
    patch_sha256: str
    source_head_commit: str
    source_snapshot_sha256: str
    files: tuple[dict[str, Any], ...]
    worker_sha256: str


@dataclass(frozen=True)
class FrozenWorker:
    fd: int
    sha256: str
    device: int
    inode: int
    size: int


@dataclass(frozen=True)
class VenvRuntime:
    base_python_root: Path
    skeleton: Path
    site_packages: Path
    python_version: str


@dataclass(frozen=True)
class SandboxResourceLimits:
    address_space_bytes: int
    cpu_seconds: int
    file_size_bytes: int
    open_files: int
    user_tasks: int


@dataclass(frozen=True)
class CommandResult:
    phase: str
    argv_sha256: str
    returncode: int
    duration_seconds: float
    timed_out: bool
    output_limited: bool
    output_bytes: int
    output_sha256: str
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.output_limited

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "argv_sha256": self.argv_sha256,
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 6),
            "timed_out": self.timed_out,
            "output_limited": self.output_limited,
            "output_bytes": self.output_bytes,
            "output_sha256": self.output_sha256,
            "output_content": "omitted_not_eligible_for_automatic_repair_egress",
            "passed": self.passed,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def clean_env(*, home: Path | str = "/nonexistent") -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: float = 60.0,
    max_output_bytes: int = MAX_WORKER_OUTPUT_BYTES,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise VerifyError("timeout_invalid", "Local command timeout must be finite and positive")
    try:
        result = subprocess.run(  # noqa: S603 - every executable is fixed by the verifier
            argv,
            cwd=cwd,
            env=clean_env(),
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerifyError("local_command_timeout", f"Local command timed out: {Path(argv[0]).name}") from exc
    if len(result.stdout) > max_output_bytes or len(result.stderr) > max_output_bytes:
        raise VerifyError("local_command_output_limit", f"Local command output is too large: {Path(argv[0]).name}")
    return result


def git_command(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    if not GIT.is_file():
        raise VerifyError("git_missing", "The fixed /usr/bin/git executable is unavailable")
    argv = [
        str(GIT),
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
        *arguments,
    ]
    result = _run(argv, cwd=repo)
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise VerifyError("git_read_failed", detail or f"Git command failed: {arguments[0]}")
    return result


def resolve_repo(raw: str) -> Path:
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise VerifyError("repo_invalid", "--repo must be a non-symlink Git working-tree directory")
    result = git_command(candidate, "rev-parse", "--show-toplevel")
    try:
        top = Path(result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    except UnicodeError as exc:
        raise VerifyError("repo_invalid", "Git repository path is not UTF-8") from exc
    if top != candidate:
        raise VerifyError("repo_toplevel_mismatch", "--repo must be the exact Git working-tree root")
    verify_directory_components(candidate, "repository")
    return candidate


def verify_directory_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise VerifyError("path_component_missing", f"Cannot inspect {label} path components") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise VerifyError("path_component_symlink", f"{label} contains a symlink or non-directory: {current}")


def verify_private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VerifyError("private_directory_missing", f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise VerifyError("private_directory_invalid", f"{label} must be a non-symlink directory")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise VerifyError("private_directory_permissions", f"{label} must be owned by this user with mode 0700")


def verifier_temp_root() -> Path:
    base = FIXED_TEMP_PARENT / f"minimax-api-worker-{os.getuid()}"
    verify_private_directory(base, "MiniMax worker temporary base")
    root = base / "verify"
    if root.exists() or root.is_symlink():
        verify_private_directory(root, "Patch verification root")
        return root
    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        raise VerifyError("verification_root_create_failed", "Cannot create the private verification root") from exc
    verify_private_directory(root, "Patch verification root")
    return root


def adjacent_worker_path() -> Path:
    return Path(__file__).resolve().with_name(WORKER_NAME)


def _worker_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def freeze_adjacent_worker(destination: Path) -> FrozenWorker:
    """Hash adjacent bytes once and freeze them into one immutable sealed memfd."""

    verify_private_directory(destination, "Private verification directory")
    source = adjacent_worker_path()
    source_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise VerifyError("worker_missing", "The adjacent MiniMax API worker is unavailable") from exc
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > MAX_WORKER_BYTES
        ):
            raise VerifyError(
                "worker_invalid",
                "The adjacent MiniMax API worker has unsafe type, ownership, mode, links, or size",
            )
        chunks: list[bytes] = []
        remaining = MAX_WORKER_BYTES + 1
        while remaining:
            chunk = os.read(source_fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(source_fd)
        if _worker_identity(before) != _worker_identity(after) or len(data) != before.st_size:
            raise VerifyError("worker_changed_during_freeze", "Adjacent MiniMax worker changed while being frozen")
        worker_hash = sha256_bytes(data)
        if worker_hash != EXPECTED_WORKER_SHA256:
            raise VerifyError(
                "worker_binary_mismatch",
                "Adjacent MiniMax worker does not match the verifier's pinned audited SHA-256",
            )
        try:
            source_path_metadata = source.lstat()
        except OSError as exc:
            raise VerifyError("worker_changed_during_freeze", "Adjacent MiniMax worker path changed") from exc
        if _worker_identity(source_path_metadata) != _worker_identity(after):
            raise VerifyError("worker_changed_during_freeze", "Adjacent MiniMax worker path changed")
    finally:
        os.close(source_fd)

    required_seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    frozen_fd = -1
    try:
        frozen_fd = os.memfd_create(
            "minimax-audited-worker",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(frozen_fd, view[written:])
            if count <= 0:
                raise OSError("short frozen worker write")
            written += count
        os.fchmod(frozen_fd, 0o400)
        os.fsync(frozen_fd)
        fcntl.fcntl(frozen_fd, fcntl.F_ADD_SEALS, required_seals)
        frozen_metadata = os.fstat(frozen_fd)
        if (
            not stat.S_ISREG(frozen_metadata.st_mode)
            or frozen_metadata.st_uid != os.getuid()
            or frozen_metadata.st_nlink != 0
            or stat.S_IMODE(frozen_metadata.st_mode) != 0o400
            or frozen_metadata.st_size != len(data)
            or fcntl.fcntl(frozen_fd, fcntl.F_GET_SEALS) & required_seals != required_seals
        ):
            raise VerifyError("worker_freeze_failed", "Frozen worker artifact has unsafe identity")
        frozen_data = bytearray()
        offset = 0
        while offset < frozen_metadata.st_size:
            chunk = os.pread(frozen_fd, min(1024 * 1024, frozen_metadata.st_size - offset), offset)
            if not chunk:
                break
            frozen_data.extend(chunk)
            offset += len(chunk)
        if bytes(frozen_data) != data or sha256_bytes(frozen_data) != EXPECTED_WORKER_SHA256:
            raise VerifyError("worker_freeze_failed", "Frozen worker bytes do not match the audited source")
        current_source = source.lstat()
        if _worker_identity(current_source) != _worker_identity(after):
            raise VerifyError("worker_changed_during_freeze", "Adjacent MiniMax worker changed after copying")
        return FrozenWorker(
            fd=frozen_fd,
            sha256=worker_hash,
            device=frozen_metadata.st_dev,
            inode=frozen_metadata.st_ino,
            size=frozen_metadata.st_size,
        )
    except VerifyError:
        raise
    except (AttributeError, OSError) as exc:
        raise VerifyError("worker_freeze_failed", "Cannot create the private sealed worker") from exc
    finally:
        if frozen_fd >= 0 and sys.exc_info()[0] is not None:
            os.close(frozen_fd)


def verify_frozen_worker(worker: FrozenWorker) -> None:
    required_seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    try:
        before = os.fstat(worker.fd)
        seals = fcntl.fcntl(worker.fd, fcntl.F_GET_SEALS)
    except OSError as exc:
        raise VerifyError("frozen_worker_changed", "Frozen audited worker descriptor is unavailable") from exc
    expected_identity = (worker.device, worker.inode, worker.size)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 0
        or stat.S_IMODE(before.st_mode) != 0o400
        or (before.st_dev, before.st_ino, before.st_size) != expected_identity
        or seals & required_seals != required_seals
    ):
        raise VerifyError("frozen_worker_changed", "Frozen audited worker changed identity or seals")
    chunks: list[bytes] = []
    offset = 0
    while offset < before.st_size:
        try:
            chunk = os.pread(worker.fd, min(1024 * 1024, before.st_size - offset), offset)
        except OSError as exc:
            raise VerifyError("frozen_worker_changed", "Cannot read frozen audited worker") from exc
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    try:
        after = os.fstat(worker.fd)
    except OSError as exc:
        raise VerifyError("frozen_worker_changed", "Frozen audited worker changed") from exc
    if (
        offset != before.st_size
        or (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size, after.st_ctime_ns)
        != (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size, before.st_ctime_ns)
        or sha256_bytes(b"".join(chunks)) != worker.sha256
        or worker.sha256 != EXPECTED_WORKER_SHA256
    ):
        raise VerifyError("frozen_worker_changed", "Frozen audited worker changed content")


def call_worker(
    worker: FrozenWorker,
    repo: Path,
    state_dir: str | None,
    command: str,
    job_id: str,
) -> dict[str, Any]:
    verify_frozen_worker(worker)
    inherited_path = f"/proc/self/fd/{worker.fd}"
    argv = [str(PYTHON), "-I", inherited_path, "--json", "--repo", str(repo)]
    if state_dir:
        argv.extend(("--state-dir", state_dir))
    argv.extend(("job", command, job_id))
    result = _run(argv, timeout=90.0, pass_fds=(worker.fd,))
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VerifyError("worker_output_invalid", "The adjacent worker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VerifyError("worker_output_invalid", "The adjacent worker response is not an object")
    if result.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else "worker_failed"
        raise VerifyError("worker_candidate_rejected", f"Worker rejected frozen candidate: {code}", exit_code=1)
    return payload


def read_regular(
    path: Path,
    max_bytes: int,
    label: str,
    *,
    reject_executable: bool = False,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerifyError("file_read_failed", f"Cannot safely read {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise VerifyError("file_type_denied", f"{label} must be a single-link regular file")
        if reject_executable and before.st_mode & 0o111:
            raise VerifyError(
                "source_executable_changed",
                f"{label} has executable mode bits outside the sealed source policy",
            )
        if before.st_size > max_bytes:
            raise VerifyError("file_size_limit", f"{label} exceeds its size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise VerifyError("file_size_limit", f"{label} exceeds its size limit")
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise VerifyError("file_changed_during_read", f"{label} changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def validate_candidate_request(job_id: str, expected_patch_sha256: str) -> None:
    if not JOB_ID_RE.fullmatch(job_id):
        raise VerifyError("job_id_invalid", "Job ID must be 20 lowercase hexadecimal characters")
    if not SHA256_RE.fullmatch(expected_patch_sha256):
        raise VerifyError("expected_patch_hash_invalid", "--expect-patch-sha256 must be 64 lowercase hex characters")


def load_frozen_candidate(
    worker: FrozenWorker,
    repo: Path,
    state_dir: str | None,
    job_id: str,
    expected_patch_sha256: str,
) -> FrozenCandidate:
    validate_candidate_request(job_id, expected_patch_sha256)
    status = call_worker(worker, repo, state_dir, "status", job_id)
    if status.get("status") != "PATCH_READY":
        raise VerifyError("patch_not_ready", f"Job status is {status.get('status')}", exit_code=1)
    if (
        status.get("runner_version") != EXPECTED_RUNNER_VERSION
        or status.get("contract_version") != EXPECTED_WORKER_CONTRACT
        or status.get("endpoint") != EXPECTED_ENDPOINT
        or status.get("model") != EXPECTED_MODEL
    ):
        raise VerifyError(
            "worker_contract_mismatch",
            "Frozen job does not match the verifier's audited worker contract",
        )
    if status.get("runner_sha256") != worker.sha256:
        raise VerifyError("worker_hash_mismatch", "Job was not created by the adjacent audited worker")
    result = status.get("result")
    if not isinstance(result, dict) or result.get("patch_sha256") != expected_patch_sha256:
        raise VerifyError(
            "expected_patch_hash_mismatch",
            "Reviewed patch hash does not match the sealed job",
            exit_code=1,
        )
    exported = call_worker(worker, repo, state_dir, "diff", job_id)
    if exported.get("sha256") != expected_patch_sha256:
        raise VerifyError("export_patch_hash_mismatch", "Deterministic export does not match the reviewed patch hash")
    expected_export = (
        FIXED_TEMP_PARENT / f"minimax-api-worker-{os.getuid()}" / "exports" / f"{job_id}.patch"
    )
    patch_path = Path(str(exported.get("patch", "")))
    if patch_path != expected_export:
        raise VerifyError("patch_export_path_invalid", "Worker returned an unexpected patch export path")
    patch_data = read_regular(patch_path, 2_000_000, "frozen patch export")
    if sha256_bytes(patch_data) != expected_patch_sha256:
        raise VerifyError("patch_export_changed", "Frozen patch export changed after worker validation")
    files = status.get("files")
    source_head = status.get("source_head_commit")
    source_snapshot = status.get("source_snapshot_sha256")
    if not isinstance(source_head, str) or not re.fullmatch(r"[0-9a-f]{40,64}", source_head):
        raise VerifyError("source_manifest_invalid", "Job source HEAD is invalid")
    if not isinstance(source_snapshot, str) or not SHA256_RE.fullmatch(source_snapshot):
        raise VerifyError("source_manifest_invalid", "Job source snapshot hash is invalid")
    validated_files = validate_source_manifest(files, source_snapshot)
    return FrozenCandidate(
        job_id=job_id,
        patch_path=patch_path,
        patch_sha256=expected_patch_sha256,
        source_head_commit=source_head,
        source_snapshot_sha256=source_snapshot,
        files=validated_files,
        worker_sha256=worker.sha256,
    )


def validate_source_manifest(raw_files: object, expected_snapshot_sha256: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_files, list) or not raw_files:
        raise VerifyError("source_manifest_invalid", "Job source file manifest is missing")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise VerifyError("source_manifest_invalid", "Job source file manifest is malformed")
        raw_path = item.get("path")
        expected_bytes = item.get("bytes")
        expected_hash = item.get("sha256")
        if (
            not isinstance(raw_path, str)
            or raw_path in seen
            or not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or expected_bytes > MAX_PROJECTION_FILE_BYTES
            or not isinstance(expected_hash, str)
            or not SHA256_RE.fullmatch(expected_hash)
            or item.get("source_executable") is not False
        ):
            raise VerifyError("source_manifest_invalid", "Selected source manifest entry is invalid")
        validate_relative_path(raw_path, "selected source path")
        seen.add(raw_path)
        validated.append(dict(item))
    canonical = json.dumps(validated, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if sha256_bytes(canonical) != expected_snapshot_sha256:
        raise VerifyError("source_manifest_invalid", "Selected source manifest hash does not match the sealed snapshot")
    return tuple(validated)


def validate_relative_path(raw: str, label: str) -> PurePosixPath:
    if not raw or CONTROL_RE.search(raw):
        raise VerifyError("projection_path_invalid", f"{label} contains control characters or is empty")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise VerifyError("projection_path_invalid", f"{label} escapes the verification projection")
    if relative.name.lower() in STARTUP_HOOK_NAMES:
        raise VerifyError("projection_startup_hook_denied", f"Startup hook is not executable in verification: {raw}")
    return relative


def ensure_safe_parent(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() or current.is_symlink():
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise VerifyError("projection_parent_invalid", "Cannot inspect projection parent") from exc
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise VerifyError("projection_parent_invalid", f"Projection parent is not a directory: {current}")
        else:
            current.mkdir(mode=0o700)
    return root / relative.as_posix()


def write_projection_file(root: Path, relative: PurePosixPath, data: bytes, mode: int) -> None:
    target = ensure_safe_parent(root, relative)
    if target.exists() or target.is_symlink():
        metadata = target.lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise VerifyError("projection_target_invalid", f"Projection target has unsafe type: {relative}")
        target.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, mode)
    except OSError as exc:
        raise VerifyError("projection_write_failed", f"Cannot materialize projection path: {relative}") from exc
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short projection write")
            written += count
    finally:
        os.close(descriptor)
    os.chmod(target, mode)


def git_batch_argv() -> list[str]:
    return [
        str(GIT),
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
        "cat-file",
        "--batch",
    ]


class TimedBatchReader:
    def __init__(self, stream: Any, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline
        self.buffer = bytearray()

    def _fill(self, label: str) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VerifyError("git_batch_timeout", f"Git cat-file timed out while reading {label}")
        ready, _, _ = select.select((self.stream,), (), (), remaining)
        if not ready:
            raise VerifyError("git_batch_timeout", f"Git cat-file timed out while reading {label}")
        chunk = os.read(self.stream.fileno(), 65_536)
        if not chunk:
            raise VerifyError("git_batch_truncated", f"Git cat-file truncated {label}")
        self.buffer.extend(chunk)

    def line(self, max_bytes: int, label: str) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > max_bytes:
                    raise VerifyError("git_batch_header_invalid", f"Git batch header is too long for {label}")
                result = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                return result
            if len(self.buffer) >= max_bytes:
                raise VerifyError("git_batch_header_invalid", f"Git batch header is too long for {label}")
            self._fill(label)

    def exact(self, size: int, label: str) -> bytes:
        while len(self.buffer) < size:
            self._fill(label)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result


def materialize_git_entry(destination: Path, mode: str, relative: PurePosixPath, data: bytes) -> None:
    if mode == "100644":
        write_projection_file(destination, relative, data, 0o600)
        return
    if mode == "100755":
        write_projection_file(destination, relative, data, 0o700)
        return
    if mode != "120000":
        raise VerifyError("projection_mode_denied", f"Unsupported Git mode {mode} at {relative}")
    try:
        target_text = data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise VerifyError("projection_symlink_denied", f"Non-UTF-8 symlink at {relative}") from exc
    target_relative = PurePosixPath(target_text)
    if target_relative.is_absolute() or ".." in target_relative.parts or CONTROL_RE.search(target_text):
        raise VerifyError("projection_symlink_denied", f"Escaping symlink at {relative}")
    target = ensure_safe_parent(destination, relative)
    os.symlink(target_text, target)


def materialize_git_blobs(
    repo: Path,
    entries: list[tuple[str, str, str, int, PurePosixPath]],
    destination: Path,
) -> None:
    process = subprocess.Popen(  # noqa: S603 - fixed Git batch, no hooks, filters, or checkout
        git_batch_argv(),
        cwd=repo,
        env=clean_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        close_fds=True,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise VerifyError("git_batch_failed", "Cannot establish Git cat-file batch pipes")
    deadline = time.monotonic() + GIT_BATCH_TIMEOUT_SECONDS
    reader = TimedBatchReader(process.stdout, deadline)
    try:
        for mode, object_type, oid, expected_size, relative in entries:
            if object_type != "blob":
                raise VerifyError("projection_object_denied", f"Unsupported Git tree object at {relative}")
            if expected_size > MAX_PROJECTION_FILE_BYTES:
                raise VerifyError(
                    "projection_file_size_limit",
                    f"Tracked Git blob exceeds the per-file projection limit: {relative}",
                )
            query = f"{oid}\n".encode("ascii")
            if process.stdin.write(query) != len(query):
                raise VerifyError("git_batch_write_failed", f"Cannot query Git object for {relative}")
            process.stdin.flush()
            header = reader.line(256, str(relative))
            try:
                actual_oid, actual_type, raw_size = header.rstrip(b"\n").split()
                actual_size = int(raw_size)
                actual_oid_text = actual_oid.decode("ascii", errors="strict")
            except (UnicodeError, ValueError) as exc:
                raise VerifyError("git_batch_header_invalid", f"Malformed Git batch header for {relative}") from exc
            if actual_oid_text != oid or actual_type != b"blob" or actual_size != expected_size:
                raise VerifyError("git_object_changed", f"Git object identity changed for {relative}")
            data = reader.exact(actual_size, str(relative))
            if reader.exact(1, str(relative)) != b"\n":
                raise VerifyError("git_batch_framing_invalid", f"Git batch framing is invalid for {relative}")
            materialize_git_entry(destination, mode, relative, data)
        process.stdin.close()
        remaining = max(0.001, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise VerifyError("git_batch_timeout", "Git cat-file did not terminate before its deadline") from exc
        if returncode != 0:
            raise VerifyError("git_batch_failed", "Git cat-file batch failed while building the projection")
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5.0)
        raise
    finally:
        for stream in (process.stdin, process.stdout):
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def materialize_tracked_head(repo: Path, commit: str, destination: Path) -> None:
    result = git_command(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", commit)
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if len(entries) > MAX_PROJECTION_FILES:
        raise VerifyError("projection_file_limit", "Tracked source projection contains too many files")
    parsed: list[tuple[str, str, str, int, PurePosixPath]] = []
    total_bytes = 0
    for entry in entries:
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_type, oid, raw_size = metadata.split()
            path_text = raw_path.decode("utf-8", errors="strict")
            size = int(raw_size)
        except (ValueError, UnicodeError) as exc:
            raise VerifyError("git_tree_invalid", "Git tree contains an unsupported entry") from exc
        relative = validate_relative_path(path_text, "Git tree path")
        parsed.append((mode.decode(), object_type.decode(), oid.decode(), size, relative))
        total_bytes += size
        if total_bytes > MAX_PROJECTION_BYTES:
            raise VerifyError("projection_size_limit", "Tracked source projection is too large")
    materialize_git_blobs(repo, parsed, destination)


def overlay_selected_snapshot(repo: Path, files: tuple[dict[str, Any], ...], destination: Path) -> None:
    seen: set[str] = set()
    for item in files:
        raw_path = item.get("path")
        expected_bytes = item.get("bytes")
        expected_hash = item.get("sha256")
        if (
            not isinstance(raw_path, str)
            or raw_path in seen
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_hash, str)
            or not SHA256_RE.fullmatch(expected_hash)
            or item.get("source_executable") is not False
        ):
            raise VerifyError("source_manifest_invalid", "Selected source manifest entry is invalid")
        seen.add(raw_path)
        relative = validate_relative_path(raw_path, "selected source path")
        data = read_regular(
            repo / relative.as_posix(),
            expected_bytes,
            f"selected source {raw_path}",
            reject_executable=True,
        )
        if len(data) != expected_bytes or sha256_bytes(data) != expected_hash:
            raise VerifyError("source_snapshot_changed", f"Selected source changed after frozen export: {raw_path}")
        write_projection_file(destination, relative, data, 0o600)


def verify_selected_snapshot(repo: Path, files: tuple[dict[str, Any], ...]) -> None:
    for item in files:
        raw_path = item.get("path")
        expected_bytes = item.get("bytes")
        expected_hash = item.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected_bytes, int)
            or not isinstance(expected_hash, str)
            or item.get("source_executable") is not False
        ):
            raise VerifyError("source_manifest_invalid", "Selected source manifest entry is invalid")
        relative = validate_relative_path(raw_path, "selected source path")
        data = read_regular(
            repo / relative.as_posix(),
            expected_bytes,
            f"selected source {raw_path}",
            reject_executable=True,
        )
        if len(data) != expected_bytes or sha256_bytes(data) != expected_hash:
            raise VerifyError("source_snapshot_changed", f"Selected source changed during verification: {raw_path}")


def verify_source_head(repo: Path, expected: str) -> None:
    current = git_command(repo, "rev-parse", "HEAD").stdout.decode("ascii", errors="strict").strip()
    if current != expected:
        raise VerifyError("source_head_changed", "Source HEAD changed during frozen patch verification", exit_code=1)


def apply_frozen_patch(destination: Path, candidate: FrozenCandidate) -> None:
    patch_data = read_regular(candidate.patch_path, 2_000_000, "frozen patch export")
    if sha256_bytes(patch_data) != candidate.patch_sha256:
        raise VerifyError("patch_export_changed", "Frozen patch changed before temporary application")
    private_patch = destination.parent / "frozen.patch"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(private_patch, flags, 0o600)
    except OSError as exc:
        raise VerifyError(
            "private_patch_create_failed",
            "Cannot copy frozen patch into private verification state",
        ) from exc
    try:
        view = memoryview(patch_data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short private patch write")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    candidate = replace(candidate, patch_path=private_patch)
    for check_only in (True, False):
        arguments = ["apply"]
        if check_only:
            arguments.append("--check")
        arguments.extend(("--recount", "--whitespace=nowarn", str(candidate.patch_path)))
        result = git_command(destination, *arguments, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
            code = "patch_apply_check_failed" if check_only else "patch_apply_failed"
            raise VerifyError(code, detail or "Frozen patch cannot be applied to the private projection", exit_code=1)


def parse_command_json(raw: str, phase: str) -> list[str]:
    if len(raw.encode("utf-8")) > MAX_COMMAND_JSON_BYTES:
        raise VerifyError("command_json_limit", f"{phase} command JSON is too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerifyError("command_json_invalid", f"{phase} command must be a JSON argv array") from exc
    if not isinstance(value, list) or not value or len(value) > MAX_COMMAND_ARGUMENTS:
        raise VerifyError("command_argv_invalid", f"{phase} command must be a non-empty bounded argv array")
    argv: list[str] = []
    for argument in value:
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES
            or CONTROL_RE.search(argument)
        ):
            raise VerifyError("command_argv_invalid", f"{phase} command contains an invalid argument")
        argv.append(argument)
    if argv[0] not in {ALLOWED_SYSTEM_PYTHON, ALLOWED_VENV_PYTHON}:
        raise VerifyError(
            "command_executable_denied",
            f"{phase} must use the exact isolated Python executable selected by the verifier",
        )
    return argv


def venv_python_is_isolated(command: list[str]) -> bool:
    saw_no_site = False
    saw_safe_path = False
    for argument in command[1:]:
        if argument == "-S":
            saw_no_site = True
            continue
        if argument == "-P":
            saw_safe_path = True
            continue
        if argument in {"-c", "-m", "--"} or argument.startswith(("-c", "-m")) or not argument.startswith("-"):
            return saw_no_site and saw_safe_path
    return saw_no_site and saw_safe_path


def system_python_is_isolated(command: list[str]) -> bool:
    saw_isolated = False
    saw_no_site = False
    saw_safe_path = False
    for argument in command[1:]:
        if argument == "-I":
            saw_isolated = True
            continue
        if argument == "-S":
            saw_no_site = True
            continue
        if argument == "-P":
            saw_safe_path = True
            continue
        if argument in {"-c", "-m", "--"} or argument.startswith(("-c", "-m")) or not argument.startswith("-"):
            return saw_isolated and saw_no_site and saw_safe_path
    return saw_isolated and saw_no_site and saw_safe_path


def validate_verification_command(command: list[str], phase: str, *, venv_enabled: bool) -> None:
    expected = ALLOWED_VENV_PYTHON if venv_enabled else ALLOWED_SYSTEM_PYTHON
    if command[0] != expected:
        raise VerifyError(
            "command_executable_denied",
            f"{phase} must use exactly {expected}",
        )
    if command[0] == ALLOWED_VENV_PYTHON:
        if not venv_python_is_isolated(command):
            raise VerifyError(
                "python_site_hook_denied",
                f"{phase} venv Python must put both -S and -P before -m, -c, --, or a script",
            )
        return
    if not system_python_is_isolated(command):
        raise VerifyError(
            "python_site_hook_denied",
            f"{phase} system Python must put -I, -S, and -P before -m, -c, --, or a script",
        )


def validate_site_packages_tree(site_packages: Path) -> None:
    """Bound the trusted host dependency tree and deny active/special entries."""

    try:
        root_metadata = site_packages.lstat()
    except OSError as exc:
        raise VerifyError("venv_invalid", "Cannot inspect venv site-packages") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise VerifyError("venv_invalid", "venv site-packages must be a non-symlink directory")
    entry_count = 0
    total_bytes = 0

    def walk_error(error: OSError) -> None:
        raise VerifyError("venv_tree_invalid", "Cannot traverse the bounded venv dependency tree") from error

    for directory, child_directories, filenames in os.walk(
        site_packages,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        current = Path(directory)
        for name, expect_directory in (
            *((name, True) for name in child_directories),
            *((name, False) for name in filenames),
        ):
            entry_count += 1
            if entry_count > MAX_VENV_SITE_ENTRIES:
                raise VerifyError("venv_tree_limit", "venv site-packages exceeds the bounded entry limit")
            if name.lower() in STARTUP_HOOK_NAMES:
                raise VerifyError("venv_startup_hook_denied", f"venv startup hook is denied: {name}")
            child = current / name
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise VerifyError("venv_tree_invalid", "Cannot inspect a venv dependency entry") from exc
            if metadata.st_dev != root_metadata.st_dev or stat.S_ISLNK(metadata.st_mode):
                raise VerifyError("venv_tree_invalid", "venv dependency tree contains a link or mount boundary")
            if expect_directory:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise VerifyError("venv_tree_invalid", "venv dependency tree contains an unsafe directory entry")
            elif not stat.S_ISREG(metadata.st_mode):
                raise VerifyError("venv_tree_invalid", "venv dependency tree contains a special file")
            else:
                total_bytes += metadata.st_size
                if total_bytes > MAX_VENV_SITE_BYTES:
                    raise VerifyError("venv_tree_limit", "venv site-packages exceeds the bounded byte limit")


def prepare_venv_runtime(repo: Path, raw: str | None, temporary: Path) -> VenvRuntime | None:
    if raw is None:
        return None
    relative = validate_relative_path(raw, "venv path")
    venv = repo / relative.as_posix()
    verify_directory_components(venv, "venv")
    try:
        metadata = venv.lstat()
    except OSError as exc:
        raise VerifyError("venv_missing", "Selected venv is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or venv.is_symlink() or metadata.st_mode & 0o022:
        raise VerifyError("venv_invalid", "Selected venv must be a non-symlink directory not writable by group/other")
    config_data = read_regular(venv / "pyvenv.cfg", 16_384, "venv pyvenv.cfg")
    try:
        config_text = config_data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise VerifyError("venv_invalid", "venv pyvenv.cfg is not UTF-8") from exc
    version_match = re.search(r"(?m)^version_info\s*=\s*(\d+\.\d+)", config_text)
    if version_match is None:
        version_match = re.search(r"(?m)^version\s*=\s*(\d+\.\d+)", config_text)
    if version_match is None:
        raise VerifyError("venv_invalid", "Cannot determine venv Python major/minor version")
    python_version = version_match.group(1)
    python_link = venv / "bin" / "python"
    verify_directory_components(python_link.parent, "venv Python parent")
    resolved_python = python_link.resolve(strict=True)
    if not resolved_python.is_file() or not os.access(resolved_python, os.X_OK):
        raise VerifyError("venv_invalid", "venv Python executable is unavailable")
    verify_directory_components(resolved_python.parent, "resolved venv Python parent")
    base_root = resolved_python.parent.parent
    verify_directory_components(base_root, "venv base runtime")
    if base_root.is_symlink() or not base_root.is_dir():
        raise VerifyError("venv_invalid", "venv base Python distribution is unsafe")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if base_root != Path("/usr") and Path("/usr") not in base_root.parents:
        try:
            base_relative_home = base_root.relative_to(home)
        except ValueError as exc:
            raise VerifyError("venv_base_runtime_denied", "venv base Python is outside trusted runtime roots") from exc
        if len(base_relative_home.parts) < 5 or base_relative_home.parts[:4] != (".local", "share", "uv", "python"):
            raise VerifyError("venv_base_runtime_denied", "venv base Python root is too broad to mount safely")
    unresolved_site_packages = venv / "lib" / f"python{python_version}" / "site-packages"
    verify_directory_components(unresolved_site_packages, "venv site-packages")
    site_packages = unresolved_site_packages.resolve()
    try:
        site_packages.relative_to(venv.resolve())
    except ValueError as exc:
        raise VerifyError("venv_invalid", "venv site-packages escapes the selected venv") from exc
    if not site_packages.is_dir() or site_packages.is_symlink():
        raise VerifyError("venv_invalid", "venv site-packages directory is unavailable")
    validate_site_packages_tree(site_packages)
    skeleton = temporary / "runtime-venv"
    (skeleton / "bin").mkdir(parents=True, mode=0o700)
    (skeleton / "lib" / f"python{python_version}" / "site-packages").mkdir(parents=True, mode=0o700)
    (skeleton / "pyvenv.cfg").write_text(
        f"home = /runtime/python/bin\nversion = {python_version}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    binary_name = resolved_python.name
    os.symlink(f"/runtime/python/bin/{binary_name}", skeleton / "bin" / "python")
    return VenvRuntime(
        base_python_root=base_root,
        skeleton=skeleton,
        site_packages=site_packages,
        python_version=python_version,
    )


def sandbox_environment(venv: VenvRuntime | None) -> dict[str, str]:
    python_paths = ["/work/src", "/work"]
    if venv is not None:
        python_paths.append(f"/runtime/venv/lib/python{venv.python_version}/site-packages")
    return {
        "COVERAGE_FILE": "/tmp/.coverage",  # noqa: S108 - private sandbox tmpfs
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/home/sandbox",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/runtime/venv/bin:/usr/local/bin:/usr/bin:/bin" if venv else "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": ":".join(python_paths),
        "PYTHONSAFEPATH": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TMPDIR": "/tmp",  # noqa: S108 - private sandbox tmpfs
        "TZ": "UTC",
        "XDG_CACHE_HOME": "/home/sandbox/.cache",
        "XDG_CONFIG_HOME": "/home/sandbox/.config",
        "XDG_STATE_HOME": "/home/sandbox/.local/state",
    }


def append_host_system_layout(argv: list[str]) -> None:
    usr = Path("/usr")
    for path in (Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VerifyError("sandbox_system_layout_invalid", f"Cannot inspect host system path: {path}") from exc
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(usr)
        except (OSError, ValueError) as exc:
            raise VerifyError("sandbox_system_layout_invalid", f"Host system path escapes /usr: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            if not target or CONTROL_RE.search(target):
                raise VerifyError("sandbox_system_layout_invalid", f"Host system symlink is invalid: {path}")
            argv.extend(("--symlink", target, str(path)))
        elif stat.S_ISDIR(metadata.st_mode):
            argv.extend(("--ro-bind", str(path), str(path)))
        else:
            raise VerifyError("sandbox_system_layout_invalid", f"Host system path has unsafe type: {path}")


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


def create_seccomp_policy_fd() -> int:
    """Compile and seal the mandatory verifier seccomp policy using fixed libseccomp."""

    try:
        resolved = LIBSECCOMP.resolve(strict=True)
        metadata = resolved.stat()
        root_metadata = Path("/").stat()
        usr_metadata = Path("/usr").stat()
        lib_metadata = Path("/usr/lib").stat()
    except OSError as exc:
        raise VerifyError("sandbox_unavailable", "Pinned libseccomp is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or (metadata.st_uid, metadata.st_gid) != (root_metadata.st_uid, root_metadata.st_gid)
        or (usr_metadata.st_uid, usr_metadata.st_gid) != (root_metadata.st_uid, root_metadata.st_gid)
        or (lib_metadata.st_uid, lib_metadata.st_gid) != (root_metadata.st_uid, root_metadata.st_gid)
        or usr_metadata.st_mode & 0o022
        or lib_metadata.st_mode & 0o022
        or (resolved != Path("/usr/lib") and Path("/usr/lib") not in resolved.parents)
    ):
        raise VerifyError("sandbox_unavailable", "Pinned libseccomp has unsafe identity or permissions")
    try:
        library = ctypes.CDLL(str(resolved), mode=os.RTLD_LOCAL | os.RTLD_NOW, use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        library.seccomp_release.restype = None
        library.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        library.seccomp_attr_set.restype = ctypes.c_int
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_ScmpArgCmp),
        ]
        library.seccomp_rule_add_array.restype = ctypes.c_int
        library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.seccomp_export_bpf.restype = ctypes.c_int
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise VerifyError("sandbox_unavailable", "Cannot load the pinned libseccomp API") from exc

    allow_action = 0x7FFF0000  # SCMP_ACT_ALLOW
    deny_action = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
    context = library.seccomp_init(allow_action)
    if not context:
        raise VerifyError("sandbox_unavailable", "Cannot initialize the mandatory seccomp policy")
    descriptor = -1
    try:
        if library.seccomp_attr_set(context, 2, 0x80000000) != 0:  # BADARCH => KILL_PROCESS
            raise VerifyError("sandbox_unavailable", "Cannot set the seccomp bad-architecture action")
        for syscall_name in SECCOMP_DENIED_SYSCALLS:
            syscall_number = library.seccomp_syscall_resolve_name(syscall_name.encode("ascii"))
            if syscall_number == -1 or library.seccomp_rule_add_array(
                context,
                deny_action,
                syscall_number,
                0,
                None,
            ) != 0:
                raise VerifyError(
                    "sandbox_unavailable",
                    f"Cannot compile mandatory seccomp rule: {syscall_name}",
                )
        if tuple(int(family) for family in SECCOMP_ALLOWED_SOCKET_FAMILIES) != (1, 2, 10):
            raise VerifyError("sandbox_unavailable", "Host socket-family ABI is outside the audited policy")

        def add_socket_rule(syscall_number: int, operation: int, value: int) -> None:
            condition = (_ScmpArgCmp * 1)(_ScmpArgCmp(0, operation, value, 0))
            if syscall_number == -1 or library.seccomp_rule_add_array(
                context,
                deny_action,
                syscall_number,
                1,
                condition,
            ) != 0:
                raise VerifyError("sandbox_unavailable", "Cannot compile the socket-family seccomp allowlist")

        socket_number = library.seccomp_syscall_resolve_name(b"socket")
        add_socket_rule(socket_number, 2, 1)  # SCMP_CMP_LT AF_UNIX
        for denied_family in range(3, 10):
            add_socket_rule(socket_number, 4, denied_family)  # SCMP_CMP_EQ
        add_socket_rule(socket_number, 6, 10)  # SCMP_CMP_GT AF_INET6
        socketpair_number = library.seccomp_syscall_resolve_name(b"socketpair")
        add_socket_rule(socketpair_number, 1, 1)  # SCMP_CMP_NE AF_UNIX
        try:
            descriptor = os.memfd_create(
                "minimax-verifier-seccomp",
                os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
            )
        except (AttributeError, OSError) as exc:
            raise VerifyError("sandbox_unavailable", "Cannot create a private seccomp filter") from exc
        if library.seccomp_export_bpf(context, descriptor) != 0:
            raise VerifyError("sandbox_unavailable", "Cannot export the mandatory seccomp filter")
        size = os.lseek(descriptor, 0, os.SEEK_END)
        if size < 1 or size > 1_048_576 or size % 8 != 0:
            raise VerifyError("sandbox_unavailable", "Compiled seccomp filter has an invalid size")
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(
            descriptor,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
        )
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != (
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        ):
            raise VerifyError("sandbox_unavailable", "Compiled seccomp filter sealing is incomplete")
        return descriptor
    except VerifyError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise VerifyError("sandbox_unavailable", "Cannot build the mandatory seccomp filter") from exc
    finally:
        library.seccomp_release(context)
        if descriptor >= 0 and sys.exc_info()[0] is not None:
            os.close(descriptor)


def build_bwrap_argv(
    projection: Path,
    command: list[str],
    *,
    venv: VenvRuntime | None,
    tmpfs_bytes: int,
    seccomp_fd: int,
) -> list[str]:
    try:
        bwrap_metadata = BWRAP.lstat()
    except OSError as exc:
        raise VerifyError("sandbox_unavailable", "Fixed /usr/bin/bwrap is unavailable") from exc
    if (
        not stat.S_ISREG(bwrap_metadata.st_mode)
        or BWRAP.is_symlink()
        or bwrap_metadata.st_mode & 0o022
        or not os.access(BWRAP, os.X_OK)
    ):
        raise VerifyError("sandbox_unavailable", "Fixed /usr/bin/bwrap is unavailable or unsafe")
    argv = [
        str(BWRAP),
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--die-with-parent",
        "--new-session",
        "--seccomp",
        str(seccomp_fd),
        "--hostname",
        "minimax-verify",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    append_host_system_layout(argv)
    argv.extend(("--dir", "/etc", "--dir", "/etc/ssl"))
    for source, destination in (
        (Path("/etc/ssl/certs"), "/etc/ssl/certs"),
        (Path("/etc/ca-certificates"), "/etc/ca-certificates"),
    ):
        if source.is_dir() and not source.is_symlink():
            argv.extend(("--ro-bind", str(source), destination))
    argv.extend(("--proc", "/proc", "--dev", "/dev"))
    for masked_path in MASKED_PROC_FILES:
        argv.extend(("--ro-bind", "/dev/null", masked_path))
    argv.extend(
        (
            "--size",
            str(tmpfs_bytes),
            "--tmpfs",
            "/tmp",  # noqa: S108 - private sandbox tmpfs
            "--size",
            str(64 * 1024 * 1024),
            "--tmpfs",
            "/home",
            "--dir",
            "/home/sandbox",
            "--dir",
            "/home/sandbox/.cache",
            "--dir",
            "/home/sandbox/.config",
            "--dir",
            "/home/sandbox/.local",
            "--dir",
            "/home/sandbox/.local/state",
            "--ro-bind",
            str(projection),
            "/work",
        )
    )
    if venv is not None:
        argv.extend(
            (
                "--dir",
                "/runtime",
                "--ro-bind",
                str(venv.base_python_root),
                "/runtime/python",
                "--ro-bind",
                str(venv.skeleton),
                "/runtime/venv",
                "--ro-bind",
                str(venv.site_packages),
                f"/runtime/venv/lib/python{venv.python_version}/site-packages",
            )
        )
    argv.append("--clearenv")
    for name, value in sorted(sandbox_environment(venv).items()):
        argv.extend(("--setenv", name, value))
    argv.extend(("--chdir", "/work", "--", *command))
    return argv


def bounded_hard_limit(requested: int, hard: int, minimum: int, error_code: str) -> int:
    if hard == resource.RLIM_INFINITY:
        limit = requested
    elif isinstance(hard, int) and hard >= 0:
        limit = min(requested, hard)
    else:
        raise VerifyError(error_code, "Host resource hard limit is invalid")
    if limit < minimum:
        raise VerifyError(error_code, "Host resource hard limit cannot support isolated verification")
    return limit


def current_host_uid_tasks() -> int:
    """Read the aggregate systemd cgroup-v2 task count for this host UID.

    The pids controller counts kernel tasks (threads), unlike enumerating only
    numeric process directories in a PID-namespace-filtered /proc mount.
    """

    uid = os.getuid()
    expected_slice = f"/user.slice/user-{uid}.slice"
    try:
        controllers = read_regular(Path("/sys/fs/cgroup/cgroup.controllers"), 4096, "cgroup controllers")
        if b"pids" not in controllers.split():
            raise VerifyError(
                "sandbox_user_task_budget_unavailable",
                "The host cgroup does not expose the pids controller",
            )
        membership_data = read_regular(
            Path(f"/proc/{os.getpid()}/cgroup"),
            4096,
            "verifier cgroup membership",
        )
        membership_text = membership_data.decode("ascii", errors="strict")
        unified = [line[3:] for line in membership_text.splitlines() if line.startswith("0::")]
        if len(unified) != 1 or not (
            unified[0] == expected_slice or unified[0].startswith(f"{expected_slice}/")
        ):
            raise VerifyError(
                "sandbox_user_task_budget_unavailable",
                "Verifier is not inside the expected host UID cgroup slice",
            )
        slice_root = Path("/sys/fs/cgroup") / expected_slice.lstrip("/")
        verify_directory_components(slice_root, "host UID cgroup slice")
        counter_data = read_regular(slice_root / "pids.current", 64, "host UID task counter")
        counter_text = counter_data.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,8}", counter_text):
            raise VerifyError(
                "sandbox_user_task_budget_unavailable",
                "Host UID task counter is invalid",
            )
        return int(counter_text)
    except (OSError, UnicodeError, VerifyError) as exc:
        if isinstance(exc, VerifyError) and exc.code == "sandbox_user_task_budget_unavailable":
            raise
        raise VerifyError(
            "sandbox_user_task_budget_unavailable",
            "Cannot establish a reliable host UID task count",
        ) from exc


def calculate_user_task_limit(current_tasks: int, hard: int) -> int:
    if not isinstance(current_tasks, int) or isinstance(current_tasks, bool) or current_tasks < 1:
        raise VerifyError(
            "sandbox_user_task_budget_unavailable",
            "Host UID task count is invalid",
        )
    cap = bounded_hard_limit(
        MAX_SANDBOX_USER_TASKS,
        hard,
        2,
        "sandbox_user_task_budget_unavailable",
    )
    if current_tasks >= cap:
        raise VerifyError(
            "sandbox_user_task_budget_unavailable",
            "Host UID has no safe incremental task budget for verification",
        )
    return min(current_tasks + SANDBOX_USER_TASK_MARGIN, cap)


def sandbox_resource_limits(max_output_bytes: int, timeout_seconds: float) -> SandboxResourceLimits:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VerifyError("timeout_invalid", "Sandbox resource timeout must be finite and positive")
    try:
        _, address_space_hard = resource.getrlimit(resource.RLIMIT_AS)
        _, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
        _, file_size_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        _, nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        _, process_hard = resource.getrlimit(resource.RLIMIT_NPROC)
    except (OSError, ValueError) as exc:
        raise VerifyError(
            "sandbox_resource_limits_unavailable",
            "Cannot inspect required host resource limits",
        ) from exc
    return SandboxResourceLimits(
        address_space_bytes=bounded_hard_limit(
            MAX_SANDBOX_ADDRESS_SPACE_BYTES,
            address_space_hard,
            MIN_SANDBOX_ADDRESS_SPACE_BYTES,
            "sandbox_memory_budget_unavailable",
        ),
        cpu_seconds=bounded_hard_limit(
            max(1, int(timeout_seconds) + 2),
            cpu_hard,
            1,
            "sandbox_cpu_budget_unavailable",
        ),
        file_size_bytes=bounded_hard_limit(
            max_output_bytes,
            file_size_hard,
            1,
            "sandbox_file_budget_unavailable",
        ),
        open_files=bounded_hard_limit(
            MAX_SANDBOX_OPEN_FILES,
            nofile_hard,
            MIN_SANDBOX_OPEN_FILES,
            "sandbox_file_budget_unavailable",
        ),
        user_tasks=calculate_user_task_limit(current_host_uid_tasks(), process_hard),
    )


def _child_limits(limits: SandboxResourceLimits) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_AS,
        (limits.address_space_bytes, limits.address_space_bytes),
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.file_size_bytes, limits.file_size_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
    resource.setrlimit(resource.RLIMIT_NPROC, (limits.user_tasks, limits.user_tasks))


def run_sandbox_command(
    phase: str,
    projection: Path,
    command: list[str],
    *,
    venv: VenvRuntime | None,
    timeout_seconds: float,
    max_output_bytes: int,
    tmpfs_bytes: int,
    temporary: Path,
) -> CommandResult:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VerifyError("timeout_invalid", "Sandbox command timeout must be finite and positive")
    seccomp_fd = create_seccomp_policy_fd()
    try:
        bwrap_argv = build_bwrap_argv(
            projection,
            command,
            venv=venv,
            tmpfs_bytes=tmpfs_bytes,
            seccomp_fd=seccomp_fd,
        )
        limits = sandbox_resource_limits(max_output_bytes, timeout_seconds)
        output_path = temporary / f"{phase}-{time.monotonic_ns()}.log"
        started = time.monotonic()
        timed_out = False
        with output_path.open("w+b") as output:
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed Bubblewrap executable, direct argv only
                    bwrap_argv,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=clean_env(),
                    close_fds=True,
                    pass_fds=(seccomp_fd,),
                    start_new_session=True,
                    preexec_fn=lambda: _child_limits(limits),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise VerifyError(
                    "sandbox_resource_limits_unavailable",
                    "Cannot start Bubblewrap with all required resource limits and seccomp policy",
                ) from exc
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                returncode = process.wait(timeout=10.0)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(seccomp_fd)
    duration = time.monotonic() - started
    data = read_regular(output_path, max_output_bytes, f"{phase} sandbox output")
    output_limited = len(data) >= max_output_bytes or returncode == -signal.SIGXFSZ
    tail = data[-MAX_OUTPUT_TAIL_BYTES:].decode("utf-8", errors="replace")
    argv_hash = sha256_bytes(json.dumps(command, separators=(",", ":")).encode("utf-8"))
    return CommandResult(
        phase=phase,
        argv_sha256=argv_hash,
        returncode=returncode,
        duration_seconds=duration,
        timed_out=timed_out,
        output_limited=output_limited,
        output_bytes=len(data),
        output_sha256=sha256_bytes(data),
        output_tail=tail,
    )


SANDBOX_PREFLIGHT = (
    "import ctypes,errno,os,platform,socket,stat;from pathlib import Path;"
    "assert os.environ['HOME']=='/home/sandbox';"
    "blocked=('TOKEN','SECRET','PASSWORD','API_KEY','ALPACA','BROKER');"
    "assert not any(k for k in os.environ if any(x in k.upper() for x in blocked));"
    "interfaces={line.split(':',1)[0].strip() for line in "
    "Path('/proc/net/dev').read_text(encoding='ascii').splitlines()[2:] if ':' in line};"
    "assert interfaces=={'lo'};"
    "netlink_denied=False;"
    "\ntry:socket.socket(socket.AF_NETLINK,socket.SOCK_RAW)"
    "\nexcept OSError as e:netlink_denied=e.errno==errno.EPERM\n"
    "assert netlink_denied;"
    "null_stat=os.stat('/dev/null');"
    "assert all(stat.S_ISCHR(os.stat(p).st_mode) and os.stat(p).st_rdev==null_stat.st_rdev "
    "for p in ('/proc/keys','/proc/key-users'));"
    "keyctl_nr={'x86_64':250,'aarch64':219}.get(platform.machine());assert keyctl_nr is not None;"
    "libc=ctypes.CDLL(None,use_errno=True);ctypes.set_errno(0);"
    "assert libc.syscall(keyctl_nr,0,0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM;"
    "vsock_denied=False;"
    "\ntry:socket.socket(socket.AF_VSOCK,socket.SOCK_STREAM)"
    "\nexcept OSError as e:vsock_denied=e.errno==errno.EPERM\n"
    "assert vsock_denied;"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
    "ok=False;"
    "\ntry:s.connect(('198.51.100.1',9))"
    "\nexcept OSError as e:ok=e.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EPERM)\n"
    "assert ok;"
    "\ntry:open('/work/.minimax-verifier-write-probe','wb')"
    "\nexcept OSError:pass"
    "\nelse:raise AssertionError('work is writable')\n"
)


def verify_candidate(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    repo = resolve_repo(args.repo)
    validate_candidate_request(args.job_id, args.expect_patch_sha256)
    temporary_root = verifier_temp_root()
    temporary = Path(tempfile.mkdtemp(prefix="candidate-", dir=temporary_root))
    os.chmod(temporary, 0o700)
    frozen_worker: FrozenWorker | None = None
    try:
        frozen_worker = freeze_adjacent_worker(temporary)
        candidate = load_frozen_candidate(
            frozen_worker,
            repo,
            args.state_dir,
            args.job_id,
            args.expect_patch_sha256,
        )
        projection = temporary / "projection"
        projection.mkdir(mode=0o700)
        verify_source_head(repo, candidate.source_head_commit)
        materialize_tracked_head(repo, candidate.source_head_commit, projection)
        overlay_selected_snapshot(repo, candidate.files, projection)
        apply_frozen_patch(projection, candidate)
        focused = [parse_command_json(raw, "focused") for raw in args.focused_argv_json]
        release = [parse_command_json(raw, "release") for raw in args.release_argv_json]
        venv_enabled = args.venv is not None
        for index, command in enumerate(focused, start=1):
            validate_verification_command(command, f"focused-{index}", venv_enabled=venv_enabled)
        for index, command in enumerate(release, start=1):
            validate_verification_command(command, f"release-{index}", venv_enabled=venv_enabled)
        venv = prepare_venv_runtime(repo, args.venv, temporary)

        preflight = run_sandbox_command(
            "preflight",
            projection,
            [str(PYTHON), "-I", "-S", "-P", "-c", SANDBOX_PREFLIGHT],
            venv=None,
            timeout_seconds=15.0,
            max_output_bytes=64_000,
            tmpfs_bytes=min(args.tmpfs_bytes, 64 * 1024 * 1024),
            temporary=temporary,
        )
        if not preflight.passed:
            raise VerifyError(
                "sandbox_unavailable",
                f"Bubblewrap preflight failed closed: {preflight.output_tail.strip()[:500]}",
            )

        results: list[CommandResult] = []
        for index, command in enumerate(focused, start=1):
            result = run_sandbox_command(
                f"focused-{index}",
                projection,
                command,
                venv=venv,
                timeout_seconds=args.focused_timeout_seconds,
                max_output_bytes=args.max_output_bytes,
                tmpfs_bytes=args.tmpfs_bytes,
                temporary=temporary,
            )
            results.append(result)
            if not result.passed:
                return False, _verification_payload(candidate, results, "FOCUSED_FAILED")
        for index, command in enumerate(release, start=1):
            result = run_sandbox_command(
                f"release-{index}",
                projection,
                command,
                venv=venv,
                timeout_seconds=args.release_timeout_seconds,
                max_output_bytes=args.max_output_bytes,
                tmpfs_bytes=args.tmpfs_bytes,
                temporary=temporary,
            )
            results.append(result)
            if not result.passed:
                return False, _verification_payload(candidate, results, "RELEASE_FAILED")
        verify_source_head(repo, candidate.source_head_commit)
        verify_selected_snapshot(repo, candidate.files)
        return True, _verification_payload(candidate, results, "PASS")
    finally:
        if frozen_worker is not None:
            with contextlib.suppress(OSError):
                os.close(frozen_worker.fd)
        purge_verification_tree(temporary)


def purge_verification_tree(temporary: Path) -> None:
    root = verifier_temp_root()
    try:
        relative = temporary.relative_to(root)
    except ValueError as exc:
        raise VerifyError(
            "verification_cleanup_path_invalid",
            "Refusing to purge outside private verification root",
        ) from exc
    if len(relative.parts) != 1 or temporary == root:
        raise VerifyError(
            "verification_cleanup_path_invalid",
            "Only a direct child of the private verification root may be purged",
        )
    try:
        metadata = temporary.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VerifyError(
            "verification_cleanup_failed",
            f"Private verification state may be retained at {temporary}",
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or temporary.is_symlink():
        raise VerifyError(
            "verification_cleanup_failed",
            f"Private verification state has unsafe type and is retained at {temporary}",
        )
    try:
        shutil.rmtree(temporary)
    except OSError as exc:
        raise VerifyError(
            "verification_cleanup_failed",
            f"Private verification state is retained at {temporary}",
        ) from exc
    if temporary.exists() or temporary.is_symlink():
        raise VerifyError("verification_cleanup_failed", f"Private verification state is retained at {temporary}")


def _verification_payload(
    candidate: FrozenCandidate,
    results: list[CommandResult],
    outcome: str,
) -> dict[str, Any]:
    return {
        "ok": outcome == "PASS",
        "bridge_version": BRIDGE_VERSION,
        "verifier_version": VERSION,
        "job_id": candidate.job_id,
        "outcome": outcome,
        "patch_sha256": candidate.patch_sha256,
        "source_head_commit": candidate.source_head_commit,
        "source_snapshot_sha256": candidate.source_snapshot_sha256,
        "worker_sha256": candidate.worker_sha256,
        "sandbox": {
            "backend": "bubblewrap",
            "network": "new_namespace_loopback_only",
            "source_projection": "read_only",
            "home": "synthetic_tmpfs",
            "environment": "fixed_allowlist",
            "nested_user_namespaces": "disabled",
            "seccomp_policy_version": SECCOMP_POLICY_VERSION,
            "seccomp_policy_sha256": SECCOMP_POLICY_SHA256,
            "kernel_surfaces": "keyrings_high_risk_syscalls_and_non_ip_socket_families_denied",
        },
        "commands": [result.as_dict() for result in results],
        "real_checkout_modified": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimax-patch-verify",
        description="Codex-only Bubblewrap verifier for one frozen MiniMax patch.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-dir")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("job_id")
    verify.add_argument("--expect-patch-sha256", required=True)
    verify.add_argument("--venv", help="Repository-relative trusted read-only venv")
    verify.add_argument("--focused-argv-json", action="append", required=True)
    verify.add_argument("--release-argv-json", action="append", required=True)
    verify.add_argument("--focused-timeout-seconds", type=float, default=300.0)
    verify.add_argument("--release-timeout-seconds", type=float, default=1800.0)
    verify.add_argument("--max-output-bytes", type=int, default=MAX_OUTPUT_BYTES)
    verify.add_argument("--tmpfs-bytes", type=int, default=DEFAULT_TMPFS_BYTES)
    return parser


def validate_arguments(args: argparse.Namespace) -> None:
    if len(args.focused_argv_json) > MAX_COMMANDS_PER_PHASE or len(args.release_argv_json) > MAX_COMMANDS_PER_PHASE:
        raise VerifyError("command_count_limit", f"At most {MAX_COMMANDS_PER_PHASE} commands are allowed per phase")
    for name in ("focused_timeout_seconds", "release_timeout_seconds"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0 or value > 7200:
            raise VerifyError("timeout_invalid", f"{name} must be finite and in (0, 7200]")
    if args.max_output_bytes < 1024 or args.max_output_bytes > 10_000_000:
        raise VerifyError("output_limit_invalid", "--max-output-bytes must be between 1024 and 10000000")
    if args.tmpfs_bytes < 16 * 1024 * 1024 or args.tmpfs_bytes > MAX_TMPFS_BYTES:
        raise VerifyError("tmpfs_limit_invalid", "--tmpfs-bytes must be between 16 MiB and 2 GiB")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in raw
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
        validate_arguments(args)
        passed, payload = verify_candidate(args)
        emit(payload, as_json=args.json)
        return 0 if passed else 1
    except VerifyError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        if wants_json:
            emit(payload, as_json=True)
        else:
            print(f"error[{exc.code}]: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
