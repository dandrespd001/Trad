"""Read-only cross-asset session close plan for paper operations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_CRITICAL,
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    read_json_artifact,
    redact_payload,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/cross_asset_session_plan/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/cross_asset_session_plan/latest.md"
DEFAULT_FUTURES_READINESS = "reports/tmp/futures_readiness/latest.json"
DEFAULT_FOREX_READINESS = "reports/tmp/forex_readiness/latest.json"


class CrossAssetSessionPlanOperationalError(RuntimeError):
    """Raised when the cross-asset session plan cannot be produced."""


@dataclass(frozen=True)
class CrossAssetSessionPlanResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_cross_asset_session_plan(
    *,
    as_of_date: str,
    positions: str | Path,
    current_time: str,
    futures_readiness: str | Path | None = DEFAULT_FUTURES_READINESS,
    forex_readiness: str | Path | None = DEFAULT_FOREX_READINESS,
    futures_session_close_time: str = "17:00",
    forex_weekend_close_time: str = "21:00",
    flatten_window_minutes: int = 30,
    longer_term_symbols: Iterable[str] = (),
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    ledger_output: str | Path | None = None,
    generated_at: str | None = None,
) -> CrossAssetSessionPlanResult:
    if flatten_window_minutes < 1:
        raise CrossAssetSessionPlanOperationalError("--flatten-window-minutes must be at least 1")
    generated = generated_at or datetime.now(UTC).isoformat()
    positions_path = Path(positions)
    try:
        position_payload = read_json_artifact(positions_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CrossAssetSessionPlanOperationalError(f"cannot read positions artifact: {exc}") from exc

    futures_payload = _read_optional_json(futures_readiness)
    forex_payload = _read_optional_json(forex_readiness)
    report = build_cross_asset_session_plan(
        as_of_date=as_of_date,
        positions=position_payload,
        positions_path=positions_path,
        current_time=current_time,
        futures_readiness=futures_payload,
        futures_readiness_path=futures_readiness,
        forex_readiness=forex_payload,
        forex_readiness_path=forex_readiness,
        futures_session_close_time=futures_session_close_time,
        forex_weekend_close_time=forex_weekend_close_time,
        flatten_window_minutes=flatten_window_minutes,
        longer_term_symbols=longer_term_symbols,
        generated_at=generated,
    )
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_json_artifact(report, output_path)
    write_text_artifact(render_cross_asset_session_plan_markdown(report), markdown_path)
    if ledger_output is not None:
        _append_ledger(ledger_output, report, output_path=output_path)
    status = str(report.get("status") or PAPER_ERROR)
    return CrossAssetSessionPlanResult(
        paper_exit_code(status),
        status,
        output_path,
        markdown_path,
        report,
    )


def build_cross_asset_session_plan(
    *,
    as_of_date: str,
    positions: Mapping[str, object],
    positions_path: str | Path,
    current_time: str,
    futures_readiness: Mapping[str, object] | None,
    futures_readiness_path: str | Path | None,
    forex_readiness: Mapping[str, object] | None,
    forex_readiness_path: str | Path | None,
    futures_session_close_time: str,
    forex_weekend_close_time: str,
    flatten_window_minutes: int,
    longer_term_symbols: Iterable[str],
    generated_at: str,
) -> dict[str, object]:
    current_minutes = _parse_hhmm(current_time, field_name="current_time")
    futures_close_minutes = _parse_hhmm(futures_session_close_time, field_name="futures_session_close_time")
    forex_close_minutes = _parse_hhmm(forex_weekend_close_time, field_name="forex_weekend_close_time")
    as_date = date.fromisoformat(as_of_date)
    is_friday = as_date.weekday() == 4
    longer_term = _clean_symbols(longer_term_symbols)
    futures_symbols = _ready_symbols(futures_readiness, key="contracts")
    forex_symbols = _ready_symbols(forex_readiness, key="pairs")
    blockers = [
        *_source_safety_blockers("position_snapshot", _mapping(positions.get("safety"))),
        *_readiness_safety_blockers("futures_readiness", futures_readiness),
        *_readiness_safety_blockers("forex_readiness", forex_readiness),
    ]
    normalized_positions = _open_positions(positions.get("positions"))
    actions: list[dict[str, object]] = []
    close_required_count = 0
    longer_term_count = 0
    review_count = 0
    watch_count = 0

    for position in normalized_positions:
        symbol = str(position["symbol"])
        asset_class = _asset_class(symbol, futures_symbols=futures_symbols, forex_symbols=forex_symbols)
        if symbol in longer_term:
            longer_term_count += 1
            actions.append(
                {
                    "symbol": symbol,
                    "asset_class": asset_class,
                    "action": "HOLD_LONGER_TERM",
                    "quantity": position.get("quantity"),
                    "reason": "explicit_longer_term_strategy",
                    "overnight_or_weekend_risk_review_required": True,
                }
            )
            continue
        if asset_class == "futures":
            minutes_to_close = futures_close_minutes - current_minutes
            if minutes_to_close <= flatten_window_minutes:
                close_required_count += 1
                actions.append(
                    {
                        "symbol": symbol,
                        "asset_class": asset_class,
                        "action": "CLOSE_BEFORE_SESSION_CLOSE",
                        "quantity": position.get("quantity"),
                        "reason": "futures_position_near_session_close"
                        if minutes_to_close >= 0
                        else "futures_position_after_session_close",
                        "minutes_to_close": minutes_to_close,
                    }
                )
            else:
                watch_count += 1
                actions.append(
                    {
                        "symbol": symbol,
                        "asset_class": asset_class,
                        "action": "WATCH_UNTIL_SESSION_FLATTEN_WINDOW",
                        "quantity": position.get("quantity"),
                        "reason": "futures_position_before_flatten_window",
                        "minutes_to_close": minutes_to_close,
                    }
                )
            continue
        if asset_class == "forex":
            minutes_to_weekend_close = forex_close_minutes - current_minutes
            if is_friday and minutes_to_weekend_close <= flatten_window_minutes:
                close_required_count += 1
                actions.append(
                    {
                        "symbol": symbol,
                        "asset_class": asset_class,
                        "action": "CLOSE_BEFORE_WEEKEND",
                        "quantity": position.get("quantity"),
                        "reason": "forex_position_near_weekend_close"
                        if minutes_to_weekend_close >= 0
                        else "forex_position_after_weekend_close",
                        "minutes_to_close": minutes_to_weekend_close,
                    }
                )
            else:
                watch_count += 1
                actions.append(
                    {
                        "symbol": symbol,
                        "asset_class": asset_class,
                        "action": "WATCH_FOREX_SESSION",
                        "quantity": position.get("quantity"),
                        "reason": "forex_market_open_24x5",
                        "friday_weekend_check": is_friday,
                    }
                )
            continue
        review_count += 1
        actions.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "action": "REVIEW_UNKNOWN_INSTRUMENT",
                "quantity": position.get("quantity"),
                "reason": "symbol_not_found_in_futures_or_forex_readiness",
            }
        )

    if not normalized_positions:
        actions.append({"action": "NO_OPEN_POSITIONS", "reason": "position_snapshot_reported_no_open_positions"})

    if blockers:
        status = PAPER_ERROR
    elif close_required_count:
        status = PAPER_CRITICAL
    elif longer_term_count or review_count or watch_count:
        status = PAPER_WARN
    else:
        status = PAPER_OK

    return _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": status,
            "source": {
                "positions": str(Path(positions_path)),
                "futures_readiness": str(Path(futures_readiness_path)) if futures_readiness_path is not None else None,
                "forex_readiness": str(Path(forex_readiness_path)) if forex_readiness_path is not None else None,
            },
            "market_clock": {
                "current_time": current_time,
                "futures_session_close_time": futures_session_close_time,
                "forex_weekend_close_time": forex_weekend_close_time,
                "flatten_window_minutes": flatten_window_minutes,
                "as_of_weekday": as_date.isoformat(),
                "is_friday": is_friday,
            },
            "instrument_universe": {
                "futures_symbols": sorted(futures_symbols),
                "forex_symbols": sorted(forex_symbols),
            },
            "summary": {
                "open_position_count": len(normalized_positions),
                "close_required_count": close_required_count,
                "longer_term_hold_count": longer_term_count,
                "review_count": review_count,
                "watch_count": watch_count,
                "blocker_count": len(blockers),
            },
            "positions": normalized_positions,
            "actions": actions,
            "blockers": sorted(set(blockers)),
            "safety": {
                "paper_only": True,
                "read_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
    )


def render_cross_asset_session_plan_markdown(payload: Mapping[str, object]) -> str:
    summary = _mapping(payload.get("summary"))
    clock = _mapping(payload.get("market_clock"))
    actions = _object_list(payload.get("actions"))
    lines = [
        "# Cross-Asset Session Plan",
        "",
        f"Status: **{payload.get('status') or PAPER_ERROR}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Current time: `{clock.get('current_time') or ''}`",
        "",
        "## Summary",
        "",
        f"Open positions: `{summary.get('open_position_count', 0)}`",
        f"Close required: `{summary.get('close_required_count', 0)}`",
        f"Longer-term holds: `{summary.get('longer_term_hold_count', 0)}`",
        f"Review count: `{summary.get('review_count', 0)}`",
        "",
        "## Actions",
        "",
        "| Action | Asset | Symbol | Reason |",
        "| --- | --- | --- | --- |",
    ]
    for action in actions:
        if isinstance(action, Mapping):
            lines.append(
                f"| `{_escape(action.get('action') or '')}` | `{_escape(action.get('asset_class') or '')}` | "
                f"`{_escape(action.get('symbol') or '')}` | `{_escape(action.get('reason') or '')}` |"
            )
    lines.extend(["", "Read only: `True`", "Orders submitted: `False`", "Live trading authorized: `False`", ""])
    return "\n".join(lines)


def _read_optional_json(path: str | Path | None) -> Mapping[str, object] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _ready_symbols(payload: Mapping[str, object] | None, *, key: str) -> set[str]:
    rows = payload.get(key) if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("symbol") or "").upper().strip()
        for row in rows
        if isinstance(row, Mapping) and str(row.get("symbol") or "").strip() and row.get("ready") is not False
    }


def _asset_class(symbol: str, *, futures_symbols: set[str], forex_symbols: set[str]) -> str:
    if symbol in futures_symbols:
        return "futures"
    if symbol in forex_symbols or _looks_like_forex_pair(symbol):
        return "forex"
    return "unknown"


def _looks_like_forex_pair(symbol: str) -> bool:
    currencies = {
        "AUD",
        "CAD",
        "CHF",
        "EUR",
        "GBP",
        "JPY",
        "NZD",
        "USD",
    }
    return len(symbol) == 6 and symbol[:3] in currencies and symbol[3:] in currencies


def _source_safety_blockers(prefix: str, safety: Mapping[str, object]) -> list[str]:
    blockers: list[str] = []
    if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
        blockers.append(f"{prefix}_live_trading_flag")
    if safety.get("broker_client_built") is True:
        blockers.append(f"{prefix}_broker_client_built")
    if safety.get("credentials_read") is True:
        blockers.append(f"{prefix}_credentials_read")
    if safety.get("orders_submitted") is True:
        blockers.append(f"{prefix}_orders_submitted")
    return blockers


def _readiness_safety_blockers(prefix: str, payload: Mapping[str, object] | None) -> list[str]:
    if not payload:
        return []
    safety = _mapping(payload.get("safety"))
    blockers = _source_safety_blockers(prefix, safety)
    if safety.get("orders_enabled") is True:
        blockers.append(f"{prefix}_orders_enabled")
    return blockers


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
        positions.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "market_value": _float_or_none(row.get("market_value")),
                "avg_entry_price": _float_or_none(row.get("avg_entry_price")),
                "current_price": _float_or_none(row.get("current_price")),
            }
        )
    return positions


def _append_ledger(path: str | Path, report: Mapping[str, object], *, output_path: Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "record_type": "cross_asset_session_plan",
        "as_of_date": report.get("as_of_date"),
        "status": report.get("status"),
        "summary": report.get("summary"),
        "output_path": str(output_path),
        "generated_at": report.get("generated_at"),
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _parse_hhmm(value: str, *, field_name: str) -> int:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise CrossAssetSessionPlanOperationalError(f"{field_name} must use HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise CrossAssetSessionPlanOperationalError(f"{field_name} must use HH:MM") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise CrossAssetSessionPlanOperationalError(f"{field_name} must use HH:MM")
    return hour * 60 + minute


def _clean_symbols(values: Iterable[str]) -> set[str]:
    return {str(value).upper().strip() for value in values if str(value).strip()}


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _redact_payload(value: object) -> dict[str, object]:
    redacted = redact_payload(value, env={})
    if not isinstance(redacted, dict):
        raise CrossAssetSessionPlanOperationalError("cross-asset session plan must be a JSON object")
    return redacted


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
