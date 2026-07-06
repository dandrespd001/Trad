"""Batch notification gate for local paper Telegram artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_CRITICAL,
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
)
from trading_ai.execution.paper_telegram_send import PaperTelegramSendResult, run_paper_telegram_send

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_telegram_notify/latest.json"
DEFAULT_SEND_OUTPUT_DIR = "reports/tmp/paper_telegram_send"


class PaperTelegramNotifyOperationalError(RuntimeError):
    """Raised when the batch Telegram notification report cannot be produced."""


@dataclass(frozen=True)
class PaperTelegramNotifyResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def run_paper_telegram_notify(
    *,
    as_of_date: str,
    artifacts: Iterable[str | Path],
    output: str | Path = DEFAULT_OUTPUT,
    send_output_dir: str | Path = DEFAULT_SEND_OUTPUT_DIR,
    ledger_output: str | Path | None = None,
    send_telegram: bool = False,
    telegram_dry_run: bool = False,
    generated_at: str | None = None,
    env: Mapping[str, str] | None = None,
) -> PaperTelegramNotifyResult:
    artifact_paths = [Path(path) for path in artifacts]
    if not artifact_paths:
        raise PaperTelegramNotifyOperationalError("at least one --artifact is required")
    generated = generated_at or datetime.now(UTC).isoformat()
    send_root = Path(send_output_dir) / as_of_date

    preflight = [
        _send_or_error(
            as_of_date=as_of_date,
            artifact=artifact,
            output=send_root / f"message_{index:03d}.json",
            send_telegram=send_telegram,
            telegram_dry_run=True if send_telegram else False,
            generated_at=generated,
            env=env,
        )
        for index, artifact in enumerate(artifact_paths, start=1)
    ]
    preflight_notifications = [_notification_from_result(result) for result in preflight]
    preflight_blocked = any(
        str(notification.get("status") or "").upper() in {PAPER_BLOCKED, PAPER_ERROR}
        for notification in preflight_notifications
    )

    if send_telegram and not telegram_dry_run and not preflight_blocked:
        results = [
            _send_or_error(
                as_of_date=as_of_date,
                artifact=artifact,
                output=send_root / f"message_{index:03d}.json",
                send_telegram=True,
                telegram_dry_run=False,
                generated_at=generated,
                env=env,
            )
            for index, artifact in enumerate(artifact_paths, start=1)
        ]
        notifications = [_notification_from_result(result) for result in results]
    else:
        notifications = preflight_notifications

    blockers = _batch_blockers(notifications)
    status = _status_from_notifications(notifications)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "source": {
            "artifacts": [str(path) for path in artifact_paths],
            "send_output_dir": str(Path(send_output_dir)),
            "ledger_output": str(Path(ledger_output)) if ledger_output is not None else None,
        },
        "summary": _summary(notifications),
        "notifications": notifications,
        "blockers": blockers,
        "safety": _safety(notifications),
    }
    output_path = Path(output)
    write_json_artifact(payload, output_path)
    if ledger_output is not None:
        _append_ledger(ledger_output, payload, output_path=output_path)
    return PaperTelegramNotifyResult(paper_exit_code(status), status, output_path, payload)


def _send_or_error(
    *,
    as_of_date: str,
    artifact: Path,
    output: Path,
    send_telegram: bool,
    telegram_dry_run: bool,
    generated_at: str,
    env: Mapping[str, str] | None,
) -> PaperTelegramSendResult:
    try:
        return run_paper_telegram_send(
            as_of_date=as_of_date,
            artifact=artifact,
            output=output,
            send_telegram=send_telegram,
            telegram_dry_run=telegram_dry_run,
            generated_at=generated_at,
            env=env,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": PAPER_ERROR,
            "source": {
                "artifact_path": str(artifact),
                "artifact_status": "ERROR",
                "artifact_as_of_date": None,
            },
            "telegram": {
                "send_enabled": False,
                "dry_run": False,
                "sent": False,
                "status": PAPER_ERROR,
                "reason": redact_secrets(str(exc), env=env),
                "message_length": 0,
                "parse_mode": "plain_text",
            },
            "blockers": ["artifact_unreadable"],
            "safety": _base_safety(),
        }
        write_json_artifact(payload, output)
        return PaperTelegramSendResult(2, PAPER_ERROR, output, payload)


def _notification_from_result(result: PaperTelegramSendResult) -> dict[str, object]:
    payload = result.payload
    source = _mapping(payload.get("source"))
    telegram = _mapping(payload.get("telegram"))
    return {
        "artifact_path": str(source.get("artifact_path") or ""),
        "artifact_status": str(source.get("artifact_status") or "UNKNOWN"),
        "artifact_as_of_date": source.get("artifact_as_of_date"),
        "send_report_path": str(result.output_path),
        "status": result.status,
        "exit_code": result.exit_code,
        "telegram_status": str(telegram.get("status") or "UNKNOWN"),
        "send_enabled": telegram.get("send_enabled") is True,
        "dry_run": telegram.get("dry_run") is True,
        "sent": telegram.get("sent") is True,
        "message_length": _int_value(telegram.get("message_length")),
        "blockers": _string_list(payload.get("blockers")),
        "safety": dict(_mapping(payload.get("safety"))),
    }


def _summary(notifications: list[Mapping[str, object]]) -> dict[str, object]:
    return {
        "artifact_count": len(notifications),
        "sent_count": sum(1 for item in notifications if item.get("sent") is True),
        "dry_run_count": sum(1 for item in notifications if item.get("dry_run") is True),
        "blocked_count": sum(1 for item in notifications if str(item.get("status") or "").upper() == PAPER_BLOCKED),
        "error_count": sum(1 for item in notifications if str(item.get("status") or "").upper() == PAPER_ERROR),
    }


def _batch_blockers(notifications: list[Mapping[str, object]]) -> list[str]:
    blockers: list[str] = []
    if any(str(item.get("status") or "").upper() == PAPER_BLOCKED for item in notifications):
        blockers.append("artifact_blocked")
    if any(str(item.get("status") or "").upper() == PAPER_ERROR for item in notifications):
        blockers.append("artifact_error")
    if any(item.get("sent") is True for item in notifications) and any(
        str(item.get("status") or "").upper() == PAPER_ERROR for item in notifications
    ):
        blockers.append("partial_telegram_send_failure")
    return blockers


def _status_from_notifications(notifications: list[Mapping[str, object]]) -> str:
    statuses = {str(item.get("status") or "UNKNOWN").upper() for item in notifications}
    if PAPER_ERROR in statuses:
        return PAPER_ERROR
    if PAPER_BLOCKED in statuses:
        return PAPER_BLOCKED
    if PAPER_CRITICAL in statuses:
        return PAPER_CRITICAL
    if PAPER_WARN in statuses:
        return PAPER_WARN
    return PAPER_OK


def _safety(notifications: list[Mapping[str, object]]) -> dict[str, object]:
    child_safety = [_mapping(item.get("safety")) for item in notifications]
    return {
        "paper_only": all(safety.get("paper_only") is True for safety in child_safety),
        "broker_client_built": any(safety.get("broker_client_built") is True for safety in child_safety),
        "credentials_read": any(safety.get("credentials_read") is True for safety in child_safety),
        "telegram_credentials_read": any(
            safety.get("telegram_credentials_read") is True for safety in child_safety
        ),
        "orders_submitted": any(safety.get("orders_submitted") is True for safety in child_safety),
        "live_trading_authorized": any(
            safety.get("live_trading_authorized") is True for safety in child_safety
        ),
        "live_trading_allowed": any(safety.get("live_trading_allowed") is True for safety in child_safety),
    }


def _base_safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "telegram_credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def _append_ledger(path: str | Path, payload: Mapping[str, object], *, output_path: Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "record_type": "paper_telegram_notify",
        "as_of_date": payload.get("as_of_date"),
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "blockers": payload.get("blockers"),
        "output_path": str(output_path),
        "generated_at": payload.get("generated_at"),
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _int_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0
