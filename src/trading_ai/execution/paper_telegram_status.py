"""Offline Telegram status message for paper trading operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
DEFAULT_OUTPUT = "reports/tmp/paper_telegram_status/latest.json"


class PaperTelegramStatusOperationalError(RuntimeError):
    """Raised when the Telegram status artifact cannot be produced."""


@dataclass(frozen=True)
class PaperTelegramStatusResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def run_paper_telegram_status(
    *,
    as_of_date: str,
    performance: str | Path | None = None,
    position_watch: str | Path | None = None,
    forecast_report: str | Path | None = None,
    signal_plan: str | Path | None = None,
    eod_position_plan: str | Path | None = None,
    operator_status: str | Path | None = None,
    output: str | Path = DEFAULT_OUTPUT,
    generated_at: str | None = None,
) -> PaperTelegramStatusResult:
    generated = generated_at or datetime.now(UTC).isoformat()
    sources = {
        "performance": performance,
        "position_watch": position_watch,
        "forecast_report": forecast_report,
        "signal_plan": signal_plan,
        "eod_position_plan": eod_position_plan,
        "operator_status": operator_status,
    }
    loaded: dict[str, dict[str, object]] = {}
    blockers: list[str] = []
    source_paths: dict[str, str | None] = {}
    for name, path in sources.items():
        source_paths[name] = str(Path(path)) if path is not None else None
        if path is None:
            continue
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(f"{name}_invalid")
            loaded[name] = {"status": PAPER_ERROR, "reason": redact_secrets(str(exc))}
            continue
        loaded[name] = payload
        blockers.extend(_source_safety_blockers(name, payload, as_of_date=as_of_date))

    sections = {
        "performance": _performance_summary(loaded.get("performance")),
        "positions": _position_summary(loaded.get("position_watch")),
        "forecast": _forecast_summary(loaded.get("forecast_report")),
        "signal": _signal_plan_summary(loaded.get("signal_plan")),
        "eod": _eod_summary(loaded.get("eod_position_plan")),
        "operator": _operator_summary(loaded.get("operator_status")),
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
    return PaperTelegramStatusResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=redacted,
    )


def _source_safety_blockers(name: str, payload: Mapping[str, object], *, as_of_date: str) -> list[str]:
    safety = _mapping(payload.get("safety"))
    blockers: list[str] = []
    source_date = str(payload.get("as_of_date") or "")
    if source_date and source_date != as_of_date:
        blockers.append(f"{name}_stale")
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
    realized = pnl.get("realized_pnl")
    line = (
        f"Performance: {payload.get('status') or 'UNKNOWN'} | "
        f"Complete sessions: {_int_value(metrics.get('complete_sessions'))} | "
        f"Fills: {_int_value(metrics.get('fills'))} | "
        f"PnL: {_format_optional(realized)}"
    )
    return {
        "present": True,
        "status": str(payload.get("status") or "UNKNOWN"),
        "complete_sessions": _int_value(metrics.get("complete_sessions")),
        "fills": _int_value(metrics.get("fills")),
        "pending_closeouts": _int_value(metrics.get("pending_closeouts")),
        "unmatched_closeouts": _int_value(metrics.get("unmatched_closeouts")),
        "realized_pnl": _float_or_none(realized),
        "line": line,
    }


def _position_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Open positions: missing"}
    positions = _open_positions(payload.get("positions"))
    protective_summary = _mapping(_mapping(payload.get("protective_order_plan")).get("summary"))
    protective_review_count = _int_value(protective_summary.get("review_count"))
    if not positions:
        line = "Open positions: none"
    else:
        line = "Open positions: " + ", ".join(
            f"{row['symbol']} {_format_quantity(row['quantity'])}" for row in positions
        )
    if protective_review_count > 0:
        line += f" | protective_reviews={protective_review_count}"
    status = str(payload.get("status") or "UNKNOWN")
    if protective_review_count > 0 and status.upper() == PAPER_OK:
        status = PAPER_WARN
    return {
        "present": True,
        "status": status,
        "open_position_count": len(positions),
        "protective_review_count": protective_review_count,
        "positions": positions,
        "line": line,
    }


def _forecast_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Forecast: missing"}
    status = str(payload.get("status") or "UNKNOWN")
    model_id = str(payload.get("model_id") or "")
    row_count = _int_value(payload.get("row_count"))
    return {
        "present": True,
        "status": status,
        "model_id": model_id,
        "row_count": row_count,
        "line": f"Forecast: {status} {model_id} rows={row_count}",
    }


def _signal_plan_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Signal: missing"}
    decision = str(payload.get("decision") or payload.get("status") or "UNKNOWN")
    selected = _mapping(payload.get("selected_signal"))
    proposal = _mapping(payload.get("selected_llm_proposal"))
    symbol = str(payload.get("selected_symbol") or selected.get("symbol") or proposal.get("symbol") or "").upper()
    action = str(selected.get("action") or proposal.get("action") or "").lower()
    probability = _float_or_none(selected.get("probability"))
    confidence = _float_or_none(proposal.get("confidence"))
    probability_text = f" p={probability:.2f}" if probability is not None else ""
    confidence_text = f" llm={confidence:.2f}" if confidence is not None else ""
    symbol_action = " ".join(part for part in (symbol, action) if part)
    line = f"Signal: {decision}"
    if symbol_action:
        line += f" {symbol_action}"
    line += probability_text + confidence_text
    return {
        "present": True,
        "status": decision,
        "decision": decision,
        "eligible_for_paper": payload.get("eligible_for_paper") is True,
        "selected_symbol": symbol,
        "selected_action": action,
        "probability": probability,
        "llm_confidence": confidence,
        "line": line,
    }


def _eod_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "EOD: missing"}
    summary = _mapping(payload.get("summary"))
    status = str(payload.get("status") or "UNKNOWN")
    close_required = _int_value(summary.get("close_required_count"))
    longer_term = _int_value(summary.get("longer_term_hold_count"))
    return {
        "present": True,
        "status": status,
        "close_required_count": close_required,
        "longer_term_hold_count": longer_term,
        "line": f"EOD: {status} close_required={close_required} longer_term={longer_term}",
    }


def _operator_summary(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {"present": False, "status": "MISSING", "line": "Operator: missing"}
    status = str(payload.get("status") or "UNKNOWN")
    return {
        "present": True,
        "status": status,
        "clean_for_paper_auto": payload.get("clean_for_paper_auto") is True,
        "line": f"Operator: {status} clean={payload.get('clean_for_paper_auto') is True}",
    }


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
        f"Paper trading {as_of_date}",
        f"Status: {status}",
        str(sections["performance"].get("line") or ""),
        str(sections["positions"].get("line") or ""),
        str(sections["forecast"].get("line") or ""),
        str(sections["signal"].get("line") or ""),
        str(sections["eod"].get("line") or ""),
        str(sections["operator"].get("line") or ""),
    ]
    if blockers:
        lines.append("Blockers: " + ", ".join(blockers))
    lines.append("Safety: paper-only, no orders submitted")
    return "\n".join(line for line in lines if line)


def _open_positions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    positions: list[dict[str, object]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        quantity = _float_or_none(row.get("quantity", row.get("qty")))
        if not symbol or quantity is None or quantity <= 0:
            continue
        positions.append({"symbol": symbol, "quantity": quantity})
    return positions


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


def _format_quantity(value: object) -> str:
    number = _float_or_none(value)
    if number is None:
        return "0"
    return f"{number:g}"


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(redact_secrets(json.dumps(dict(payload), sort_keys=True)))
