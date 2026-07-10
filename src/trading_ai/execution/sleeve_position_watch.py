"""Read-only position + fills surveillance for the sleeve portfolio (M9).

The governed sleeve-rebalance cycle (M3) writes plan+submission artifacts once
per day, but the operator has no READ-ONLY inspection of the broker's current
state between cycles. This module is the dedicated watch command: it lists
open positions with their unrealized PnL, the day's filled sleeve orders, and
the live account risk context (Sprint M7 kill-switch inputs), and warns when
the account is approaching either of the daily-loss / drawdown limits.

Outputs are two artifacts:

1. ``output`` — the machine-readable scorecard (positions, fills, account_risk,
   incidents, status, safety). Always written.
2. ``telegram_artifact`` (optional) — a sendable Telegram payload mirroring
   the schema used by ``paper-telegram-send``: ``schema_version``,
   ``as_of_date``, ``status`` (OK | WARN), ``message`` (multiline), and
   ``safety`` declaring ``paper_only: true``. The launcher invokes
   ``paper-telegram-send`` to actually push it.

The module NEVER submits or cancels orders — every broker call is read-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.config import ConfigError, load_risk_config
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
)
from trading_ai.execution.sleeve_rebalance import (
    DEFAULT_EQUITY_HIGHWATER_PATH,
    _account_risk_context,
)

SCHEMA_VERSION = "1.0"
WARN_FRACTION = 0.75  # warn at 75% of a kill-switch limit


@dataclass(frozen=True)
class SleevePositionWatchResult:
    exit_code: int
    status: str  # OK | WARN | BLOCKED
    output_path: Path
    payload: dict[str, object]


def _coerce_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerce_position_value(position: Any, attribute: str, *, default: Any = None) -> Any:
    """Tolerantly pull ``attribute`` from a position (dict or duck-typed object)."""
    if isinstance(position, Mapping):
        return position.get(attribute, default)
    return getattr(position, attribute, default)


def _collect_positions(broker: Any) -> list[dict[str, object]]:
    """Return a normalized list of position dicts from the broker.

    Each entry carries ``symbol``, ``quantity``, ``market_value``,
    ``avg_entry_price``, ``unrealized_pl``, ``unrealized_plpc`` — every PnL
    field is ``None`` when the broker snapshot omits it so the watch report
    always renders even on a thin paper snapshot.
    """
    raw_positions = broker.read_positions()
    entries: list[dict[str, object]] = []
    for position in raw_positions or []:
        entries.append(
            {
                "symbol": str(_coerce_position_value(position, "symbol", default="") or "").upper(),
                "quantity": _coerce_position_value(position, "quantity"),
                "qty": _coerce_position_value(position, "qty"),
                "market_value": _coerce_position_value(position, "market_value"),
                "avg_entry_price": _coerce_optional_float(
                    _coerce_position_value(position, "avg_entry_price", default=None)
                ),
                "unrealized_pl": _coerce_optional_float(
                    _coerce_position_value(position, "unrealized_pl", default=None)
                ),
                "unrealized_plpc": _coerce_optional_float(
                    _coerce_position_value(position, "unrealized_plpc", default=None)
                ),
            }
        )
    return entries


def _position_quantity(position: Mapping[str, object]) -> float | None:
    """Return the position's quantity from either ``quantity`` or ``qty``.

    The sleeve cycle reads ``qty`` from the broker; PaperPosition uses
    ``quantity``. The watch report needs one canonical field per position.
    """
    for key in ("quantity", "qty"):
        value = position.get(key)
        coerced = _coerce_optional_float(value)
        if coerced is not None:
            return coerced
    return None


def _collect_fills_today(
    broker: Any,
    *,
    as_of: date,
) -> tuple[list[dict[str, object]], list[str]]:
    """Return ``(fills, incidents)`` for today, with graceful degradation.

    Only ``sleeve-`` prefixed orders from ``list_orders(status="closed")`` are
    considered, and only those with positive filled quantity. A raising
    ``list_orders`` is recorded as ``orders_list_failed`` — fills become
    empty but the rest of the report still ships.
    """
    incidents: list[str] = []
    try:
        closed_orders = broker.list_orders(status="closed")
    except Exception as exc:  # noqa: BLE001 - broker failures degrade to an incident
        incidents.append(f"orders_list_failed:{type(exc).__name__}:{exc}")
        return [], incidents
    fills: list[dict[str, object]] = []
    as_of_iso = as_of.isoformat()
    for order in closed_orders or []:
        client_order_id = str(_coerce_position_value(order, "client_order_id", default="") or "")
        if not client_order_id.startswith("sleeve-"):
            continue
        filled_qty = _coerce_optional_float(_coerce_position_value(order, "filled_quantity", default=None))
        if filled_qty is None or filled_qty <= 0:
            continue
        updated_at = str(_coerce_position_value(order, "updated_at", default="") or "")
        if not updated_at:
            continue
        # updated_at is an ISO timestamp — keep the date prefix.
        updated_date = updated_at[:10]
        if updated_date != as_of_iso:
            continue
        fills.append(
            {
                "client_order_id": client_order_id,
                "symbol": str(_coerce_position_value(order, "symbol", default="") or "").upper(),
                "side": str(_coerce_position_value(order, "side", default="") or "").lower(),
                "filled_qty": filled_qty,
                "filled_avg_price": _coerce_optional_float(
                    _coerce_position_value(order, "filled_avg_price", default=None)
                ),
                "notional": _coerce_optional_float(
                    _coerce_position_value(order, "notional", default=None)
                ),
                "updated_at": updated_at,
            }
        )
    return fills, incidents


def _resolve_risk_warnings(
    risk_context: Mapping[str, object] | None,
    *,
    max_daily_loss_pct: float,
    max_drawdown_pct: float,
) -> list[str]:
    """Return blocker slugs when the account is approaching a kill-switch."""
    blockers: list[str] = []
    if risk_context is None:
        return blockers
    daily_pnl_pct = _coerce_optional_float(risk_context.get("daily_pnl_pct")) or 0.0
    current_drawdown_pct = _coerce_optional_float(risk_context.get("current_drawdown_pct")) or 0.0
    daily_loss_threshold = -abs(max_daily_loss_pct) * WARN_FRACTION
    drawdown_threshold = abs(max_drawdown_pct) * WARN_FRACTION
    if daily_pnl_pct <= daily_loss_threshold:
        blockers.append("approaching_kill_switch:daily_loss")
    if current_drawdown_pct >= drawdown_threshold:
        blockers.append("approaching_kill_switch:drawdown")
    return blockers


def _render_telegram_message(
    *,
    as_of: date,
    positions: list[Mapping[str, object]],
    fills: list[Mapping[str, object]],
    risk_context: Mapping[str, object] | None,
    status: str,
    warnings: list[str],
) -> str:
    """Build the multiline Telegram message body (Telegram-safe ASCII)."""
    lines: list[str] = [f"Posiciones paper {as_of.isoformat()}"]
    if not positions:
        lines.append("sin posiciones abiertas")
    else:
        for position in positions:
            symbol = str(position.get("symbol") or "?")
            qty = _position_quantity(position)
            market_value = _coerce_optional_float(position.get("market_value"))
            unrealized_plpc = _coerce_optional_float(position.get("unrealized_plpc"))
            qty_text = f"{qty:g}" if qty is not None else "?"
            mv_text = f"{market_value:.2f}" if market_value is not None else "?"
            plpc_text = f"{unrealized_plpc * 100:.2f}%" if unrealized_plpc is not None else "n/a"
            lines.append(f"{symbol} qty={qty_text} ${mv_text} PnL {plpc_text}")
    if fills:
        lines.append("Fills hoy:")
        for fill in fills:
            symbol = str(fill.get("symbol") or "?")
            side = str(fill.get("side") or "?")
            filled_avg_price = _coerce_optional_float(fill.get("filled_avg_price"))
            filled_qty = _coerce_optional_float(fill.get("filled_qty"))
            notional = _coerce_optional_float(fill.get("notional"))
            if notional is not None:
                body = f"${notional:.2f}"
            elif filled_qty is not None and filled_avg_price is not None:
                body = f"{filled_qty:g}@{filled_avg_price:.2f}"
            else:
                body = "n/a"
            price_text = f"@{filled_avg_price:.2f}" if filled_avg_price is not None else ""
            lines.append(f"{symbol} {side} {body} {price_text}".rstrip())
    if risk_context is not None:
        equity = _coerce_optional_float(risk_context.get("equity"))
        daily_pnl_pct = _coerce_optional_float(risk_context.get("daily_pnl_pct"))
        current_drawdown_pct = _coerce_optional_float(risk_context.get("current_drawdown_pct"))
        equity_text = f"${equity:.2f}" if equity is not None else "n/a"
        dd_text = f"{current_drawdown_pct * 100:.2f}%" if current_drawdown_pct is not None else "n/a"
        pnl_text = f"{daily_pnl_pct * 100:.2f}%" if daily_pnl_pct is not None else "n/a"
        lines.append(f"equity {equity_text} dd {dd_text} pnl_dia {pnl_text}")
    if status == PAPER_WARN and warnings:
        for blocker in warnings:
            lines.append(f"AVISO: {blocker}")
    return "\n".join(lines)


def run_sleeve_position_watch(
    *,
    risk_config: str | Path,
    output: str | Path,
    telegram_artifact: str | Path | None = None,
    broker: Any,
    as_of_date: date | None = None,
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    generated_at: str | None = None,
) -> SleevePositionWatchResult:
    """Build the sleeve position watch report (read-only)."""

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()

    if broker is None:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "positions": [],
            "fills_today": [],
            "account_risk": None,
            "incidents": ["broker_unavailable"],
            "blockers": ["broker_unavailable"],
            "status": PAPER_BLOCKED,
            "safety": {"read_only": True, "orders_submitted": False},
        }
        write_json_artifact(payload, output_path)
        return SleevePositionWatchResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    try:
        risk = load_risk_config(risk_config, allow_live=False)
    except ConfigError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "positions": [],
            "fills_today": [],
            "account_risk": None,
            "incidents": [f"risk_config_error:{exc}"],
            "blockers": [f"risk_config_error:{exc}"],
            "status": PAPER_BLOCKED,
            "safety": {"read_only": True, "orders_submitted": False},
        }
        write_json_artifact(payload, output_path)
        return SleevePositionWatchResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    incidents: list[str] = []
    try:
        positions = _collect_positions(broker)
    except Exception as exc:  # noqa: BLE001 - broker failure: report and continue
        incidents.append(f"positions_read_failed:{type(exc).__name__}:{exc}")
        positions = []

    fills, fill_incidents = _collect_fills_today(broker, as_of=as_of)
    incidents.extend(fill_incidents)

    risk_context = _account_risk_context(broker, Path(equity_highwater_path))
    if risk_context is None:
        incidents.append("account_risk_context_unavailable")

    risk_warnings = _resolve_risk_warnings(
        risk_context,
        max_daily_loss_pct=risk.max_daily_loss_pct,
        max_drawdown_pct=risk.max_drawdown_pct,
    )

    all_incidents = sorted(set(incidents + risk_warnings))
    blockers = all_incidents
    if all_incidents:
        status = PAPER_WARN
    else:
        status = PAPER_OK

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "positions": positions,
        "fills_today": fills,
        "account_risk": risk_context,
        "incidents": all_incidents,
        "blockers": blockers,
        "status": status,
        "safety": {"read_only": True, "orders_submitted": False},
    }
    write_json_artifact(payload, output_path)

    if telegram_artifact is not None:
        telegram_payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "as_of_date": as_of.isoformat(),
            "status": status if status != PAPER_BLOCKED else PAPER_WARN,
            "message": _render_telegram_message(
                as_of=as_of,
                positions=positions,
                fills=fills,
                risk_context=risk_context,
                status=status,
                warnings=risk_warnings,
            ),
            "safety": {
                "paper_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
        write_json_artifact(telegram_payload, Path(telegram_artifact))

    return SleevePositionWatchResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


__all__ = [
    "SCHEMA_VERSION",
    "WARN_FRACTION",
    "SleevePositionWatchResult",
    "run_sleeve_position_watch",
]
