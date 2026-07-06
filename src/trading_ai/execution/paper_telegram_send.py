"""Send or dry-run local paper Telegram artifacts."""

from __future__ import annotations

from collections.abc import Mapping
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
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
)
from trading_ai.execution.paper_monitor import TELEGRAM_MESSAGE_LIMIT, send_paper_monitor_telegram

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_telegram_send/latest.json"
SENDABLE_SOURCE_STATUSES = {PAPER_OK, PAPER_WARN, PAPER_CRITICAL}


class PaperTelegramSendOperationalError(RuntimeError):
    """Raised when a local Telegram artifact cannot be sent or previewed."""


@dataclass(frozen=True)
class PaperTelegramSendResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def run_paper_telegram_send(
    *,
    as_of_date: str,
    artifact: str | Path,
    output: str | Path = DEFAULT_OUTPUT,
    send_telegram: bool = False,
    telegram_dry_run: bool = False,
    generated_at: str | None = None,
    env: Mapping[str, str] | None = None,
) -> PaperTelegramSendResult:
    generated = generated_at or datetime.now(UTC).isoformat()
    artifact_path = Path(artifact)
    payload = read_json_artifact(artifact_path)
    message = str(payload.get("message") or "").strip()
    source_status = str(payload.get("status") or "UNKNOWN").upper()
    blockers = _source_blockers(payload, as_of_date=as_of_date, message=message, source_status=source_status)

    if blockers:
        status = PAPER_BLOCKED
        telegram = {
            "send_enabled": False,
            "dry_run": False,
            "sent": False,
            "status": "BLOCKED",
            "reason": "source_blocked",
            "message_length": len(message),
            "parse_mode": _parse_mode(payload),
        }
    elif not send_telegram or telegram_dry_run:
        status = source_status if source_status in SENDABLE_SOURCE_STATUSES else PAPER_OK
        telegram = {
            "send_enabled": send_telegram,
            "dry_run": True,
            "sent": False,
            "status": "DRY_RUN",
            "reason": "telegram_dry_run" if send_telegram else "send_telegram_not_requested",
            "message_length": len(message),
            "parse_mode": _parse_mode(payload),
        }
    else:
        result = send_paper_monitor_telegram(message[:TELEGRAM_MESSAGE_LIMIT], env=env)
        if result.sent:
            status = source_status if source_status in SENDABLE_SOURCE_STATUSES else PAPER_OK
        else:
            status = PAPER_ERROR
        telegram = {
            "send_enabled": True,
            "dry_run": False,
            "sent": result.sent,
            "status": result.status,
            "message_length": len(message[:TELEGRAM_MESSAGE_LIMIT]),
            "parse_mode": _parse_mode(payload),
            "credentials_read": True,
        }
        if result.reason:
            telegram["reason"] = redact_secrets(result.reason, env=env)
        if result.http_status is not None:
            telegram["http_status"] = result.http_status

    output_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "source": {
            "artifact_path": str(artifact_path),
            "artifact_status": source_status,
            "artifact_as_of_date": payload.get("as_of_date"),
            "schema_version": payload.get("schema_version"),
        },
        "telegram": telegram,
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": send_telegram and not telegram_dry_run and not blockers,
            "telegram_credentials_read": send_telegram and not telegram_dry_run and not blockers,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    output_path = Path(output)
    write_json_artifact(output_payload, output_path)
    return PaperTelegramSendResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=output_payload,
    )


def _source_blockers(
    payload: Mapping[str, object],
    *,
    as_of_date: str,
    message: str,
    source_status: str,
) -> list[str]:
    blockers: list[str] = []
    if not message:
        blockers.append("source_message_missing")
    source_date = payload.get("as_of_date")
    if source_date and str(source_date) != as_of_date:
        blockers.append("source_as_of_date_mismatch")
    if source_status not in SENDABLE_SOURCE_STATUSES:
        blockers.append(f"source_status_{source_status.lower()}")
    blockers.extend(f"source_{item}" for item in _reason_list(payload.get("blockers")))

    safety = _mapping(payload.get("safety"))
    if safety.get("paper_only") is not True:
        blockers.append("source_not_paper_only")
    if safety.get("broker_client_built") is True:
        blockers.append("source_broker_client_built")
    if safety.get("credentials_read") is True:
        blockers.append("source_credentials_read")
    if safety.get("orders_submitted") is True:
        blockers.append("source_orders_submitted")
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append("source_live_trading_flag")
    return sorted(set(blockers))


def _parse_mode(payload: Mapping[str, object]) -> str:
    telegram = _mapping(payload.get("telegram"))
    return str(telegram.get("parse_mode") or "plain_text")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _reason_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
