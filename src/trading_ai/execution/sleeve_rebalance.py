"""Governed crypto-sleeve rebalance cycle (paper-only by default).

This module bridges the validated momentum-vol-target strategy (§28) to the
broker paper account: it reads fresh OHLCV data, builds the causal target
weights snapshot via :func:`compute_target_weights_snapshot`, and produces a
per-symbol rebalance plan against the broker's live positions. Orders are
report-only by default and only submitted when ``confirm_submit`` is passed
together with a non-``None`` broker (the CLI doubles down on opt-in via
``--real-paper --confirm-paper --confirm-auto-submit``).

Default behavior is fail-closed: any validation, freshness, or history
problem blocks the cycle and the payload says so explicitly. The payload's
``safety`` block is the single source of truth for whether real orders were
sent (``paper_only`` is always true because the live adapter is not wired
in this sprint).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.backtest.engine import BacktestConfig, compute_target_weights_snapshot
from trading_ai.config import ConfigError, load_risk_config, load_universe_config
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.alpaca_paper import PaperOrder
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
)

SCHEMA_VERSION = "1.0"
CRYPTO_MIN_NOTIONAL_USD_DEFAULT = 10.0
MIN_DELTA_USD = 1.0
NOISE_DELTA_USD = 1.0


@dataclass(frozen=True)
class SleeveRebalanceResult:
    exit_code: int
    status: str  # "REPORT_ONLY" | "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def map_broker_symbol_to_pair(symbol: str, universe_symbols: Iterable[str]) -> str | None:
    """Map an Alpaca-style broker symbol ("BTCUSD") to its universe pair ("BTC/USD")."""
    by_compact = {pair.replace("/", ""): pair for pair in universe_symbols}
    return by_compact.get(symbol.upper())


def _coerce_float_market_value(position: object, *, default: float = 0.0) -> float:
    """Tolerantly pull ``market_value`` from a broker position (dict or attribute)."""
    if isinstance(position, dict):
        raw = position.get("market_value", default)
    else:
        raw = getattr(position, "market_value", default)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)


def _read_broker_positions_by_pair(broker: Any, universe_symbols: Iterable[str]) -> tuple[dict[str, float], list[str]]:
    """Return ``({pair: market_value}, [ignored_symbol, ...])`` from the broker.

    Symbols that do not map back to a universe pair are IGNORED (reported in
    ``ignored_positions``) rather than rebalanced — the sleeve cycle is
    scoped to the configured universe.
    """
    raw_positions = broker.read_positions()
    current_by_pair: dict[str, float] = {}
    ignored: list[str] = []
    universe_set = list(universe_symbols)
    for position in raw_positions:
        if isinstance(position, dict):
            broker_symbol = str(position.get("symbol", "")).upper()
        else:
            broker_symbol = str(getattr(position, "symbol", "")).upper()
        if not broker_symbol:
            continue
        pair = map_broker_symbol_to_pair(broker_symbol, universe_set)
        if pair is None:
            ignored.append(broker_symbol)
            continue
        current_by_pair[pair] = _coerce_float_market_value(position, default=0.0)
    return current_by_pair, ignored


def _last_close(close_by_symbol: dict[str, dict[str, float]], symbol: str, dates: list[str]) -> float | None:
    closes = close_by_symbol.get(symbol)
    if not closes or not dates:
        return None
    for ts in reversed(dates):
        if ts in closes:
            return float(closes[ts])
    return None


def _build_close_by_symbol(records: Iterable[dict[str, object]]) -> dict[str, dict[str, float]]:
    by_symbol: dict[str, dict[str, float]] = {}
    for row in records:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        timestamp = str(row.get("timestamp", ""))
        close_value = row.get("close")
        if close_value is None or close_value == "":
            continue
        try:
            by_symbol.setdefault(symbol, {})[timestamp] = float(close_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return by_symbol


def _build_close_by_symbol_from_engine(records: Iterable[dict[str, object]]) -> tuple[dict[str, dict[str, float]], list[str]]:
    grouped = _build_close_by_symbol(records)
    dates = sorted({ts for closes in grouped.values() for ts in closes})
    return grouped, dates


def _build_plan_entry(
    *,
    pair: str,
    weight: float,
    current_value: float,
    target_notional: float,
    reference_price: float | None,
    position_qty: float | None = None,
    min_notional: float = CRYPTO_MIN_NOTIONAL_USD_DEFAULT,
) -> dict[str, object]:
    delta = target_notional - current_value
    abs_delta = abs(delta)
    if abs_delta < NOISE_DELTA_USD:
        action = "hold"
        notional: float | None = round(target_notional, 2)
        quantity: float | None = None
        return {
            "pair": pair,
            "action": action,
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional,
            "quantity": quantity,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    if target_notional <= 0.0 and current_value > 0.0:
        # Full exit — close by exact quantity, no min-notional hop.
        quantity_value = float(position_qty) if position_qty is not None else None
        return {
            "pair": pair,
            "action": "sell_all",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": round(current_value, 2),
            "quantity": quantity_value,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    if delta > 0:
        notional_amount = round(delta, 2)
        if notional_amount < min_notional:
            return {
                "pair": pair,
                "action": "skip_below_min",
                "target_notional": round(target_notional, 2),
                "current_notional": round(current_value, 2),
                "delta": round(delta, 2),
                "notional": notional_amount,
                "quantity": None,
                "reference_price": reference_price,
                "weight": round(weight, 6),
                "skip_reason": "below_crypto_min_notional",
            }
        return {
            "pair": pair,
            "action": "buy",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional_amount,
            "quantity": None,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    # delta < 0, target > 0 → partial sell
    notional_amount = round(-delta, 2)
    if notional_amount < min_notional:
        return {
            "pair": pair,
            "action": "skip_below_min",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional_amount,
            "quantity": None,
            "reference_price": reference_price,
            "weight": round(weight, 6),
            "skip_reason": "below_crypto_min_notional",
        }
    return {
        "pair": pair,
        "action": "sell",
        "target_notional": round(target_notional, 2),
        "current_notional": round(current_value, 2),
        "delta": round(delta, 2),
        "notional": notional_amount,
        "quantity": None,
        "reference_price": reference_price,
        "weight": round(weight, 6),
    }


def _execute_submissions(
    *,
    plan: list[dict[str, object]],
    broker: Any,
    as_of_date: date,
    universe_name: str,
) -> list[dict[str, object]]:
    submissions: list[dict[str, object]] = []
    for entry in plan:
        action = str(entry.get("action"))
        if action not in {"buy", "sell", "sell_all"}:
            submissions.append(
                {
                    "pair": entry.get("pair"),
                    "action": action,
                    "submitted": False,
                    "skipped": True,
                    "status": "skipped",
                    "reasons": ("action_does_not_submit",),
                }
            )
            continue
        pair = str(entry.get("pair"))
        side = "buy" if action == "buy" else "sell"
        notional_value = entry.get("notional")
        quantity_value = entry.get("quantity")
        reference_price = entry.get("reference_price")
        client_order_id = f"sleeve-{as_of_date.isoformat()}-{pair.replace('/', '')}-{side}"
        order_kwargs: dict[str, Any] = {
            "symbol": pair,
            "side": side,
            "client_order_id": client_order_id,
            "reference_price": reference_price,
        }
        if action == "sell_all" and quantity_value is not None:
            order_kwargs["quantity"] = float(quantity_value)
        else:
            if notional_value is None:
                submissions.append(
                    {
                        "pair": pair,
                        "action": action,
                        "submitted": False,
                        "skipped": True,
                        "status": "skipped",
                        "reasons": ("missing_notional",),
                    }
                )
                continue
            order_kwargs["notional"] = float(notional_value)
        order = PaperOrder(**order_kwargs)
        result = broker.submit_order(order)
        accepted_attr = getattr(result, "accepted", False)
        status_attr = getattr(result, "status", "unknown")
        reasons_attr = getattr(result, "reasons", ())
        submissions.append(
            {
                "pair": pair,
                "action": action,
                "client_order_id": client_order_id,
                "submitted": bool(accepted_attr),
                "skipped": False,
                "status": str(status_attr),
                "reasons": list(reasons_attr) if isinstance(reasons_attr, (tuple, list)) else [str(reasons_attr)],
            }
        )
    # Reference the universe through a closure-captured local to keep the
    # function signature honest; suppress unused warnings.
    _ = universe_name
    return submissions


def _exit_code_for_status(status: str) -> int:
    base = paper_exit_code(status)
    if status == "REPORT_ONLY":
        return 0
    return base


def run_sleeve_rebalance(
    *,
    universe_config: str | Path,
    risk_config: str | Path,
    dataset: str | Path,
    output: str | Path,
    notional_usd: float,
    momentum_window: int = 120,
    periods_per_year: int = 365,
    max_single_position: float = 0.10,
    max_age_days: int = 3,
    as_of_date: date | None = None,
    broker: Any | None = None,
    confirm_submit: bool = False,
    generated_at: str | None = None,
) -> SleeveRebalanceResult:
    """Run the governed crypto-sleeve rebalance cycle (report-only by default)."""

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()
    blockers: list[str] = []

    if notional_usd <= 0:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": ["invalid_notional_budget"],
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": False,
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    try:
        universe = load_universe_config(universe_config)
        risk = load_risk_config(risk_config, allow_live=False)
    except ConfigError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": [f"config_error:{exc}"],
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    try:
        raw_records = read_records(dataset)
    except (OSError, ValueError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "blockers": [f"dataset_unreadable:{exc}"],
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    validation = validate_ohlcv_records(raw_records, allowed_symbols=universe.symbols)
    if not validation.valid:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "blockers": list(validation.errors),
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    close_by_symbol, dates = _build_close_by_symbol_from_engine(raw_records)
    if not dates:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "blockers": ["empty_dataset"],
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    last_date_in_dataset = date.fromisoformat(dates[-1][:10]) if len(dates[-1]) >= 10 else None
    if last_date_in_dataset is None:
        blockers.append("dataset_stale:unparseable_last_date")
    else:
        age_days = (as_of - last_date_in_dataset).days
        if age_days > max_age_days:
            blockers.append(f"dataset_stale:{dates[-1]}")

    snapshot = compute_target_weights_snapshot(
        raw_records,
        BacktestConfig(
            momentum_window=momentum_window,
            volatility_window=momentum_window,
            periods_per_year=periods_per_year,
            max_single_position=max_single_position,
        ),
    )
    if not snapshot["sufficient_history"]:
        blockers.append("insufficient_history")

    if blockers:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "blockers": blockers,
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    raw_weights = snapshot["weights"]
    weights = {str(symbol).upper(): float(value) for symbol, value in raw_weights.items()}  # type: ignore[union-attr]
    as_of_iso = str(snapshot["as_of"])

    if broker is None:
        current_by_pair: dict[str, float] = {}
        ignored_positions: list[str] = []
        position_qty_by_pair: dict[str, float] = {}
    else:
        current_by_pair, ignored_positions = _read_broker_positions_by_pair(broker, universe.symbols)
        # Recover raw quantities so sell_all actions carry exact qty. The
        # broker tuple is duck-typed (SimpleNamespace / PaperPosition / dict),
        # so we coerce tolerant of all three.
        position_qty_by_pair = {}
        for position in broker.read_positions():
            if isinstance(position, dict):
                broker_symbol = str(position.get("symbol", "")).upper()
                qty_raw = position.get("qty")
                if qty_raw is None:
                    qty_raw = position.get("quantity")
            else:
                broker_symbol = str(getattr(position, "symbol", "")).upper()
                qty_raw = getattr(position, "qty", None)
                if qty_raw is None:
                    qty_raw = getattr(position, "quantity", None)
            if not broker_symbol or qty_raw is None or qty_raw == "":
                continue
            pair = map_broker_symbol_to_pair(broker_symbol, universe.symbols)
            if pair is not None:
                try:
                    position_qty_by_pair[pair] = float(qty_raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue

    plan: list[dict[str, object]] = []
    all_pairs = sorted({pair.upper() for pair in universe.symbols} | set(weights) | set(current_by_pair))
    for pair in all_pairs:
        weight = weights.get(pair, 0.0)
        target_notional = float(weight) * notional_usd
        current_value = float(current_by_pair.get(pair, 0.0))
        reference_price = _last_close(close_by_symbol, pair, dates)
        if weight > 0:
            entry = _build_plan_entry(
                pair=pair,
                weight=weight,
                current_value=current_value,
                target_notional=target_notional,
                reference_price=reference_price,
                position_qty=position_qty_by_pair.get(pair),
            )
        else:
            entry = _build_plan_entry(
                pair=pair,
                weight=0.0,
                current_value=current_value,
                target_notional=0.0,
                reference_price=reference_price,
                position_qty=position_qty_by_pair.get(pair),
            )
        plan.append(entry)

    orders_submitted = False
    submissions: list[dict[str, object]] = []
    if confirm_submit and broker is not None:
        submissions = _execute_submissions(
            plan=plan,
            broker=broker,
            as_of_date=as_of,
            universe_name=universe.name,
        )
        # orders_submitted must reflect what actually reached the broker: an
        # all-hold/skip plan under confirm_submit sends nothing and must say
        # so. Rejected orders still count — they were attempted.
        orders_submitted = any(not submission.get("skipped", False) for submission in submissions)
        any_rejected = any(
            (not submission.get("submitted", False)) and not submission.get("skipped", False)
            for submission in submissions
        )
        status = PAPER_WARN if any_rejected else PAPER_OK
    else:
        status = "REPORT_ONLY"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of_iso,
        "universe": universe.name,
        "dataset": str(dataset),
        "params": {
            "notional_usd": float(notional_usd),
            "momentum_window": int(momentum_window),
            "periods_per_year": int(periods_per_year),
            "max_single_position": float(max_single_position),
            "max_age_days": int(max_age_days),
        },
        "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
        "plan": plan,
        "ignored_positions": sorted(set(ignored_positions)),
        "submissions": submissions,
        "blockers": blockers,
        "status": status,
        "safety": {
            "paper_only": True,
            "orders_submitted": bool(orders_submitted),
            "confirm_submit": bool(confirm_submit),
            "live_trading_authorized": False,
        },
    }
    write_json_artifact(payload, output_path)

    exit_code = _exit_code_for_status(status)
    return SleeveRebalanceResult(
        exit_code=exit_code,
        status=status,
        output_path=output_path,
        payload=payload,
    )
