"""Observational caller for the executor-owned paper safe-flatten workflow.

The CLI owns no Alpaca client, credentials, order journal, kill switch or
broker mutation.  It durably records one operation identity, starts the
executor workflow at most once, and then performs read-only status polling.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_ERROR,
    PAPER_OK,
    paper_exit_code,
    read_json_artifact,
    redact_payload_json,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_executor_client import (
    PaperExecutorBrokerClient,
    PaperSafeFlattenStatus,
)
from trading_ai.execution.paper_executor_ipc import (
    PaperExecutorIpcError,
    PaperExecutorOutcomeUnknownError,
    PaperExecutorRemoteError,
)

SCHEMA_VERSION = "3.0"
OPERATION_INTENT_SCHEMA_VERSION = 1
DEFAULT_OUTPUT = "reports/tmp/paper_safe_flatten/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_safe_flatten/latest.md"
DEFAULT_POLL_ATTEMPTS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 0.5


class PaperSafeFlattenOperationalError(RuntimeError):
    """Raised before any executor mutation when the caller contract is unsafe."""


@dataclass(frozen=True)
class PaperSafeFlattenResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_safe_flatten(
    *,
    confirm_paper: bool,
    confirm_flatten: bool,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    reset_kill_switch_after: bool = False,
    as_of_date: str = "today",
    risk_state_path: str | Path = "reports/tmp/paper/risk_state.json",
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    order_journal_path: str | Path | None = None,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> PaperSafeFlattenResult:
    """Start once and observe the executor-owned emergency workflow."""

    del config, risk, as_of_date, risk_state_path, order_journal_path
    if not confirm_paper or not confirm_flatten:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_flatten:
            missing.append("--confirm-flatten")
        raise PaperSafeFlattenOperationalError(
            "paper safe flatten requires " + " and ".join(missing)
        )
    if reset_kill_switch_after:
        raise PaperSafeFlattenOperationalError(
            "paper safe flatten keeps the durable kill switch latched; "
            "--reset-kill-switch-after is forbidden"
        )
    if poll_attempts < 1:
        raise PaperSafeFlattenOperationalError("poll_attempts must be at least 1")
    if poll_interval_seconds < 0:
        raise PaperSafeFlattenOperationalError(
            "poll_interval_seconds must be non-negative"
        )

    output_path = Path(output)
    markdown_path = Path(markdown_output)
    intent_path = output_path.parent / "operation.json"
    client = PaperExecutorBrokerClient()
    operation_id: str | None = None
    status: PaperSafeFlattenStatus | None = None
    outcome_unknown: dict[str, object] | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None

    try:
        active = client.get_active_safe_flatten()
        if active is not None:
            operation_id = active.operation_id
            status = active
            _write_operation_intent(
                intent_path,
                operation_id=operation_id,
                phase="observing_active",
            )
        else:
            prepared = _read_operation_intent(intent_path)
            if prepared is not None:
                operation_id = prepared
                try:
                    status = client.get_safe_flatten_status(operation_id)
                except PaperExecutorRemoteError as exc:
                    if exc.code != "operation_not_found":
                        raise
            if operation_id is None or status is None:
                operation_id = operation_id or uuid.uuid4().hex
                _write_operation_intent(
                    intent_path,
                    operation_id=operation_id,
                    phase="prepared",
                )
                try:
                    status = client.start_safe_flatten(operation_id).status
                except PaperExecutorOutcomeUnknownError as exc:
                    outcome_unknown = _outcome_unknown_payload(exc)
                    failure_stage = "start_outcome_unknown"
                    try:
                        status = client.get_safe_flatten_status(operation_id)
                    except PaperExecutorIpcError as status_exc:
                        failure_reason = redact_secrets(str(status_exc))
                _write_operation_intent(
                    intent_path,
                    operation_id=operation_id,
                    phase=(
                        "observing"
                        if status is not None
                        else "start_outcome_unknown"
                    ),
                )

        if status is not None:
            for attempt in range(poll_attempts):
                if status.terminal:
                    break
                if (attempt > 0 or status.state != "latched") and attempt + 1 < poll_attempts:
                    sleep(poll_interval_seconds)
                try:
                    status = client.get_safe_flatten_status(operation_id)
                except PaperExecutorIpcError as exc:
                    failure_stage = "status_observation"
                    failure_reason = redact_secrets(str(exc))
                    break
    except PaperExecutorIpcError as exc:
        failure_stage = failure_stage or "executor_rpc"
        failure_reason = redact_secrets(str(exc))
    except (OSError, ValueError) as exc:
        failure_stage = failure_stage or "operation_intent"
        failure_reason = redact_secrets(str(exc))

    success = (
        status is not None
        and status.state == "flat_latched"
        and status.terminal
        and status.reconciled
        and status.kill_switch_active
    )
    if not success and failure_stage is None:
        failure_stage = "workflow_incomplete"
        failure_reason = (
            "safe_flatten_failed_latched"
            if status is not None and status.state == "failed_latched"
            else "safe_flatten_not_terminal"
        )
    result_status = PAPER_OK if success else PAPER_ERROR
    payload = redact_payload_json(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_timestamp(),
            "operation_id": operation_id,
            "status": result_status,
            "executor_state": None if status is None else status.state,
            "state_version": None if status is None else status.state_version,
            "terminal": False if status is None else status.terminal,
            "reconciled": False if status is None else status.reconciled,
            "kill_switch_active_after": (
                None if status is None else status.kill_switch_active
            ),
            "retry_allowed": False,
            "failure_stage": failure_stage,
            "failure_reason": failure_reason,
            "failure_code": None if status is None else status.failure_code,
            "outcome_unknown": (
                outcome_unknown
                if outcome_unknown is not None
                else _status_outcome_unknown(status)
            ),
            "started_at": None if status is None else status.started_at,
            "updated_at": None if status is None else status.updated_at,
        }
    )
    try:
        if operation_id is not None:
            _write_operation_intent(
                intent_path,
                operation_id=operation_id,
                phase=("flat_latched" if success else "observation_stopped"),
            )
        _write_result(payload, output_path=output_path, markdown_path=markdown_path)
    except Exception as exc:
        failed_payload = dict(payload)
        failed_payload.update(
            {
                "status": PAPER_ERROR,
                "failure_stage": "artifact_persistence",
                "failure_reason": redact_secrets(str(exc)),
            }
        )
        return PaperSafeFlattenResult(
            exit_code=paper_exit_code(PAPER_ERROR),
            status=PAPER_ERROR,
            output_path=output_path,
            markdown_path=markdown_path,
            payload=failed_payload,
        )
    return PaperSafeFlattenResult(
        exit_code=paper_exit_code(result_status),
        status=result_status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _write_operation_intent(
    path: Path,
    *,
    operation_id: str,
    phase: str,
) -> None:
    _canonical_operation_id(operation_id)
    if phase not in {
        "prepared",
        "observing",
        "observing_active",
        "start_outcome_unknown",
        "flat_latched",
        "observation_stopped",
    }:
        raise ValueError("safe-flatten operation phase is invalid")
    payload = json.dumps(
        {
            "schema_version": OPERATION_INTENT_SCHEMA_VERSION,
            "operation_id": operation_id,
            "phase": phase,
            "updated_at": _utc_timestamp(),
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("operation intent write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_operation_intent(path: Path) -> str | None:
    if not path.exists():
        return None
    payload = read_json_artifact(path)
    if set(payload) != {"schema_version", "operation_id", "phase", "updated_at"}:
        raise ValueError("safe-flatten operation intent fields are invalid")
    if payload["schema_version"] != OPERATION_INTENT_SCHEMA_VERSION:
        raise ValueError("safe-flatten operation intent schema is unsupported")
    operation_id = _canonical_operation_id(payload["operation_id"])
    if not isinstance(payload["phase"], str) or not isinstance(payload["updated_at"], str):
        raise ValueError("safe-flatten operation intent is invalid")
    return operation_id


def _canonical_operation_id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("safe-flatten operation id is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("safe-flatten operation id is invalid") from exc
    if parsed.int == 0 or value != parsed.hex:
        raise ValueError("safe-flatten operation id is invalid")
    return parsed.hex


def _outcome_unknown_payload(
    error: PaperExecutorOutcomeUnknownError,
) -> dict[str, object]:
    return {
        "request_id": error.request_id,
        "operation": error.operation,
        "phase": error.phase,
        "retry_allowed": False,
        "target_present": error.target is not None,
    }


def _status_outcome_unknown(
    status: PaperSafeFlattenStatus | None,
) -> dict[str, object] | None:
    if status is None or status.outcome_unknown is None:
        return None
    return {
        "request_id": status.outcome_unknown.request_id,
        "operation": status.outcome_unknown.operation,
        "phase": status.outcome_unknown.phase,
        "retry_allowed": False,
    }


def _write_result(
    payload: dict[str, object],
    *,
    output_path: Path,
    markdown_path: Path,
) -> None:
    write_json_artifact(payload, output_path)
    lines = [
        "# Paper Safe Flatten",
        "",
        f"- Status: **{payload['status']}**",
        f"- Executor state: `{payload['executor_state']}`",
        f"- Operation: `{payload['operation_id']}`",
        f"- Reconciled: `{payload['reconciled']}`",
        f"- Kill switch active: `{payload['kill_switch_active_after']}`",
        f"- Retry allowed: `{payload['retry_allowed']}`",
    ]
    if payload.get("failure_stage"):
        lines.append(f"- Failure stage: `{payload['failure_stage']}`")
    write_text_artifact("\n".join(lines) + "\n", markdown_path)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
