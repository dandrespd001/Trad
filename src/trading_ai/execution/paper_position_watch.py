"""Read-only tracking for open Alpaca paper positions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker
from trading_ai.execution.paper_common import (
    PAPER_ERROR,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_execute_session import (
    PaperExecuteOperationalError,
    _load_approved_session_package,
    _load_risk_from_session,
    _load_universe_from_session,
    _mapping_or_none,
    _order_from_close_action,
    _paper_account_to_dict,
    _paper_order_intent_to_dict,
    _paper_order_result_to_dict,
    _paper_order_snapshot_to_dict,
    _paper_position_to_dict,
    _signal_list,
)
from trading_ai.execution.paper_position_plan import build_position_plan, close_actions
from trading_ai.execution.paper_risk_state import DEFAULT_RISK_STATE_PATH, load_risk_state

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_position_watch/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_position_watch/latest.md"


class PaperPositionWatchOperationalError(RuntimeError):
    """Raised when position watch cannot safely run."""


@dataclass(frozen=True)
class PaperPositionWatchResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_position_watch(
    *,
    session_dir: str | Path,
    confirm_paper: bool,
    confirm_dynamic_position_actions: bool = False,
    as_of_date: str = "today",
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
) -> PaperPositionWatchResult:
    if not confirm_paper:
        raise PaperPositionWatchOperationalError("paper position watch requires --confirm-paper")

    root = Path(session_dir)
    if not root.exists() or not root.is_dir():
        raise PaperPositionWatchOperationalError(f"session directory does not exist: {root}")

    output_path = Path(output)
    markdown_path = Path(markdown_output)
    resolved_as_of_date = datetime.now(UTC).date().isoformat() if as_of_date == "today" else as_of_date
    try:
        payload = build_paper_position_watch(
            session_dir=root,
            execute_closes=confirm_dynamic_position_actions,
            as_of_date=resolved_as_of_date,
        )
    except (PaperExecuteOperationalError, OSError, ValueError) as exc:
        payload = _error_payload(session_dir=root, reason=redact_secrets(str(exc)))
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_position_watch_markdown(payload), markdown_path)
    status = str(payload.get("status") or PAPER_ERROR)
    return PaperPositionWatchResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def build_paper_position_watch(
    *,
    session_dir: Path,
    execute_closes: bool = False,
    as_of_date: str = "today",
) -> dict[str, object]:
    package = _load_approved_session_package(session_dir)
    universe = _load_universe_from_session(package.session, session_dir)
    risk_limits = _load_risk_from_session(package.session, session_dir)
    client = build_alpaca_paper_client()
    broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk_limits, dry_run=False)
    account = broker.read_account()
    positions = broker.read_positions()
    open_orders = broker.list_orders(status="open")
    signal_report = package.signal_report
    selected_signal = _mapping_or_none(signal_report.get("selected_signal"))
    signals = _signal_list(signal_report.get("signals"))
    risk_state = load_risk_state(DEFAULT_RISK_STATE_PATH)
    position_plan = build_position_plan(
        signals=signals,
        selected_signal=selected_signal,
        positions=positions,
        signal_quality=_mapping_or_none(signal_report.get("signal_quality")),
        paper_notional_usd=float(risk_limits.paper_notional_usd),
        stop_loss_atr_mult=float(risk_limits.stop_loss_atr_mult),
        take_profit_atr_mult=float(risk_limits.take_profit_atr_mult),
        trailing_atr_mult=float(risk_limits.trailing_atr_mult),
        trailing_high_by_symbol=risk_state.trailing_stops,
    )
    summary = _mapping(position_plan.get("summary"))
    close_count = _int_value(summary.get("close_count"))
    open_count = _int_value(summary.get("open_count"))

    # Protective-exit supervisor: when confirmed, execute CLOSE actions only.
    # Opens are never submitted here (a low-frequency intraday loop must only de-risk).
    resolved_as_of_date = datetime.now(UTC).date().isoformat() if as_of_date == "today" else as_of_date
    position_order_results: list[dict[str, object]] = []
    orders_submitted = False
    close_failed = False
    if execute_closes:
        for close_action in close_actions(position_plan):
            close_order = _order_from_close_action(close_action, as_of_date=resolved_as_of_date)
            close_result = broker.submit_order(close_order)
            final_close_order = (
                broker.get_order_by_client_id(close_order.client_order_id) if close_result.accepted else None
            )
            orders_submitted = orders_submitted or close_result.accepted
            close_failed = close_failed or not close_result.accepted
            position_order_results.append(
                {
                    "action": dict(close_action),
                    "order_sent": _paper_order_intent_to_dict(close_order),
                    "broker_result": _paper_order_result_to_dict(close_result),
                    "final_order": _paper_order_snapshot_to_dict(final_close_order)
                    if final_close_order is not None
                    else None,
                }
            )

    status = PAPER_ERROR if close_failed else PAPER_WARN if close_count or open_count else PAPER_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "session": {
            "session_dir": str(session_dir),
            "as_of_date": package.session.get("as_of_date"),
            "ready_for_paper_review": package.session.get("ready_for_paper_review") is True,
        },
        "account": _paper_account_to_dict(account),
        "positions": [_paper_position_to_dict(position) for position in positions],
        "open_orders": [_paper_order_snapshot_to_dict(order) for order in open_orders],
        "position_plan": position_plan,
        "position_order_results": position_order_results,
        "safety": {
            "paper_only": True,
            "read_only": not execute_closes,
            "closes_only": execute_closes,
            "orders_submitted": orders_submitted,
            "orders_cancelled": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_position_watch_markdown(payload: Mapping[str, object]) -> str:
    plan = _mapping(payload.get("position_plan"))
    summary = _mapping(plan.get("summary"))
    actions_value = plan.get("actions")
    actions = actions_value if isinstance(actions_value, list) else []
    lines = [
        "# Paper Position Watch",
        "",
        f"Status: **{payload.get('status') or PAPER_ERROR}**",
        f"Generated at: `{payload.get('generated_at') or ''}`",
        "",
        "## Summary",
        "",
        f"Positions: `{summary.get('position_count', 0)}`",
        f"Hold: `{summary.get('hold_count', 0)}`",
        f"Close: `{summary.get('close_count', 0)}`",
        f"Open: `{summary.get('open_count', 0)}`",
        "",
        "## Actions",
        "",
        "| Action | Symbol | Reason |",
        "| --- | --- | --- |",
    ]
    if not actions:
        lines.append("| OK | none | No position actions. |")
    for action in actions:
        if isinstance(action, Mapping):
            lines.append(
                f"| `{action.get('action') or ''}` | `{action.get('symbol') or ''}` | "
                f"`{action.get('reason') or ''}` |"
            )
    safety = _mapping(payload.get("safety"))
    lines.extend(
        [
            "",
            f"Read-only: `{safety.get('read_only', True)}`",
            f"Closes only: `{safety.get('closes_only', False)}`",
            f"Orders submitted: `{safety.get('orders_submitted', False)}`",
            "Live trading authorized: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _error_payload(*, session_dir: Path, reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": PAPER_ERROR,
        "session": {"session_dir": str(session_dir)},
        "reason": reason,
        "position_plan": {"actions": [], "summary": {}},
        "safety": {
            "paper_only": True,
            "read_only": True,
            "orders_submitted": False,
            "orders_cancelled": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0
