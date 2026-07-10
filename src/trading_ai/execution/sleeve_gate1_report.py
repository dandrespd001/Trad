"""Aggregate daily sleeve-rebalance artifacts into a Gate 1 scorecard (M5).

The governed sleeve-rebalance cycle (M3) writes one ``cycle_<sleeve>_<date>.json``
per sleeve per day plus an ``allocation_<date>.json``. As the operator
accumulates Gate 1 paper days, this module rolls them up into a single
scorecard with orders, fills, effective cost per trade, PnL proxy, and any
incidents — without ever submitting orders (read-only against the broker).

Design notes
------------
- Fail-soft per artifact: a corrupt or missing cycle file is recorded as an
  ``incident`` (``unreadable:<name>``) and does not crash the report.
- Cost-effective cost is measured ONLY when both the broker-side fill and the
  snapshot ``reference_price`` are available and positive — otherwise the
  observation is excluded from the distribution (preserves sample quality for
  the §30 sensitivity margin discussion).
- Status mapping mirrors the rest of the paper fleet: ``BLOCKED`` for zero
  cycles, ``WARN`` if any cycle ``BLOCKED`` or any incident recorded, else
  ``OK``. ``exit_code`` follows ``paper_exit_code``.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"

# Cycle files emitted by ``run_sleeve_rebalance`` follow this naming. The
# ``allocation`` files use a slightly different shape and are picked up by a
# separate pattern.
_CYCLE_PATTERN = re.compile(r"^cycle_([a-z0-9_]+)_(\d{4}-\d{2}-\d{2})\.json$")
_ALLOCATION_PATTERN = re.compile(r"^allocation_(\d{4}-\d{2}-\d{2})\.json$")
# Fallback for top-level daily cycle files (no sleeve prefix).
_CYCLE_DAILY_PATTERN = re.compile(r"^cycle_(\d{4}-\d{2}-\d{2})\.json$")


@dataclass(frozen=True)
class Gate1ReportResult:
    exit_code: int
    status: str  # "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def _coerce_float(value: object, *, default: float | None = None) -> float | None:
    """Tolerantly coerce to ``float``; ``None`` if missing or non-numeric."""
    if value is None or value == "":
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result


def _coerce_position_value(position: Any, attribute: str, *, default: Any = None) -> Any:
    """Pull ``attribute`` from a position that may be a dict or a duck-typed object."""
    if isinstance(position, Mapping):
        return position.get(attribute, default)
    return getattr(position, attribute, default)


def _scan_cycle_files(cycles_dir: Path) -> tuple[list[tuple[str, date, Path]], list[tuple[date, Path]]]:
    """Return (cycle_records, allocation_records) discovered in ``cycles_dir``.

    Files whose name does not match the expected patterns are ignored
    silently — they belong to other commands and must not influence the
    scorecard.
    """
    cycles: list[tuple[str, date, Path]] = []
    allocations: list[tuple[date, Path]] = []
    if not cycles_dir.exists() or not cycles_dir.is_dir():
        return cycles, allocations
    for entry in sorted(cycles_dir.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        match = _CYCLE_PATTERN.match(entry.name)
        if match:
            try:
                sleeve_date = date.fromisoformat(match.group(2))
            except ValueError:
                continue
            cycles.append((match.group(1), sleeve_date, entry))
            continue
        match = _ALLOCATION_PATTERN.match(entry.name)
        if match:
            try:
                allocation_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            allocations.append((allocation_date, entry))
            continue
        match = _CYCLE_DAILY_PATTERN.match(entry.name)
        if match:
            try:
                daily_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            cycles.append(("daily", daily_date, entry))
    return cycles, allocations


def _in_window(target: date, start: date | None, end: date | None) -> bool:
    if start is not None and target < start:
        return False
    if end is not None and target > end:
        return False
    return True


def _extract_plan_reference_prices(plan: object) -> dict[str, float]:
    """Map ``symbol/pair -> reference_price`` from a cycle ``plan`` list."""
    result: dict[str, float] = {}
    if not isinstance(plan, list):
        return result
    for entry in plan:
        if not isinstance(entry, Mapping):
            continue
        symbol = str(entry.get("pair") or entry.get("symbol") or "").upper()
        if not symbol:
            continue
        price = _coerce_float(entry.get("reference_price"))
        if price is not None and price > 0:
            result[symbol] = price
    return result


def _lookup_submission_order(
    broker: Any,
    client_order_id: str,
) -> tuple[Mapping[str, object] | None, str | None]:
    """Return ``(snapshot_dict, error_label)``.

    The snapshot is a plain dict the caller can serialize, or ``None`` if the
    broker lookup failed. ``error_label`` carries the reason when the lookup
    raised — it is the ``order_lookup_failed:<cid>`` incident slug.
    """
    try:
        snapshot = broker.get_order_by_client_id(client_order_id)
    except Exception as exc:  # noqa: BLE001
        return None, f"order_lookup_failed:{client_order_id}:{type(exc).__name__}:{exc}"
    if snapshot is None:
        return None, f"order_lookup_failed:{client_order_id}:not_found"
    if isinstance(snapshot, Mapping):
        return dict(snapshot), None
    # Duck-typed broker: convert to a plain dict.
    serialized: dict[str, object] = {}
    for attribute in (
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "status",
        "notional",
        "quantity",
        "filled_quantity",
        "filled_avg_price",
        "submitted_at",
        "created_at",
        "updated_at",
        "expires_at",
    ):
        serialized[attribute] = getattr(snapshot, attribute, None)
    return serialized, None


def _summarize_fills(
    effective_costs: list[float],
) -> dict[str, object]:
    if not effective_costs:
        return {"min": None, "median": None, "max": None, "n": 0}
    return {
        "min": round(min(effective_costs), 2),
        "median": round(float(statistics.median(effective_costs)), 2),
        "max": round(max(effective_costs), 2),
        "n": len(effective_costs),
    }


def _render_markdown(payload: Mapping[str, object]) -> str:
    """Render a human-readable scorecard from the JSON payload."""
    status = str(payload.get("status") or "")
    window = payload.get("window") or {}
    start = str(window.get("start") or "—")
    end = str(window.get("end") or "—")
    days = payload.get("days") or []
    n_cycles = payload.get("n_cycles") or 0
    by_status = payload.get("by_status") or {}
    fills = payload.get("fills") or []
    costs = payload.get("effective_cost_bps") or {}
    incidents = payload.get("incidents") or []
    positions = payload.get("positions") or []
    equity = payload.get("account_equity")
    safety = payload.get("safety") or {}

    lines: list[str] = []
    lines.append(f"# Gate 1 Scorecard — {status}")
    lines.append("")
    lines.append(f"- Window: {start} → {end}")
    lines.append(f"- Days with cycles: **{len(days)}** ({', '.join(days) if days else 'none'})")
    lines.append(f"- Cycles: {n_cycles}")
    lines.append(f"- By status: {by_status}")
    lines.append(
        f"- Safety: read_only={safety.get('read_only')} "
        f"orders_submitted={safety.get('orders_submitted')}"
    )
    if equity is not None:
        lines.append(f"- Account equity (USD): {equity}")
    lines.append("")
    if isinstance(payload.get("per_day"), list) and payload["per_day"]:
        lines.append("## Per-day")
        lines.append("")
        lines.append("| Date | Sleeves | Statuses | Submitted | Errored |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in payload["per_day"]:  # type: ignore[union-attr]
            sleeves = ",".join(str(s) for s in (entry.get("sleeves") or []))
            statuses = ",".join(
                f"{item.get('sleeve')}={item.get('status')}"
                for item in (entry.get("sleeves_status") or [])
            )
            lines.append(
                f"| {entry.get('date')} | {sleeves} | {statuses} | "
                f"{entry.get('orders_submitted', 0)} | {entry.get('orders_errored', 0)} |"
            )
        lines.append("")
    if fills:
        lines.append("## Fills")
        lines.append("")
        lines.append("| Date | Sleeve | Symbol | Side | Qty | Avg Price | Reference | Cost (bps) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for fill in fills:
            lines.append(
                f"| {fill.get('date')} | {fill.get('sleeve')} | {fill.get('symbol')} | "
                f"{fill.get('side')} | {fill.get('filled_quantity')} | "
                f"{fill.get('filled_avg_price')} | {fill.get('reference_price')} | "
                f"{fill.get('effective_cost_bps')} |"
            )
        lines.append("")
    lines.append("## Effective cost (bps)")
    lines.append("")
    lines.append(f"- n={costs.get('n')} min={costs.get('min')} median={costs.get('median')} max={costs.get('max')}")
    lines.append("")
    if positions:
        lines.append("## Positions (snapshot)")
        lines.append("")
        lines.append("| Symbol | Qty | Market Value | Unrealized PnL |")
        lines.append("| --- | --- | --- | --- |")
        for position in positions:
            lines.append(
                f"| {position.get('symbol')} | {position.get('qty')} | "
                f"{position.get('market_value')} | {position.get('unrealized_pl')} |"
            )
        lines.append("")
    if incidents:
        lines.append("## Incidents")
        lines.append("")
        for incident in incidents:
            lines.append(f"- {incident}")
        lines.append("")
    return "\n".join(lines)


def run_gate1_report(
    *,
    cycles_dir: str | Path,
    output: str | Path,
    markdown_output: str | Path | None = None,
    start: date | None = None,
    end: date | None = None,
    broker: Any | None = None,
    generated_at: str | None = None,
) -> Gate1ReportResult:
    """Build the Gate 1 scorecard from the artifacts in ``cycles_dir``."""

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    cycles_root = Path(cycles_dir)
    if start is not None and end is not None and start > end:
        # A backwards window is a degenerate input — surface it as a
        # BLOCKED report so the caller cannot mistake empty data for "all
        # clean".
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "cycles_dir": str(cycles_root),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "status": PAPER_BLOCKED,
            "incidents": ["invalid_window:start_after_end"],
            "days": [],
            "n_cycles": 0,
            "by_status": {},
            "per_day": [],
            "fills": [],
            "effective_cost_bps": {"min": None, "median": None, "max": None, "n": 0},
            "positions": [],
            "account_equity": None,
            "safety": {"read_only": True, "orders_submitted": False},
        }
        write_json_artifact(payload, output_path)
        return Gate1ReportResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    cycles, allocations = _scan_cycle_files(cycles_root)
    cycles = [
        (sleeve, cycle_date, path)
        for sleeve, cycle_date, path in cycles
        if _in_window(cycle_date, start, end)
    ]
    allocations = [
        (allocation_date, path)
        for allocation_date, path in allocations
        if _in_window(allocation_date, start, end)
    ]

    incidents: list[str] = []
    by_status: dict[str, int] = {}
    fills: list[dict[str, object]] = []
    effective_costs: list[float] = []
    per_day_map: dict[str, dict[str, object]] = {}

    for sleeve, cycle_date, path in cycles:
        date_key = cycle_date.isoformat()
        try:
            payload_cycle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            incidents.append(f"unreadable:{path.name}:{type(exc).__name__}:{exc}")
            continue
        if not isinstance(payload_cycle, Mapping):
            incidents.append(f"unreadable:{path.name}:not_a_mapping")
            continue

        cycle_status = str(payload_cycle.get("status") or "UNKNOWN")
        by_status[cycle_status] = by_status.get(cycle_status, 0) + 1

        plan_reference_prices = _extract_plan_reference_prices(payload_cycle.get("plan"))
        submissions = payload_cycle.get("submissions") or []
        if not isinstance(submissions, list):
            submissions = []
        submitted_count = 0
        errored_count = 0
        for submission in submissions:
            if not isinstance(submission, Mapping):
                continue
            is_submitted = bool(submission.get("submitted"))
            submission_status = str(submission.get("status") or "")
            if is_submitted:
                submitted_count += 1
            if submission_status == "error":
                errored_count += 1
            if not is_submitted:
                continue
            client_order_id = submission.get("client_order_id")
            if not isinstance(client_order_id, str) or not client_order_id:
                continue
            if broker is None:
                continue
            snapshot, error_label = _lookup_submission_order(broker, client_order_id)
            if error_label is not None:
                incidents.append(error_label)
                continue
            assert snapshot is not None  # for type-checkers
            fill_record: dict[str, object] = {
                "date": date_key,
                "sleeve": sleeve,
                "client_order_id": client_order_id,
                "symbol": snapshot.get("symbol") or submission.get("pair"),
                "side": snapshot.get("side"),
                "order_status": snapshot.get("status"),
                "filled_quantity": snapshot.get("filled_quantity"),
                "filled_avg_price": snapshot.get("filled_avg_price"),
                "notional": snapshot.get("notional"),
            }
            pair_key = str(fill_record.get("symbol") or submission.get("pair") or "").upper()
            reference_price = plan_reference_prices.get(pair_key)
            fill_record["reference_price"] = reference_price
            filled_avg_price = _coerce_float(snapshot.get("filled_avg_price"))
            if (
                filled_avg_price is not None
                and reference_price is not None
                and reference_price > 0
                and str(snapshot.get("status") or "").lower() == "filled"
            ):
                cost_bps = abs(filled_avg_price - reference_price) / reference_price * 10_000
                fill_record["effective_cost_bps"] = round(cost_bps, 2)
                effective_costs.append(cost_bps)
            fills.append(fill_record)

        day_entry = per_day_map.setdefault(
            date_key,
            {
                "date": date_key,
                "sleeves": [],
                "sleeves_status": [],
                "orders_submitted": 0,
                "orders_errored": 0,
            },
        )
        if sleeve not in day_entry["sleeves"]:  # type: ignore[operator]
            day_entry["sleeves"].append(sleeve)  # type: ignore[union-attr]
        day_entry["sleeves_status"].append(  # type: ignore[union-attr]
            {"sleeve": sleeve, "status": cycle_status}
        )
        day_entry["orders_submitted"] = int(day_entry["orders_submitted"]) + submitted_count  # type: ignore[arg-type]
        day_entry["orders_errored"] = int(day_entry["orders_errored"]) + errored_count  # type: ignore[arg-type]

    # Day-level aggregates are sorted for deterministic output.
    per_day = [per_day_map[key] for key in sorted(per_day_map.keys())]
    days = sorted(per_day_map.keys())
    n_cycles = sum(1 for _ in cycles)

    positions_out: list[dict[str, object]] = []
    account_equity: float | None = None
    if broker is not None:
        try:
            positions_raw = broker.read_positions()
        except Exception as exc:  # noqa: BLE001
            incidents.append(f"positions_read_failed:{type(exc).__name__}:{exc}")
            positions_raw = []
        for position in positions_raw or []:
            unrealized = _coerce_position_value(position, "unrealized_pl")
            entry_position: dict[str, object] = {
                "symbol": _coerce_position_value(position, "symbol"),
                "qty": _coerce_position_value(position, "qty"),
                "market_value": _coerce_position_value(position, "market_value"),
                "unrealized_pl": unrealized,
            }
            positions_out.append(entry_position)
        try:
            account = broker.read_account()
        except Exception as exc:  # noqa: BLE001
            incidents.append(f"account_read_failed:{type(exc).__name__}:{exc}")
            account = None
        if account is not None:
            account_equity = _coerce_float(getattr(account, "equity", None))

    # Status routing: BLOCKED if zero cycles; WARN if any cycle BLOCKED or
    # any incident recorded; OK otherwise.
    if n_cycles == 0 and not allocations:
        status = PAPER_BLOCKED
        incidents.append("no_cycles_found")
    elif incidents or by_status.get(PAPER_BLOCKED, 0) > 0:
        status = PAPER_WARN
    else:
        status = PAPER_OK

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "cycles_dir": str(cycles_root),
        "window": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "days": days,
        "n_cycles": n_cycles,
        "by_status": dict(sorted(by_status.items())),
        "per_day": per_day,
        "fills": fills,
        "effective_cost_bps": _summarize_fills(effective_costs),
        "incidents": incidents,
        "positions": positions_out,
        "account_equity": account_equity,
        "status": status,
        "safety": {"read_only": True, "orders_submitted": False},
    }
    write_json_artifact(payload, output_path)
    if markdown_output is not None:
        write_text_artifact(_render_markdown(payload), markdown_output)
    return Gate1ReportResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


__all__ = ["Gate1ReportResult", "SCHEMA_VERSION", "run_gate1_report"]