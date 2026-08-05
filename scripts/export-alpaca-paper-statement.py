#!/usr/bin/env python3
"""Export a paper-only order fill through the credential-free executor IPC."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.alpaca_paper import PaperOrderSnapshot
from trading_ai.execution.paper_executor_client import PaperExecutorBrokerClient
from trading_ai.execution.paper_executor_ipc import PaperExecutorIpcError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-order-id", required=True)
    parser.add_argument("--order-id")
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--raw-output")
    parser.add_argument("--search-after")
    parser.add_argument("--search-until")
    args = parser.parse_args()

    try:
        broker = PaperExecutorBrokerClient()
        order = _find_order(
            broker=broker,
            client_order_id=args.client_order_id,
            order_id=args.order_id,
            as_of_date=args.as_of_date,
            search_after=args.search_after,
            search_until=args.search_until,
        )
    except (PaperExecutorIpcError, RuntimeError, ValueError):
        print("paper executor order lookup failed", file=sys.stderr)
        return 2

    if not isinstance(order, dict):
        print("Alpaca order lookup returned an unexpected response", file=sys.stderr)
        return 2

    row = _statement_row(order)
    if not row["client_order_id"]:
        row["client_order_id"] = args.client_order_id
    errors = _row_errors(row, as_of_date=args.as_of_date)
    if errors:
        print("broker order cannot be exported as a valid statement: " + ", ".join(errors), file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "client_order_id",
                "symbol",
                "side",
                "quantity",
                "filled_avg_price",
                "filled_at",
                "realized_pnl",
                "source",
                "broker_order_id",
                "broker_status",
            ],
        )
        writer.writeheader()
        writer.writerow(row)

    raw_output = Path(args.raw_output) if args.raw_output else None
    if raw_output is not None:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(json.dumps(_redacted_order(order), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote Alpaca paper statement CSV to {output}")
    print(f"client_order_id={row['client_order_id']} symbol={row['symbol']} status={row['broker_status']}")
    return 0


def _find_order(
    *,
    broker: PaperExecutorBrokerClient,
    client_order_id: str,
    order_id: str | None,
    as_of_date: str,
    search_after: str | None,
    search_until: str | None,
) -> dict[str, object]:
    if order_id:
        order = _order_dict(broker.get_order(order_id=order_id))
        broker_client_order_id = str(order["client_order_id"])
        if broker_client_order_id != client_order_id:
            raise RuntimeError("paper executor order-id lookup mismatch")
        return order

    after = search_after or _default_after(client_order_id, as_of_date)
    until = search_until or f"{as_of_date}T23:59:59Z"
    orders = broker.list_orders(status="all")
    matches = [
        _order_dict(order)
        for order in orders
        if order.client_order_id == client_order_id
    ]
    if len(matches) > 1:
        raise RuntimeError("paper executor returned duplicate client order ids")
    if len(matches) == 1:
        return matches[0]

    activity_match = _find_order_from_fill_activities(
        broker=broker,
        client_order_id=client_order_id,
        after=after,
        until=until,
    )
    if activity_match is not None:
        return activity_match

    raise RuntimeError(
        "paper executor returned no matching order"
    )


def _find_order_from_fill_activities(
    *,
    broker: PaperExecutorBrokerClient,
    client_order_id: str,
    after: str,
    until: str,
) -> dict[str, object] | None:
    activities = broker.list_fill_activities(
        after=_timestamp(after),
        until=_timestamp(until),
    )
    for activity in activities[:1000]:
        order = broker.get_order(order_id=activity.order_id)
        if order.client_order_id != client_order_id:
            continue
        merged = _order_dict(order)
        if not merged["filled_at"]:
            merged["filled_at"] = activity.transaction_time
        if not merged["filled_qty"]:
            merged["filled_qty"] = activity.cumulative_quantity or activity.quantity
        if not merged["filled_avg_price"]:
            merged["filled_avg_price"] = activity.price
        return merged
    return None


def _order_dict(order: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.order_type,
        "time_in_force": order.time_in_force,
        "status": order.status,
        "qty": order.quantity,
        "filled_qty": order.filled_quantity,
        "filled_avg_price": order.filled_avg_price,
        "filled_at": order.filled_at,
        "realized_pnl": order.realized_pnl,
        "submitted_at": order.submitted_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("paper statement search timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("paper statement search timestamp must include timezone")
    return parsed.astimezone(UTC)


def _statement_row(order: dict[str, object]) -> dict[str, object]:
    side = _enum_text(order.get("side")).lower()
    realized_pnl = order.get("realized_pnl")
    source = "alpaca_paper_executor"
    if realized_pnl in {None, ""} and side == "buy":
        realized_pnl = "0.0"
        source = "alpaca_paper_executor_realized_pnl_unavailable"
    return {
        "client_order_id": _text(order.get("client_order_id")),
        "symbol": _text(order.get("symbol")).upper(),
        "side": side,
        "quantity": _text(order.get("filled_qty") or order.get("qty")),
        "filled_avg_price": _text(order.get("filled_avg_price")),
        "filled_at": _text(order.get("filled_at")),
        "realized_pnl": _text(realized_pnl),
        "source": source,
        "broker_order_id": _text(order.get("id")),
        "broker_status": _enum_text(order.get("status")).lower(),
    }


def _row_errors(row: dict[str, object], *, as_of_date: str) -> list[str]:
    errors: list[str] = []
    for field in ("client_order_id", "symbol", "side", "quantity", "filled_avg_price", "filled_at", "realized_pnl"):
        if row.get(field) in {None, ""}:
            errors.append(f"missing_{field}")
    status = str(row.get("broker_status") or "").lower()
    if status not in {"filled", "partially_filled"}:
        errors.append(f"broker_status_{status or 'missing'}")
    filled_at = str(row.get("filled_at") or "")
    if filled_at[:10] != as_of_date:
        errors.append(f"filled_at_outside_as_of_date:{filled_at[:10] or 'missing'}")
    for numeric_field in ("quantity", "filled_avg_price", "realized_pnl"):
        try:
            float(str(row.get(numeric_field)))
        except (TypeError, ValueError):
            errors.append(f"invalid_{numeric_field}")
    return errors


def _redacted_order(order: dict[str, object]) -> dict[str, object]:
    allowed = {
        "id",
        "client_order_id",
        "symbol",
        "side",
        "type",
        "time_in_force",
        "status",
        "qty",
        "filled_qty",
        "filled_avg_price",
        "filled_at",
        "submitted_at",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in order.items() if key in allowed}


def _enum_text(value: object) -> str:
    text = _text(getattr(value, "value", value))
    return text.rsplit(".", 1)[-1] if "." in text else text


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _default_after(client_order_id: str, as_of_date: str) -> str:
    match = re.search(r"(20\d{6})", client_order_id)
    if match is not None:
        raw = match.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}T00:00:00Z"
    return f"{as_of_date}T00:00:00Z"


if __name__ == "__main__":
    raise SystemExit(main())
