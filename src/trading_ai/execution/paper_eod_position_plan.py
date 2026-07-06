"""Paper-only end-of-day position plan before market close."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    PAPER_CRITICAL,
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_eod_position_plan/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_eod_position_plan/latest.md"


class PaperEodPositionPlanOperationalError(RuntimeError):
    """Raised when the EOD position plan cannot be produced."""


@dataclass(frozen=True)
class PaperEodPositionPlanResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_eod_position_plan(
    *,
    as_of_date: str,
    position_watch: str | Path,
    current_time: str,
    market_close_time: str = "16:00",
    flatten_window_minutes: int = 15,
    longer_term_symbols: Iterable[str] = (),
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    ledger_output: str | Path | None = None,
    timezone: str = "America/New_York",
    generated_at: str | None = None,
) -> PaperEodPositionPlanResult:
    if flatten_window_minutes < 1:
        raise PaperEodPositionPlanOperationalError("--flatten-window-minutes must be at least 1")
    generated = generated_at or datetime.now(UTC).isoformat()
    watch_path = Path(position_watch)
    try:
        watch = read_json_artifact(watch_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PaperEodPositionPlanOperationalError(f"cannot read position watch: {exc}") from exc

    report = build_paper_eod_position_plan(
        as_of_date=as_of_date,
        position_watch=watch,
        position_watch_path=watch_path,
        current_time=current_time,
        market_close_time=market_close_time,
        flatten_window_minutes=flatten_window_minutes,
        longer_term_symbols=longer_term_symbols,
        timezone=timezone,
        generated_at=generated,
    )
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_json_artifact(report, output_path)
    write_text_artifact(render_paper_eod_position_plan_markdown(report), markdown_path)
    if ledger_output is not None:
        _append_ledger(ledger_output, report, output_path=output_path)
    status = str(report.get("status") or PAPER_ERROR)
    return PaperEodPositionPlanResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_paper_eod_position_plan(
    *,
    as_of_date: str,
    position_watch: Mapping[str, object],
    position_watch_path: str | Path,
    current_time: str,
    market_close_time: str,
    flatten_window_minutes: int,
    longer_term_symbols: Iterable[str],
    timezone: str,
    generated_at: str,
) -> dict[str, object]:
    longer_term = _clean_symbols(longer_term_symbols)
    current_minutes = _parse_hhmm(current_time, field_name="current_time")
    close_minutes = _parse_hhmm(market_close_time, field_name="market_close_time")
    minutes_to_close = close_minutes - current_minutes
    after_close = minutes_to_close < 0
    within_window = minutes_to_close <= flatten_window_minutes
    safety = _mapping(position_watch.get("safety"))
    blockers = _watch_safety_blockers(safety)
    positions = _open_positions(position_watch.get("positions"))
    actions: list[dict[str, object]] = []
    close_required_count = 0
    longer_term_count = 0
    wait_count = 0

    for position in positions:
        symbol = str(position["symbol"])
        if symbol in longer_term:
            longer_term_count += 1
            actions.append(
                {
                    "action": "HOLD_LONGER_TERM",
                    "symbol": symbol,
                    "quantity": position.get("quantity"),
                    "reason": "explicit_longer_term_strategy",
                    "overnight_risk_review_required": True,
                    "suggested_next_command": "review longer-term thesis and overnight risk before market close",
                }
            )
            continue
        if within_window:
            close_required_count += 1
            actions.append(
                {
                    "action": "CLOSE_BEFORE_MARKET_CLOSE",
                    "symbol": symbol,
                    "quantity": position.get("quantity"),
                    "reason": "intraday_position_near_market_close" if not after_close else "intraday_position_after_market_close",
                    "suggested_next_command": _flatten_command(as_of_date=as_of_date, symbols=positions, longer_term=longer_term),
                }
            )
        else:
            wait_count += 1
            actions.append(
                {
                    "action": "WATCH_UNTIL_FLATTEN_WINDOW",
                    "symbol": symbol,
                    "quantity": position.get("quantity"),
                    "reason": "position_open_before_flatten_window",
                    "minutes_to_close": minutes_to_close,
                }
            )

    if not positions:
        actions.append({"action": "NO_OPEN_POSITIONS", "reason": "position_watch_reported_no_open_positions"})

    if blockers:
        status = PAPER_ERROR
    elif close_required_count:
        status = PAPER_CRITICAL
    elif longer_term_count or wait_count:
        status = PAPER_WARN
    else:
        status = PAPER_OK

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "source": {"position_watch": str(Path(position_watch_path))},
        "market_clock": {
            "timezone": timezone,
            "current_time": current_time,
            "market_close_time": market_close_time,
            "minutes_to_close": minutes_to_close,
            "flatten_window_minutes": flatten_window_minutes,
            "within_flatten_window": within_window,
            "after_close": after_close,
        },
        "summary": {
            "open_position_count": len(positions),
            "close_required_count": close_required_count,
            "longer_term_hold_count": longer_term_count,
            "wait_count": wait_count,
            "blocker_count": len(blockers),
        },
        "positions": positions,
        "actions": actions,
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
    return _redact_payload(payload)


def render_paper_eod_position_plan_markdown(payload: Mapping[str, object]) -> str:
    clock = _mapping(payload.get("market_clock"))
    summary = _mapping(payload.get("summary"))
    actions = _object_list(payload.get("actions"))
    lines = [
        "# Paper EOD Position Plan",
        "",
        f"Status: **{payload.get('status') or PAPER_ERROR}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Time: `{clock.get('current_time') or ''}` / close `{clock.get('market_close_time') or ''}`",
        f"Minutes to close: `{clock.get('minutes_to_close')}`",
        "",
        "## Summary",
        "",
        f"Open positions: `{summary.get('open_position_count', 0)}`",
        f"Close required: `{summary.get('close_required_count', 0)}`",
        f"Longer-term holds: `{summary.get('longer_term_hold_count', 0)}`",
        "",
        "## Actions",
        "",
        "| Action | Symbol | Reason |",
        "| --- | --- | --- |",
    ]
    for action in actions:
        if isinstance(action, Mapping):
            lines.append(
                f"| `{_escape(action.get('action') or '')}` | `{_escape(action.get('symbol') or '')}` | "
                f"`{_escape(action.get('reason') or '')}` |"
            )
    lines.extend(["", "Paper only: `True`", "Orders submitted: `False`", "Live trading authorized: `False`", ""])
    return "\n".join(lines)


def _watch_safety_blockers(safety: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append(_blocker("ERROR", "watch_live_flag_true", "position watch declared live trading enabled"))
    if safety.get("broker_client_built") is True and safety.get("paper_only") is not True:
        blockers.append(_blocker("ERROR", "watch_not_paper_only", "position watch is not paper-only"))
    return blockers


def _open_positions(value: object) -> list[dict[str, object]]:
    positions: list[dict[str, object]] = []
    if not isinstance(value, list):
        return positions
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


def _flatten_command(*, as_of_date: str, symbols: list[dict[str, object]], longer_term: set[str]) -> str:
    close_symbols = [str(position.get("symbol")) for position in symbols if str(position.get("symbol")) not in longer_term]
    if longer_term:
        return "manual selective paper close required for " + ",".join(close_symbols)
    return f"trading-ai paper-safe-flatten --as-of-date {as_of_date} --confirm-paper --confirm-flatten"


def _append_ledger(path: str | Path, report: Mapping[str, object], *, output_path: Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "record_type": "paper_eod_position_plan",
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
        raise PaperEodPositionPlanOperationalError(f"{field_name} must use HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise PaperEodPositionPlanOperationalError(f"{field_name} must use HH:MM") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise PaperEodPositionPlanOperationalError(f"{field_name} must use HH:MM")
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


def _blocker(severity: str, code: str, message: str) -> dict[str, object]:
    return {"severity": severity, "code": code, "message": message}


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(redact_secrets(json.dumps(dict(payload), sort_keys=True)))


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
