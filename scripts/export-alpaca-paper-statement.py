#!/usr/bin/env python3
"""Export a paper-only Alpaca order fill as a statement CSV.

This is intentionally read-only: it calls Alpaca's paper Trading API order
lookup endpoints and writes only the normalized fields expected by
``paper-statement-validate``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PAPER_BASE_URL = "https://paper-api.alpaca.markets/v2"
REQUIRED_ENV = ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--client-order-id", required=True)
    parser.add_argument("--order-id")
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--raw-output")
    parser.add_argument("--search-after")
    parser.add_argument("--search-until")
    args = parser.parse_args()

    env = _load_env(Path(args.env_file))
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        print("missing Alpaca paper credential environment variables: " + ", ".join(missing), file=sys.stderr)
        return 2

    try:
        order = _find_order(
            env=env,
            client_order_id=args.client_order_id,
            order_id=args.order_id,
            as_of_date=args.as_of_date,
            search_after=args.search_after,
            search_until=args.search_until,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
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


def _load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def _get_json(url: str, *, env: dict[str, str], params: dict[str, str]) -> object:
    request_url = f"{url}?{urlencode(params)}"
    request = Request(
        request_url,
        headers={
            "APCA-API-KEY-ID": env["ALPACA_PAPER_API_KEY"],
            "APCA-API-SECRET-KEY": env["ALPACA_PAPER_SECRET_KEY"],
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Alpaca paper API returned HTTP {exc.code}: {_safe_error_body(body)}") from exc
    except URLError as exc:
        raise RuntimeError(f"Alpaca paper API request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("Alpaca paper API request timed out") from exc


def _find_order(
    *,
    env: dict[str, str],
    client_order_id: str,
    order_id: str | None,
    as_of_date: str,
    search_after: str | None,
    search_until: str | None,
) -> dict[str, object]:
    if order_id:
        order = _get_json(f"{PAPER_BASE_URL}/orders/{quote(order_id)}", env=env, params={})
        if not isinstance(order, dict):
            raise RuntimeError("Alpaca order-id lookup returned an unexpected response")
        broker_client_order_id = str(order.get("client_order_id") or "")
        if broker_client_order_id != client_order_id:
            raise RuntimeError(
                f"Alpaca order-id lookup mismatch: expected {client_order_id}, got {broker_client_order_id or 'missing'}"
            )
        return order

    try:
        direct = _get_json(
            f"{PAPER_BASE_URL}/orders:by_client_order_id",
            env=env,
            params={"client_order_id": client_order_id},
        )
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
    else:
        if isinstance(direct, dict):
            return direct

    after = search_after or _default_after(client_order_id, as_of_date)
    until = search_until or f"{as_of_date}T23:59:59Z"
    orders = _get_json(
        f"{PAPER_BASE_URL}/orders",
        env=env,
        params={
            "status": "all",
            "limit": "500",
            "direction": "asc",
            "after": after,
            "until": until,
        },
    )
    if not isinstance(orders, list):
        raise RuntimeError("Alpaca order search returned an unexpected response")
    matches = [
        order
        for order in orders
        if isinstance(order, dict) and str(order.get("client_order_id") or "") == client_order_id
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Alpaca paper API returned duplicate orders for {client_order_id}")
    if len(matches) == 1:
        return matches[0]

    activity_match = _find_order_from_fill_activities(
        env=env,
        client_order_id=client_order_id,
        after=after,
        until=until,
    )
    if activity_match is not None:
        return activity_match

    raise RuntimeError(
        f"Alpaca paper API returned no order for {client_order_id}; "
        f"orders_search_count={len(orders)} fill_activity_match=false search={after}..{until}"
    )


def _find_order_from_fill_activities(
    *,
    env: dict[str, str],
    client_order_id: str,
    after: str,
    until: str,
) -> dict[str, object] | None:
    activities = _get_fill_activities(env=env, after=after, until=until)
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        order_id = str(activity.get("order_id") or "")
        if not order_id:
            continue
        try:
            order = _get_json(f"{PAPER_BASE_URL}/orders/{quote(order_id)}", env=env, params={})
        except RuntimeError:
            continue
        if not isinstance(order, dict):
            continue
        if str(order.get("client_order_id") or "") != client_order_id:
            continue
        merged = dict(order)
        merged.setdefault("filled_at", activity.get("transaction_time") or activity.get("date"))
        merged.setdefault("filled_qty", activity.get("qty") or activity.get("cum_qty"))
        merged.setdefault("filled_avg_price", activity.get("price"))
        return merged
    return None


def _get_fill_activities(*, env: dict[str, str], after: str, until: str) -> list[object]:
    activities: list[object] = []
    page_token = ""
    for _ in range(10):
        params = {
            "after": after,
            "until": until,
            "direction": "asc",
            "page_size": "100",
        }
        if page_token:
            params["page_token"] = page_token
        page = _get_json(f"{PAPER_BASE_URL}/account/activities/FILL", env=env, params=params)
        if not isinstance(page, list) or not page:
            break
        activities.extend(page)
        last = page[-1]
        if not isinstance(last, dict):
            break
        next_token = str(last.get("id") or "")
        if not next_token or next_token == page_token:
            break
        page_token = next_token
        if len(page) < 100:
            break
    return activities


def _statement_row(order: dict[str, object]) -> dict[str, object]:
    side = _enum_text(order.get("side")).lower()
    realized_pnl = order.get("realized_pnl")
    source = "alpaca_paper_orders_api"
    if realized_pnl in {None, ""} and side == "buy":
        realized_pnl = "0.0"
        source = "alpaca_paper_orders_api_realized_pnl_unavailable"
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


def _safe_error_body(body: str) -> str:
    if len(body) > 500:
        body = body[:500] + "..."
    return body.replace("\n", " ")


def _default_after(client_order_id: str, as_of_date: str) -> str:
    match = re.search(r"(20\d{6})", client_order_id)
    if match is not None:
        raw = match.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}T00:00:00Z"
    return f"{as_of_date}T00:00:00Z"


if __name__ == "__main__":
    raise SystemExit(main())
