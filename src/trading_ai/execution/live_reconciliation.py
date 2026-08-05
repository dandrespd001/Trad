"""Live reconciliation primitives for fake/dry-run workflows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LivePosition:
    symbol: str
    quantity: float


@dataclass(frozen=True)
class LiveOrderSnapshot:
    symbol: str
    client_order_id: str
    status: str
    age_seconds: int = 0


@dataclass(frozen=True)
class LiveReconciliationReport:
    status: str
    divergences: list[dict[str, object]]


def reconcile_live_positions(
    *,
    expected_positions: Sequence[LivePosition],
    broker_positions: Sequence[LivePosition],
    open_orders: Sequence[LiveOrderSnapshot],
    allowlist: Sequence[str],
    fill_timeout_seconds: int = 300,
) -> LiveReconciliationReport:
    divergences: list[dict[str, object]] = []
    allow_names = [
        symbol.strip().upper()
        for symbol in allowlist
        if isinstance(symbol, str) and symbol.strip()
    ]
    if len(allow_names) != len(allowlist) or len(set(allow_names)) != len(allow_names):
        divergences.append(_divergence("allowlist_invalid", "", "allowlist must be unique strings"))
    allow = set(allow_names)
    expected = _position_map(expected_positions, source="expected", divergences=divergences)
    broker = _position_map(broker_positions, source="broker", divergences=divergences)
    timeout_valid = type(fill_timeout_seconds) is int and fill_timeout_seconds > 0
    if not timeout_valid:
        divergences.append(
            _divergence(
                "fill_timeout_config_invalid",
                "",
                "fill timeout must be a positive integer",
            )
        )

    for symbol, quantity in broker.items():
        if symbol not in allow:
            divergences.append(_divergence("symbol_not_allowlisted", symbol, f"{symbol} is outside allowlist"))
        expected_quantity = expected.get(symbol)
        if expected_quantity is None:
            if abs(quantity) > 0:
                divergences.append(_divergence("unexpected_position", symbol, f"unexpected broker position {quantity}"))
            continue
        if abs(quantity - expected_quantity) > 1e-9:
            divergences.append(
                _divergence("quantity_mismatch", symbol, f"expected {expected_quantity} but broker has {quantity}")
            )
    for symbol, quantity in expected.items():
        if symbol not in allow:
            divergences.append(
                _divergence("symbol_not_allowlisted", symbol, f"{symbol} is outside allowlist")
            )
        if symbol not in broker and abs(quantity) > 0:
            divergences.append(_divergence("quantity_mismatch", symbol, f"expected {quantity} but broker has 0"))

    active_statuses = {
        "accepted",
        "calculated",
        "held",
        "new",
        "partially_filled",
        "pending",
        "pending_cancel",
        "pending_new",
        "pending_replace",
    }
    terminal_statuses = {"canceled", "expired", "filled", "rejected", "replaced", "stopped", "suspended"}
    seen_order_ids: set[str] = set()
    for order in open_orders:
        symbol = order.symbol.strip().upper() if isinstance(order.symbol, str) else ""
        client_order_id = (
            order.client_order_id.strip()
            if isinstance(order.client_order_id, str)
            else ""
        )
        if not symbol or not client_order_id:
            divergences.append(
                _divergence("open_order_identity_invalid", symbol, "open order identity is invalid")
            )
        elif client_order_id in seen_order_ids:
            divergences.append(
                _divergence(
                    "duplicate_open_order_id",
                    symbol,
                    f"duplicate open order {client_order_id}",
                )
            )
        else:
            seen_order_ids.add(client_order_id)
        if symbol not in allow:
            divergences.append(_divergence("symbol_not_allowlisted", symbol, f"{symbol} order is outside allowlist"))
        status = order.status.strip().lower() if isinstance(order.status, str) else ""
        if status in active_statuses:
            divergences.append(_divergence("pending_order", symbol, f"{client_order_id} is {status}"))
        elif status in terminal_statuses:
            divergences.append(
                _divergence(
                    "terminal_order_in_open_snapshot",
                    symbol,
                    f"{client_order_id} is terminal ({status})",
                )
            )
        else:
            divergences.append(
                _divergence("unknown_order_status", symbol, f"{client_order_id} has unknown status")
            )
        age_valid = type(order.age_seconds) is int and order.age_seconds >= 0
        if not age_valid:
            divergences.append(
                _divergence("open_order_age_invalid", symbol, f"{client_order_id} has invalid age")
            )
        elif timeout_valid and status in active_statuses and order.age_seconds >= fill_timeout_seconds:
            divergences.append(_divergence("fill_timeout", symbol, f"{client_order_id} exceeded timeout"))

    return LiveReconciliationReport(status="BLOCKED" if divergences else "OK", divergences=divergences)


def _divergence(code: str, symbol: str, message: str) -> dict[str, object]:
    return {"code": code, "symbol": symbol, "message": message}


def _position_map(
    positions: Sequence[LivePosition],
    *,
    source: str,
    divergences: list[dict[str, object]],
) -> dict[str, float]:
    result: dict[str, float] = {}
    for position in positions:
        symbol = position.symbol.strip().upper() if isinstance(position.symbol, str) else ""
        quantity = _finite_float(position.quantity)
        if not symbol or quantity is None:
            divergences.append(
                _divergence(
                    f"{source}_position_invalid",
                    symbol,
                    f"{source} position is invalid",
                )
            )
            continue
        if symbol in result:
            divergences.append(
                _divergence(
                    f"duplicate_{source}_position",
                    symbol,
                    f"duplicate {source} position for {symbol}",
                )
            )
            continue
        result[symbol] = quantity
    return result


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None
