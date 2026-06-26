"""Live reconciliation primitives for fake/dry-run workflows."""

from __future__ import annotations

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
    allow = {symbol.upper() for symbol in allowlist}
    expected = {position.symbol.upper(): float(position.quantity) for position in expected_positions}
    broker = {position.symbol.upper(): float(position.quantity) for position in broker_positions}
    divergences: list[dict[str, object]] = []

    for symbol, quantity in broker.items():
        if symbol not in allow:
            divergences.append(_divergence("symbol_not_allowlisted", symbol, f"{symbol} is outside allowlist"))
        if symbol not in expected and abs(quantity) > 0:
            divergences.append(_divergence("unexpected_position", symbol, f"unexpected broker position {quantity}"))
        elif abs(quantity - expected[symbol]) > 1e-9:
            divergences.append(
                _divergence("quantity_mismatch", symbol, f"expected {expected[symbol]} but broker has {quantity}")
            )
    for symbol, quantity in expected.items():
        if symbol not in broker and abs(quantity) > 0:
            divergences.append(_divergence("quantity_mismatch", symbol, f"expected {quantity} but broker has 0"))

    for order in open_orders:
        symbol = order.symbol.upper()
        if symbol not in allow:
            divergences.append(_divergence("symbol_not_allowlisted", symbol, f"{symbol} order is outside allowlist"))
        if str(order.status).lower() in {"new", "accepted", "pending", "partially_filled"}:
            divergences.append(_divergence("pending_order", symbol, f"{order.client_order_id} is {order.status}"))
            if order.age_seconds >= fill_timeout_seconds:
                divergences.append(_divergence("fill_timeout", symbol, f"{order.client_order_id} exceeded timeout"))

    return LiveReconciliationReport(status="BLOCKED" if divergences else "OK", divergences=divergences)


def _divergence(code: str, symbol: str, message: str) -> dict[str, object]:
    return {"code": code, "symbol": symbol, "message": message}
