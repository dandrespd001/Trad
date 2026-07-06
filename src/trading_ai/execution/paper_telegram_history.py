"""Offline Telegram history message for paper trading operations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_telegram_history/latest.json"


class PaperTelegramHistoryOperationalError(RuntimeError):
    """Raised when the Telegram history artifact cannot be produced."""


@dataclass(frozen=True)
class PaperTelegramHistoryResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def run_paper_telegram_history(
    *,
    as_of_date: str,
    performance: str | Path | None = None,
    weekly_summary: str | Path | None = None,
    ledger_inputs: Iterable[str | Path] = (),
    max_events: int = 5,
    output: str | Path = DEFAULT_OUTPUT,
    generated_at: str | None = None,
) -> PaperTelegramHistoryResult:
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    generated = generated_at or datetime.now(UTC).isoformat()
    blockers: list[str] = []
    source_paths = {
        "performance": str(Path(performance)) if performance is not None else None,
        "weekly_summary": str(Path(weekly_summary)) if weekly_summary is not None else None,
        "ledger_inputs": [str(Path(path)) for path in ledger_inputs],
    }
    performance_payload = _read_source("performance", performance, blockers)
    weekly_payload = _read_source("weekly_summary", weekly_summary, blockers)
    ledger = _ledger_summary([Path(path) for path in ledger_inputs], max_events=max_events, blockers=blockers)
    sections = {
        "performance": _performance_summary(performance_payload),
        "weekly": _weekly_summary(weekly_payload),
        "ledger": ledger,
    }
    status = PAPER_BLOCKED if blockers else _overall_status(sections)
    message = _render_message(as_of_date=as_of_date, status=status, sections=sections, blockers=blockers)
    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "sources": source_paths,
        "sections": sections,
        "message": message,
        "telegram": {
            "send_enabled": False,
            "sent": False,
            "message_length": len(message),
            "parse_mode": "plain_text",
        },
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    redacted = _redact_payload(payload_out)
    output_path = Path(output)
    write_json_artifact(redacted, output_path)
    return PaperTelegramHistoryResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=redacted,
    )


def _read_source(
    name: str,
    path: str | Path | None,
    blockers: list[str],
) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append(f"{name}_invalid")
        return {"status": PAPER_ERROR, "reason": redact_secrets(str(exc))}
    blockers.extend(_source_safety_blockers(name, payload))
    return payload


def _source_safety_blockers(name: str, payload: Mapping[str, object]) -> list[str]:
    safety = _mapping(payload.get("safety"))
    blockers: list[str] = []
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append(f"{name}_live_trading_flag")
    if safety.get("orders_submitted") is True:
        blockers.append(f"{name}_orders_submitted")
    if safety.get("credentials_read") is True:
        blockers.append(f"{name}_credentials_read")
    return blockers


def _performance_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Performance: missing"}
    metrics = _mapping(payload.get("paper_metrics"))
    pnl = _mapping(metrics.get("pnl"))
    status = str(payload.get("status") or "UNKNOWN")
    sessions = _int_value(metrics.get("complete_sessions"))
    fills = _int_value(metrics.get("fills"))
    realized = _float_or_none(pnl.get("realized_pnl"))
    line = f"Performance: {status} sessions={sessions} fills={fills} PnL={_format_optional(realized)}"
    return {
        "present": True,
        "status": status,
        "complete_sessions": sessions,
        "fills": fills,
        "realized_pnl": realized,
        "line": line,
    }


def _weekly_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Weekly: missing"}
    decisions = _mapping(payload.get("decisions"))
    counts = _mapping(decisions.get("counts"))
    ledger = _mapping(payload.get("ledger"))
    status = str(payload.get("status") or "UNKNOWN")
    continue_count = _int_value(counts.get("CONTINUE"))
    review_count = _int_value(counts.get("REVIEW"))
    stop_count = _int_value(counts.get("STOP"))
    fills = _int_value(ledger.get("fills"))
    return {
        "present": True,
        "status": status,
        "week": str(payload.get("week") or ""),
        "continue_count": continue_count,
        "review_count": review_count,
        "stop_count": stop_count,
        "fills": fills,
        "line": f"Weekly: {status} CONTINUE={continue_count} REVIEW={review_count} STOP={stop_count} fills={fills}",
    }


def _ledger_summary(paths: list[Path], *, max_events: int, blockers: list[str]) -> dict[str, object]:
    events: list[dict[str, object]] = []
    event_count = 0
    executions = 0
    closeouts = 0
    pending = 0
    unmatched = 0
    for path in paths:
        if not path.exists():
            blockers.append("ledger_missing")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            blockers.append("ledger_invalid")
            events.append({"event_type": "ledger_error", "status": "ERROR", "reason": redact_secrets(str(exc))})
            continue
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                blockers.append("ledger_invalid_json")
                continue
            if not isinstance(payload, Mapping):
                blockers.append("ledger_invalid_event")
                continue
            event_count += 1
            blockers.extend(_source_safety_blockers("ledger", payload))
            event_type = str(payload.get("event_type") or payload.get("record_type") or "unknown")
            status = str(payload.get("status") or payload.get("state") or payload.get("order_state") or "UNKNOWN")
            if event_type == "paper_execution":
                executions += 1
            if event_type == "paper_closeout":
                closeouts += 1
                if status.upper() == "PENDING":
                    pending += 1
                elif status.upper() == "UNMATCHED":
                    unmatched += 1
            events.append(
                {
                    "path": str(path),
                    "line_number": line_number,
                    "event_type": event_type,
                    "status": status,
                    "symbol": str(payload.get("symbol") or "").upper(),
                    "generated_at": str(payload.get("generated_at") or payload.get("as_of_date") or ""),
                    "line": _event_line(event_type, status, payload),
                }
            )
    recent = sorted(events, key=lambda item: str(item.get("generated_at") or ""), reverse=True)[:max_events]
    line = (
        f"Ledger: events={event_count} executions={executions} closeouts={closeouts} "
        f"pending={pending} unmatched={unmatched}"
    )
    return {
        "present": bool(paths),
        "status": PAPER_OK if not blockers else PAPER_WARN,
        "event_count": event_count,
        "executions": executions,
        "closeouts": closeouts,
        "pending": pending,
        "unmatched": unmatched,
        "recent_events": recent,
        "line": line,
    }


def _event_line(event_type: str, status: str, payload: Mapping[str, object]) -> str:
    symbol = str(payload.get("symbol") or "").upper()
    side = str(payload.get("side") or "").lower()
    parts = [event_type, status]
    if symbol:
        parts.append(symbol)
    if side:
        parts.append(side)
    return " ".join(parts)


def _overall_status(sections: Mapping[str, Mapping[str, object]]) -> str:
    statuses = {str(section.get("status") or "MISSING").upper() for section in sections.values()}
    if PAPER_ERROR in statuses:
        return PAPER_ERROR
    if "CRITICAL" in statuses or PAPER_WARN in statuses or "MISSING" in statuses:
        return PAPER_WARN
    return PAPER_OK


def _render_message(
    *,
    as_of_date: str,
    status: str,
    sections: Mapping[str, Mapping[str, object]],
    blockers: list[str],
) -> str:
    lines = [
        f"Paper history {as_of_date}",
        f"Status: {status}",
        str(sections["performance"].get("line") or ""),
        str(sections["weekly"].get("line") or ""),
        str(sections["ledger"].get("line") or ""),
    ]
    recent = sections["ledger"].get("recent_events")
    if isinstance(recent, list) and recent:
        lines.append("Recent: " + "; ".join(str(_mapping(event).get("line") or "") for event in recent))
    if blockers:
        lines.append("Blockers: " + ", ".join(blockers))
    lines.append("Safety: paper-only, no Telegram send, no orders submitted")
    return "\n".join(line for line in lines if line)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _format_optional(value: object) -> str:
    number = _float_or_none(value)
    return "n/a" if number is None else f"{number:.2f}"


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(redact_secrets(json.dumps(dict(payload), sort_keys=True)))
