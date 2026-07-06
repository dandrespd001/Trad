"""Read-only tracking for open Alpaca paper positions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from trading_ai.execution.paper_risk_state import save_risk_state

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
    risk_state_path: str | Path = DEFAULT_RISK_STATE_PATH,
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
            risk_state_path=risk_state_path,
        )
    except (PaperExecuteOperationalError, OSError, ValueError) as exc:
        payload = _error_payload(session_dir=root, as_of_date=resolved_as_of_date, reason=redact_secrets(str(exc)))
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
    risk_state_path: str | Path = DEFAULT_RISK_STATE_PATH,
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
    risk_state = load_risk_state(risk_state_path)
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
    risk_state = replace(risk_state, trailing_stops=_plan_trailing_highs(position_plan))
    save_risk_state(risk_state, risk_state_path)
    summary = _mapping(position_plan.get("summary"))
    close_count = _int_value(summary.get("close_count"))
    open_count = _int_value(summary.get("open_count"))
    protective_order_plan = _build_protective_order_plan(position_plan=position_plan, open_orders=open_orders)
    protective_summary = _mapping(protective_order_plan.get("summary"))
    protective_review_count = _int_value(protective_summary.get("review_count"))

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

    status = PAPER_ERROR if close_failed else PAPER_WARN if close_count or open_count or protective_review_count else PAPER_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_date": resolved_as_of_date,
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
        "protective_order_plan": protective_order_plan,
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
    protective_plan = _mapping(payload.get("protective_order_plan"))
    protective_summary = _mapping(protective_plan.get("summary"))
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
        f"Protective order reviews: `{protective_summary.get('review_count', 0)}`",
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


def _error_payload(*, session_dir: Path, as_of_date: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_date": as_of_date,
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


def _plan_trailing_highs(position_plan: Mapping[str, object]) -> dict[str, float]:
    summary = _mapping(position_plan.get("summary"))
    highs = summary.get("trailing_highs")
    result: dict[str, float] = {}
    if isinstance(highs, Mapping):
        for symbol, value in highs.items():
            number = _float_or_none(value)
            if number is not None:
                result[str(symbol).upper()] = number
    return result


def _build_protective_order_plan(
    *,
    position_plan: Mapping[str, object],
    open_orders: tuple[object, ...],
) -> dict[str, object]:
    actions: list[dict[str, object]] = []
    missing_stop_loss = 0
    missing_take_profit = 0
    stale_stop_loss = 0
    stale_take_profit = 0
    order_snapshots = [order for order in open_orders if isinstance(order, object)]
    for plan_action in _plan_actions(position_plan):
        if str(plan_action.get("action") or "").upper() != "HOLD":
            continue
        symbol = str(plan_action.get("symbol") or "").upper()
        quantity = _float_or_none(plan_action.get("quantity"))
        levels = _mapping(plan_action.get("protective_levels"))
        stop_loss_price = _float_or_none(levels.get("stop_loss_price"))
        take_profit_price = _float_or_none(levels.get("take_profit_price"))
        if stop_loss_price is not None and stop_loss_price > 0:
            action, missing, stale = _protective_order_action(
                symbol=symbol,
                quantity=quantity,
                protection_type="stop_loss",
                target_price=stop_loss_price,
                open_orders=order_snapshots,
            )
            if action is not None:
                actions.append(action)
            missing_stop_loss += missing
            stale_stop_loss += stale
        if take_profit_price is not None and take_profit_price > 0:
            action, missing, stale = _protective_order_action(
                symbol=symbol,
                quantity=quantity,
                protection_type="take_profit",
                target_price=take_profit_price,
                open_orders=order_snapshots,
            )
            if action is not None:
                actions.append(action)
            missing_take_profit += missing
            stale_take_profit += stale
    review_count = len(actions)
    return {
        "status": PAPER_WARN if review_count else PAPER_OK,
        "actions": actions,
        "summary": {
            "missing_stop_loss_count": missing_stop_loss,
            "missing_take_profit_count": missing_take_profit,
            "stale_stop_loss_count": stale_stop_loss,
            "stale_take_profit_count": stale_take_profit,
            "review_count": review_count,
        },
        "safety": {
            "paper_only": True,
            "read_only": True,
            "orders_submitted": False,
            "orders_cancelled": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def _protective_order_action(
    *,
    symbol: str,
    quantity: float | None,
    protection_type: str,
    target_price: float,
    open_orders: list[object],
) -> tuple[dict[str, object] | None, int, int]:
    matching_orders = [
        order for order in open_orders if _order_symbol(order) == symbol and _order_matches_protection(order, protection_type)
    ]
    base = {
        "symbol": symbol,
        "side": "sell",
        "quantity": quantity,
        "protection_type": protection_type,
        "target_price": target_price,
    }
    if not matching_orders:
        return (
            {
                **base,
                "action": "CREATE_PROTECTIVE_ORDER",
                "reason": f"missing_{protection_type}_order",
            },
            1,
            0,
        )
    aligned = [order for order in matching_orders if _prices_match(_order_protection_price(order, protection_type), target_price)]
    if aligned:
        return None, 0, 0
    current_order = matching_orders[0]
    return (
        {
            **base,
            "action": "UPDATE_PROTECTIVE_ORDER",
            "reason": f"stale_{protection_type}_order",
            "current_price": _order_protection_price(current_order, protection_type),
            "current_order": _paper_order_snapshot_to_dict(current_order),
        },
        0,
        1,
    )


def _plan_actions(position_plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    actions = position_plan.get("actions")
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, Mapping)]


def _order_symbol(order: object) -> str:
    return str(getattr(order, "symbol", "") or "").upper()


def _order_matches_protection(order: object, protection_type: str) -> bool:
    if str(getattr(order, "side", "") or "").lower() != "sell":
        return False
    order_type = str(getattr(order, "order_type", "") or "").lower()
    if protection_type == "stop_loss":
        return order_type in {"stop", "stop_limit", "trailing_stop"}
    if protection_type == "take_profit":
        return order_type == "limit"
    return False


def _order_protection_price(order: object, protection_type: str) -> float | None:
    if protection_type == "stop_loss":
        return _float_or_none(getattr(order, "stop_price", None))
    if protection_type == "take_profit":
        return _float_or_none(getattr(order, "limit_price", None))
    return None


def _prices_match(current: float | None, target: float) -> bool:
    if current is None:
        return False
    return abs(current - target) <= max(0.01, abs(target) * 0.001)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
