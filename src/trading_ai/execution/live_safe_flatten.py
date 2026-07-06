"""Dry-run live safe flatten evidence.

Before S12 this module never submits real orders. It only derives the close
orders that a human-approved rollback path would need.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.execution.live_reconciliation import LivePosition
from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact

DEFAULT_OUTPUT_DIR = "reports/tmp/live_safe_flatten"


@dataclass(frozen=True)
class LiveSafeFlattenResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_live_safe_flatten(
    *,
    as_of_date: str,
    broker: Any,
    allowlist: Sequence[str],
    reviewer: str,
    reason: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LiveSafeFlattenResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "live_safe_flatten.json"
    markdown_path = output_root / "live_safe_flatten.md"
    allow = {symbol.upper() for symbol in allowlist}
    blockers: list[str] = []
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")

    positions = _read_positions(broker)
    flatten_orders: list[dict[str, object]] = []
    for position in positions:
        symbol = position.symbol.upper()
        if symbol not in allow:
            blockers.append(f"symbol_not_allowlisted:{symbol}")
            continue
        quantity = float(position.quantity)
        if quantity == 0:
            continue
        flatten_orders.append(
            {
                "symbol": symbol,
                "side": "sell" if quantity > 0 else "buy",
                "quantity": abs(quantity),
                "simulated": True,
                "client_order_id": f"live-flatten-{as_of_date}-{symbol}".lower(),
            }
        )

    if blockers:
        flatten_orders = []
    status = "DRY_RUN_READY" if not blockers else "BLOCKED"
    payload = {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "as_of_date": as_of_date,
        "status": status,
        "reviewer": reviewer,
        "reason": reason,
        "blockers": _dedupe(blockers),
        "flatten_orders": flatten_orders,
        "flatten_count": len(flatten_orders),
        "safety": {
            "dry_run_only": True,
            "fake_broker_used": True,
            "orders_submitted": False,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_execution_enabled": False,
        },
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(_render(payload), markdown_path)
    return LiveSafeFlattenResult(
        exit_code=0 if status == "DRY_RUN_READY" else 1,
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _read_positions(broker: Any) -> list[LivePosition]:
    raw_positions = broker.read_positions()
    result: list[LivePosition] = []
    for item in raw_positions:
        if isinstance(item, LivePosition):
            result.append(item)
        else:
            result.append(LivePosition(symbol=str(getattr(item, "symbol")), quantity=float(getattr(item, "quantity"))))
    return result


def _render(payload: dict[str, object]) -> str:
    lines = [
        "# Live Safe Flatten",
        "",
        f"Status: **{payload.get('status')}**",
        f"As of date: `{payload.get('as_of_date')}`",
        "",
        "| Symbol | Side | Quantity | Simulated |",
        "| --- | --- | --- | --- |",
    ]
    orders = payload.get("flatten_orders")
    if not isinstance(orders, list) or not orders:
        lines.append("| none | - | 0 | True |")
    else:
        for order in orders:
            if isinstance(order, dict):
                lines.append(
                    f"| `{order.get('symbol')}` | `{order.get('side')}` | "
                    f"`{order.get('quantity')}` | `{order.get('simulated')}` |"
                )
    lines.extend(["", "Orders submitted: `False`", ""])
    return "\n".join(lines)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
