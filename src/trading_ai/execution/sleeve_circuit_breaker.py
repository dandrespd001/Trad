"""Governed escalation of the sleeve risk kill-switches (Sprint M11, §33).

When the daily-loss or drawdown kill-switch trips on a real broker account,
the existing M7 behavior only blocks NEW orders — it does not reduce live
exposure. This module layers a three-state escalation on top of those
signals so that, when the risk context says "breached":

* stage ``none``     → reduce HALF of every sleeve position (PARTIAL).
* stage ``partial_done`` AND >=24h since first breach -> FLATTEN the rest,
  mark ``paused=True`` so subsequent rebalance cycles BLOCK until an
  operator writes a reviewed, valid resume state.
* stage ``flattened`` -> stay paused (no action — the human must intervene).

A clean breach (no breach AND ``paused=False``) resets ``stage`` to ``none``
and emits ``breach_cleared``. A clean breach while ``paused=True`` is
INTENTIONALLY IGNORED: only the operator can resume by replacing the latch
with a reviewed, valid state. Deleting it is insufficient because missing
state also blocks confirmed execution.

The state lives in a small JSON file (``breaker_state.json`` by default).
Confirmed execution is fail-closed when this file is missing or invalid.
Report-only mode may still build a diagnostic plan from a synthetic safe
``stage="none"`` state, but it never submits or mutates state.

An accepted order is not a completed risk transition.  Confirmed execution
polls each order by ``client_order_id`` until it is fully filled, then reads
positions and open orders again.  ``partial_done`` is persisted only after
the observed signed quantities match the expected reduction; ``flattened``
is persisted only after positions and open orders are both empty.  Any
ambiguous or failed reconciliation preserves the prior stage and latches
``paused=True``.

Submitting real orders requires THREE confirmations: ``--real-paper``,
``--confirm-paper``, AND ``--confirm-actions``. With ``confirm_actions=False``
the module computes the full plan and writes it under ``planned_actions``
without calling ``submit_order`` and without mutating the state file.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.config import ConfigError, load_risk_config, load_universe_config
from trading_ai.execution.alpaca_paper import PaperOrder
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
)
from trading_ai.execution.sleeve_rebalance import (
    DEFAULT_BREAKER_STATE_PATH,
    DEFAULT_EQUITY_HIGHWATER_PATH,
    _account_risk_context,
    map_broker_symbol_to_pair,
)

SCHEMA_VERSION = "2.0"
# §33: persistent breaches past this window escalate PARTIAL -> FLATTEN.
_ESCALATION_HOURS = 24.0
_BREAKER_UNIVERSE_CONFIGS = ("configs/universe.yml", "configs/crypto_alpaca.yml")
DEFAULT_POLL_ATTEMPTS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
_POSITION_TOLERANCE = 1e-8
_TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
}


@dataclass(frozen=True)
class SleeveCircuitBreakerResult:
    exit_code: int
    status: str  # "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class _BreakerStateRead:
    state: dict[str, object]
    status: str  # "ok" | "missing" | "corrupt"
    diagnostics: tuple[str, ...] = ()


# ------------------------------- helpers -----------------------------------


def _coerce_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _now_utc(now: Callable[[], datetime] | None) -> datetime:
    """Return an injected or wall-clock UTC datetime for transition timing."""
    if now is None:
        return datetime.now(UTC)
    return now()


def _load_breaker_state(state_path: Path) -> _BreakerStateRead:
    """Read and strictly validate state without authorizing execution.

    Missing/corrupt state carries a synthetic ``none`` value only so
    report-only mode can describe what it *would* do.  The caller must block
    confirmed execution unless ``status == "ok"``.
    """
    defaults: dict[str, object] = {
        "stage": "none",
        "paused": False,
        "first_breach_at": None,
        "updated_at": None,
    }
    if not state_path.exists():
        return _BreakerStateRead(defaults, "missing", ("breaker_state_missing",))
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _BreakerStateRead(
            defaults,
            "corrupt",
            (f"breaker_state_unreadable:{type(exc).__name__}",),
        )
    except ValueError:
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_invalid_json",))
    if not isinstance(payload, dict):
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_not_object",))
    stage = str(payload.get("stage") or "none")
    if stage not in {"none", "partial_done", "flattened"}:
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_invalid_stage",))
    paused_raw = payload.get("paused", False)
    if not isinstance(paused_raw, bool):
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_invalid_paused",))
    raw_first = payload.get("first_breach_at")
    raw_updated = payload.get("updated_at")
    if raw_first is not None and not isinstance(raw_first, str):
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_invalid_first_breach_at",))
    if raw_updated is not None and not isinstance(raw_updated, str):
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_invalid_updated_at",))
    if stage in {"partial_done", "flattened"} and not raw_first:
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_missing_first_breach_at",))
    if stage == "none" and raw_first:
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_unexpected_first_breach_at",))
    for field_name, raw_value in (
        ("first_breach_at", raw_first),
        ("updated_at", raw_updated),
    ):
        if raw_value is None:
            continue
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return _BreakerStateRead(defaults, "corrupt", (f"breaker_state_invalid_{field_name}",))
        if parsed.tzinfo is None:
            return _BreakerStateRead(defaults, "corrupt", (f"breaker_state_naive_{field_name}",))
    if stage == "flattened" and not paused_raw:
        return _BreakerStateRead(defaults, "corrupt", ("breaker_state_flattened_not_paused",))
    return _BreakerStateRead(
        {
            "stage": stage,
            "paused": paused_raw,
            "first_breach_at": raw_first,
            "updated_at": raw_updated,
        },
        "ok",
    )


def _save_breaker_state(state: Mapping[str, object], state_path: Path) -> None:
    write_json_artifact(dict(state), state_path)


def _union_universe_symbols() -> list[str]:
    """Load both sleeve universes (etf + crypto) and return their merged symbols.

    The breaker must act on every sleeve position the broker may hold — not
    just one universe — so the union is read on each invocation. Loading is
    silent on ``ConfigError`` (e.g. missing file) so a missing alt-universe
    config never blocks the primary path.
    """
    symbols: list[str] = []
    for config_path in _BREAKER_UNIVERSE_CONFIGS:
        try:
            symbols.extend(load_universe_config(config_path).symbols)
        except (ConfigError, OSError):
            continue
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        upper = symbol.upper()
        if upper in seen:
            continue
        seen.add(upper)
        ordered.append(upper)
    return ordered


def _position_qty(position: Any) -> float | None:
    """Tolerantly extract ``quantity``/``qty`` from a broker position."""
    for attr in ("quantity", "qty"):
        raw: Any = position.get(attr) if isinstance(position, dict) else getattr(position, attr, None)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _position_avg_price(position: Any) -> float | None:
    """Tolerantly extract ``avg_entry_price`` from a broker position."""
    raw: Any = (
        position.get("avg_entry_price") if isinstance(position, dict) else getattr(position, "avg_entry_price", None)
    )
    return _coerce_optional_float(raw)


def _position_symbol(position: Any) -> str:
    raw = position.get("symbol", "") if isinstance(position, dict) else getattr(position, "symbol", "")
    return str(raw or "").strip().upper()


def _position_market_value(position: Any) -> float | None:
    raw = position.get("market_value") if isinstance(position, dict) else getattr(position, "market_value", None)
    return _coerce_optional_float(raw)


def _asset_key(symbol: object) -> str:
    return str(symbol or "").upper().replace("/", "").replace("-", "")


def _normalize_position_snapshot(
    raw_positions: tuple[Any, ...],
    *,
    universe_symbols: list[str],
) -> tuple[list[dict[str, object]], dict[str, float], list[str]]:
    """Normalize a complete broker position snapshot or return blockers.

    An unmapped, malformed, non-finite, or duplicate position makes the
    snapshot unsafe for automated reduction.  No such position may be
    silently ignored by a circuit breaker.
    """

    normalized: list[dict[str, object]] = []
    quantities: dict[str, float] = {}
    blockers: list[str] = []
    for index, position in enumerate(raw_positions):
        broker_symbol = _position_symbol(position)
        if not broker_symbol:
            blockers.append(f"position_symbol_missing:{index}")
            continue
        pair = map_broker_symbol_to_pair(broker_symbol, universe_symbols)
        if pair is None:
            blockers.append(f"position_unmapped:{broker_symbol}")
            continue
        qty = _position_qty(position)
        if qty is None or not math.isfinite(qty):
            blockers.append(f"position_quantity_invalid:{broker_symbol}")
            continue
        if pair in quantities:
            blockers.append(f"position_duplicate:{pair}")
            continue
        quantities[pair] = qty
        normalized.append(
            {
                "pair": pair,
                "broker_symbol": broker_symbol,
                "quantity": qty,
                "market_value": _position_market_value(position),
                "avg_entry_price": _position_avg_price(position),
            }
        )
    return normalized, quantities, blockers


# ----------------------------- order building ------------------------------


def _build_breaker_order(
    *,
    pair: str,
    side: str,
    qty: float,
    client_order_id: str,
    position_intent: str,
    risk_context: Mapping[str, object],
    avg_price: float | None,
) -> PaperOrder:
    """Build a PaperOrder carrying the M7 risk-context kwargs.

    The breaker passes ``daily_pnl_pct`` and ``current_drawdown_pct`` so the
    broker's ``evaluate_risk_state`` can fire its kill-switch on this order
    too. ``estimated_position_weight`` and ``projected_gross_exposure`` are
    computed from the order's notional at the position's avg entry price —
    a degraded broker snapshot just produces 0.0 (acceptable because the
    breaker already passed the threshold).
    """
    equity = float(risk_context.get("equity", 0.0) or 0.0)
    notional = qty * (avg_price or 0.0)
    weight = notional / equity if equity > 0 else 0.0
    return PaperOrder(
        symbol=pair,
        side=side,
        client_order_id=client_order_id,
        quantity=qty,
        notional=None,
        daily_pnl_pct=float(risk_context.get("daily_pnl_pct", 0.0) or 0.0),
        current_drawdown_pct=float(risk_context.get("current_drawdown_pct", 0.0) or 0.0),
        estimated_position_weight=weight,
        projected_gross_exposure=weight,
        reference_price=avg_price,
        position_intent=position_intent,
    )


def _record_breaker_submission(
    *,
    pair: str,
    side: str,
    client_order_id: str,
    qty: float,
    position_intent: str,
    broker: Any,
    risk_context: Mapping[str, object],
    avg_price: float | None,
    poll_attempts: int,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
) -> dict[str, object]:
    """Submit one reduction and require a broker-confirmed complete fill."""
    order = _build_breaker_order(
        pair=pair,
        side=side,
        qty=qty,
        client_order_id=client_order_id,
        position_intent=position_intent,
        risk_context=risk_context,
        avg_price=avg_price,
    )
    try:
        result = broker.submit_order(order)
    except Exception as exc:  # noqa: BLE001 - broker surface (M7 idiom)
        return {
            "pair": pair,
            "side": side,
            "quantity": round(float(qty), 8),
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "error",
            "terminal_status": None,
            "filled_quantity": 0.0,
            "reconciled": False,
            "reasons": [f"submit_error:{type(exc).__name__}"],
        }
    accepted = bool(getattr(result, "accepted", False))
    submission_status = str(getattr(result, "status", "unknown"))
    reasons_attr = getattr(result, "reasons", ())
    reasons = list(reasons_attr) if isinstance(reasons_attr, (tuple, list)) else [str(reasons_attr)]
    record: dict[str, object] = {
        "pair": pair,
        "side": side,
        "quantity": round(float(qty), 8),
        "client_order_id": client_order_id,
        "submitted": accepted,
        "skipped": False,
        "status": submission_status,
        "terminal_status": None,
        "filled_quantity": 0.0,
        "reconciled": False,
        "reasons": reasons,
    }
    if not accepted:
        if not reasons:
            reasons.append(f"submit_not_accepted:{submission_status}")
        return record

    last_snapshot: Any | None = None
    last_lookup_error: str | None = None
    for attempt in range(poll_attempts):
        try:
            snapshot = broker.get_order_by_client_id(client_order_id)
            last_snapshot = snapshot
            last_lookup_error = None
        except Exception as exc:  # noqa: BLE001 - broker read surface
            last_lookup_error = type(exc).__name__
        if last_snapshot is not None:
            terminal_status = str(getattr(last_snapshot, "status", "") or "").lower()
            if terminal_status in _TERMINAL_ORDER_STATUSES:
                break
        if attempt + 1 < poll_attempts:
            sleep(poll_interval_seconds)

    if last_snapshot is None:
        record["reasons"] = [
            f"order_lookup_unavailable:{last_lookup_error or 'unknown'}",
        ]
        return record

    terminal_status = str(getattr(last_snapshot, "status", "") or "").lower()
    filled_quantity = _coerce_optional_float(
        getattr(last_snapshot, "filled_quantity", getattr(last_snapshot, "filled_qty", None))
    )
    record["terminal_status"] = terminal_status or None
    record["filled_quantity"] = filled_quantity or 0.0

    reconciliation_reasons: list[str] = []
    if last_lookup_error is not None:
        reconciliation_reasons.append(f"order_lookup_unavailable:{last_lookup_error}")
    if terminal_status != "filled":
        reconciliation_reasons.append(f"order_not_filled:{terminal_status or 'unknown'}")
    if filled_quantity is None or not math.isclose(
        filled_quantity,
        qty,
        rel_tol=1e-9,
        abs_tol=_POSITION_TOLERANCE,
    ):
        reconciliation_reasons.append("filled_quantity_mismatch")
    snapshot_client_id = str(getattr(last_snapshot, "client_order_id", "") or "")
    if snapshot_client_id != client_order_id:
        reconciliation_reasons.append("client_order_id_mismatch")
    snapshot_symbol = getattr(last_snapshot, "symbol", "")
    if _asset_key(snapshot_symbol) != _asset_key(pair):
        reconciliation_reasons.append("order_symbol_mismatch")
    snapshot_side = str(getattr(last_snapshot, "side", "") or "").lower()
    if snapshot_side != side:
        reconciliation_reasons.append("order_side_mismatch")
    snapshot_qty = _coerce_optional_float(getattr(last_snapshot, "quantity", None))
    if snapshot_qty is not None and not math.isclose(
        snapshot_qty,
        qty,
        rel_tol=1e-9,
        abs_tol=_POSITION_TOLERANCE,
    ):
        reconciliation_reasons.append("order_quantity_mismatch")

    record["reconciled"] = not reconciliation_reasons
    record["reasons"] = reconciliation_reasons
    return record


def _build_breaker_actions(
    *,
    positions: list[dict[str, object]],
    stage_after: str,
    as_of: date,
) -> list[dict[str, object]]:
    """Build the planned breaker actions for the chosen escalation.

    ``stage_after`` must be ``"partial_done"`` or ``"flattened"`` — the
    caller is expected to gate on the transition decision. Each returned
    dict has the same shape so report-only and confirmed payloads render
    identically.
    """
    if stage_after not in {"partial_done", "flattened"}:
        return []
    actions: list[dict[str, object]] = []
    for position in positions:
        pair = str(position["pair"])
        signed_qty = float(position["quantity"])
        if abs(signed_qty) <= _POSITION_TOLERANCE:
            continue
        side = "sell" if signed_qty > 0 else "buy"
        if stage_after == "partial_done":
            action_qty = round(abs(signed_qty) / 2.0, 8)
            cid = f"breaker-{as_of.isoformat()}-{pair.replace('/', '')}-half"
            position_intent = "reduce"
        else:  # "flattened"
            action_qty = round(abs(signed_qty), 8)
            cid = f"breaker-{as_of.isoformat()}-{pair.replace('/', '')}-all"
            position_intent = "close"
        if action_qty <= 0:
            continue
        actions.append(
            {
                "pair": pair,
                "side": side,
                "quantity": action_qty,
                "client_order_id": cid,
                "stage_at_action": stage_after,
                "position_intent": position_intent,
                "initial_quantity": signed_qty,
                "current_market_value": _position_market_value(position),
                "avg_entry_price": position.get("avg_entry_price"),
            }
        )
    return actions


def _execute_actions(
    *,
    planned: list[dict[str, object]],
    broker: Any,
    risk_context: Mapping[str, object],
    poll_attempts: int,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
) -> list[dict[str, object]]:
    """Submit planned reductions and collect terminal fill evidence."""
    records: list[dict[str, object]] = []
    for entry in planned:
        pair = str(entry.get("pair"))
        record = _record_breaker_submission(
            pair=pair,
            side=str(entry.get("side") or "").lower(),
            client_order_id=str(entry.get("client_order_id")),
            qty=float(entry.get("quantity") or 0.0),
            position_intent=str(entry.get("position_intent") or ""),
            broker=broker,
            risk_context=risk_context,
            avg_price=_coerce_optional_float(entry.get("avg_entry_price")),
            poll_attempts=poll_attempts,
            poll_interval_seconds=poll_interval_seconds,
            sleep=sleep,
        )
        records.append(record)
    return records


def _reconcile_action_transition(
    *,
    action_kind: str,
    planned: list[dict[str, object]],
    records: list[dict[str, object]],
    initial_quantities: Mapping[str, float],
    broker: Any,
    universe_symbols: list[str],
) -> dict[str, object]:
    """Re-read broker state and prove the requested exposure transition."""

    reasons: list[str] = []
    if action_kind == "partial" and not planned:
        reasons.append("no_reducible_positions")
    if len(records) != len(planned) or any(not bool(record.get("reconciled")) for record in records):
        reasons.append("orders_not_fully_filled")

    try:
        raw_positions = tuple(broker.read_positions())
    except Exception as exc:  # noqa: BLE001 - read-only broker surface
        return {
            "status": "failed",
            "reconciled": False,
            "reasons": [*reasons, f"post_positions_unavailable:{type(exc).__name__}"],
            "positions": [],
            "open_orders": [],
        }
    normalized, final_quantities, position_blockers = _normalize_position_snapshot(
        raw_positions,
        universe_symbols=universe_symbols,
    )
    reasons.extend(position_blockers)

    try:
        open_orders = tuple(broker.list_orders(status="open"))
    except Exception as exc:  # noqa: BLE001 - read-only broker surface
        return {
            "status": "failed",
            "reconciled": False,
            "reasons": [*reasons, f"open_orders_unavailable:{type(exc).__name__}"],
            "positions": normalized,
            "open_orders": [],
        }
    open_order_rows = [
        {
            "order_id": str(getattr(order, "order_id", "") or ""),
            "client_order_id": str(getattr(order, "client_order_id", "") or ""),
            "symbol": str(getattr(order, "symbol", "") or ""),
            "side": str(getattr(order, "side", "") or ""),
            "status": str(getattr(order, "status", "") or ""),
        }
        for order in open_orders
    ]
    if open_orders:
        reasons.append(f"open_orders_remaining:{len(open_orders)}")

    if action_kind == "partial":
        by_pair = {str(entry["pair"]): entry for entry in planned}
        unexpected = set(final_quantities) - set(initial_quantities)
        for pair in sorted(unexpected):
            if abs(final_quantities[pair]) > _POSITION_TOLERANCE:
                reasons.append(f"unexpected_position:{pair}")
        for pair, initial_qty in initial_quantities.items():
            if abs(initial_qty) <= _POSITION_TOLERANCE:
                continue
            entry = by_pair.get(pair)
            if entry is None:
                reasons.append(f"position_not_reduced:{pair}")
                continue
            action_qty = float(entry["quantity"])
            expected_qty = initial_qty - action_qty if initial_qty > 0 else initial_qty + action_qty
            observed_qty = float(final_quantities.get(pair, 0.0))
            if not math.isclose(
                observed_qty,
                expected_qty,
                rel_tol=1e-9,
                abs_tol=_POSITION_TOLERANCE,
            ):
                reasons.append(f"partial_position_mismatch:{pair}")
            if abs(observed_qty) >= abs(initial_qty) - _POSITION_TOLERANCE:
                reasons.append(f"exposure_not_reduced:{pair}")
    elif action_kind == "flatten":
        if normalized:
            reasons.append(f"positions_remaining:{len(normalized)}")
        residuals = {pair: qty for pair, qty in final_quantities.items() if abs(qty) > _POSITION_TOLERANCE}
        if residuals:
            reasons.extend(f"residual_position:{pair}" for pair in sorted(residuals))
    else:
        reasons.append("invalid_action_kind")

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "status": "reconciled" if not unique_reasons else "failed",
        "reconciled": not unique_reasons,
        "reasons": unique_reasons,
        "positions": normalized,
        "position_quantities": final_quantities,
        "open_orders": open_order_rows,
        "open_order_count": len(open_orders),
    }


# ----------------------------- telegram rendering --------------------------


def _render_telegram_message(
    *,
    as_of: date,
    breached: bool,
    stage_before: str,
    stage_after: str,
    paused: bool,
    actions: list[Mapping[str, object]],
    risk_context: Mapping[str, object] | None,
    events: list[str],
) -> str:
    """Build a Telegram-safe message mirroring M9's positional style."""
    lines: list[str] = [f"Circuit breaker paper {as_of.isoformat()}"]
    for event in events:
        lines.append(f"BREAKER: {event}")
    lines.append(f"stage {stage_before}->{stage_after} breached={str(breached).lower()} paused={str(paused).lower()}")
    if risk_context is not None:
        equity = _coerce_optional_float(risk_context.get("equity"))
        daily_pnl_pct = _coerce_optional_float(risk_context.get("daily_pnl_pct"))
        current_drawdown_pct = _coerce_optional_float(risk_context.get("current_drawdown_pct"))
        equity_text = f"${equity:.2f}" if equity is not None else "n/a"
        pnl_text = f"{daily_pnl_pct * 100:.2f}%" if daily_pnl_pct is not None else "n/a"
        dd_text = f"{current_drawdown_pct * 100:.2f}%" if current_drawdown_pct is not None else "n/a"
        lines.append(f"equity {equity_text} pnl_dia {pnl_text} dd {dd_text}")
    if actions:
        lines.append("Acciones:")
        for action in actions:
            pair = str(action.get("pair") or "?")
            side = str(action.get("side") or "?")
            qty = _coerce_optional_float(action.get("quantity"))
            cid = str(action.get("client_order_id") or "")
            qty_text = f"{qty:g}" if qty is not None else "?"
            lines.append(f"{pair} {side} {qty_text} {cid}")
    return "\n".join(lines)


# ----------------------------- public entry point ---------------------------


def run_sleeve_circuit_breaker(
    *,
    risk_config: str | Path,
    output: str | Path,
    telegram_artifact: str | Path | None = None,
    broker: Any,
    state_path: str | Path = DEFAULT_BREAKER_STATE_PATH,
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    confirm_actions: bool = False,
    as_of_date: date | None = None,
    now: Callable[[], datetime] | None = None,
    generated_at: str | None = None,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> SleeveCircuitBreakerResult:
    """Evaluate and (optionally) escalate the sleeve kill-switches (§33).

    The state machine lives on disk (``state_path``); only confirmed
    transitions are persisted. See the module docstring for the
    noop/partial/flatten transitions.
    """
    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()
    state_path_obj = Path(state_path)
    events: list[str] = []
    blockers: list[str] = []

    if poll_attempts < 1 or poll_interval_seconds < 0:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": ["invalid_order_poll_configuration"],
            "status": PAPER_BLOCKED,
            "safety": {
                "read_only": not confirm_actions,
                "paper_only": True,
                "actions_executed": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveCircuitBreakerResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    # 1) Risk config — fail-closed on a missing/invalid risk YAML.
    try:
        risk = load_risk_config(risk_config, allow_live=False)
    except ConfigError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": [f"risk_config_error:{exc}"],
            "status": PAPER_BLOCKED,
            "safety": {
                "read_only": not confirm_actions,
                "paper_only": True,
                "actions_executed": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveCircuitBreakerResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    # 2) Account risk context. None -> BLOCKED, no state mutation, no actions.
    risk_context = _account_risk_context(
        broker,
        Path(equity_highwater_path),
        require_existing_high_water=confirm_actions,
        persist_high_water=confirm_actions,
    )
    if risk_context is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "account_risk": None,
            "blockers": ["account_risk_context_unavailable"],
            "status": PAPER_BLOCKED,
            "safety": {
                "read_only": not confirm_actions,
                "paper_only": True,
                "actions_executed": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveCircuitBreakerResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    # 3) Prior state. Missing/corrupt state may inform a report-only plan but
    #    can never authorize broker mutations.
    state_read = _load_breaker_state(state_path_obj)
    prior = state_read.state
    stage_before = str(prior.get("stage") or "none")
    paused_before = bool(prior.get("paused", False))
    first_breach_at_raw = prior.get("first_breach_at")
    state_persisted: bool | None = None

    if confirm_actions and state_read.status != "ok":
        blockers.extend(state_read.diagnostics)
        events.append("state_fail_closed")
        safe_latch = {
            "stage": "none",
            "paused": True,
            "first_breach_at": None,
            "updated_at": _now_utc(now).isoformat(),
        }
        try:
            _save_breaker_state(safe_latch, state_path_obj)
            state_persisted = True
        except Exception as exc:  # noqa: BLE001 - state storage boundary
            state_persisted = False
            blockers.append(f"safe_latch_persistence_failed:{type(exc).__name__}")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "account_risk": risk_context,
            "breached": None,
            "stage_before": stage_before,
            "stage_after": stage_before,
            "proposed_stage": stage_before,
            "paused": True,
            "proposed_paused": True,
            "first_breach_at": first_breach_at_raw,
            "proposed_first_breach_at": first_breach_at_raw,
            "state_load_status": state_read.status,
            "state_diagnostics": list(state_read.diagnostics),
            "state_persisted": state_persisted,
            "transition_reconciled": False,
            "actions": [],
            "planned_actions": [],
            "events": events,
            "blockers": blockers,
            "reconciliation": {
                "status": "not_run",
                "reconciled": False,
                "reasons": list(state_read.diagnostics),
            },
            "safety": {
                "read_only": False,
                "paper_only": True,
                "actions_executed": False,
                "accepted_is_not_transitioned": True,
                "terminal_fill_required": True,
                "post_trade_reconciliation_required": True,
            },
            "status": PAPER_BLOCKED,
        }
        write_json_artifact(payload, output_path)
        return SleeveCircuitBreakerResult(
            exit_code=paper_exit_code(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    # 4) Breach check.
    daily_pnl_pct = float(risk_context.get("daily_pnl_pct", 0.0))
    current_drawdown_pct = float(risk_context.get("current_drawdown_pct", 0.0))
    breached = bool(
        daily_pnl_pct <= -abs(float(risk.max_daily_loss_pct))
        or current_drawdown_pct >= abs(float(risk.max_drawdown_pct))
    )

    # 5) Build a proposed transition. For exposure-changing actions this is
    #    not the final transition until terminal fills and broker state agree.
    proposed_stage = stage_before
    proposed_paused = paused_before
    proposed_first_breach = first_breach_at_raw
    action_kind: str | None = None  # "partial" | "flatten" | None

    if paused_before:
        events.append("paused_latched")
    elif not breached and stage_before == "none":
        events.append("noop_clean")
    elif not breached and stage_before != "none":
        # Clean breach with a prior escalation => reset.
        proposed_stage = "none"
        proposed_first_breach = None
        events.append("breach_cleared")
    elif breached and stage_before == "none":
        # New breach => PARTIAL.
        proposed_stage = "partial_done"
        proposed_first_breach = _now_utc(now).isoformat()
        action_kind = "partial"
        events.append("partial_requested" if confirm_actions else "partial_planned")
    elif breached and stage_before == "partial_done":
        # Already partial: check whether the 24h escalation window elapsed.
        try:
            first_dt = datetime.fromisoformat(str(first_breach_at_raw)) if first_breach_at_raw else None
        except ValueError:
            first_dt = None
        elapsed_hours: float | None = None
        if first_dt is not None:
            elapsed = _now_utc(now) - first_dt
            elapsed_hours = elapsed.total_seconds() / 3600.0
        if first_dt is not None and elapsed_hours is not None and elapsed_hours >= _ESCALATION_HOURS:
            proposed_stage = "flattened"
            proposed_paused = True
            action_kind = "flatten"
            events.append("flatten_requested" if confirm_actions else "flatten_planned")
        else:
            events.append("awaiting_escalation")
    elif breached and stage_before == "flattened":
        proposed_paused = True
        events.append("paused_latched")
    else:
        events.append("noop_unhandled_state")

    if confirm_actions and action_kind is None:
        stage_after = proposed_stage
        paused_after = proposed_paused
        first_breach_after = proposed_first_breach
    else:
        stage_after = stage_before
        paused_after = paused_before
        first_breach_after = first_breach_at_raw

    # 6) Build the action plan from one complete position snapshot.
    universe_symbols = _union_universe_symbols()
    planned_actions: list[dict[str, object]] = []
    initial_positions: list[dict[str, object]] = []
    initial_quantities: dict[str, float] = {}
    plan_blockers: list[str] = []
    if action_kind is not None:
        try:
            raw_positions = tuple(broker.read_positions())
        except Exception as exc:  # noqa: BLE001 - broker read surface
            plan_blockers.append(f"positions_unavailable:{type(exc).__name__}")
        else:
            initial_positions, initial_quantities, plan_blockers = _normalize_position_snapshot(
                raw_positions,
                universe_symbols=universe_symbols,
            )
            planned_actions = _build_breaker_actions(
                positions=initial_positions,
                stage_after=proposed_stage,
                as_of=as_of,
            )
            nonzero_positions = sum(abs(quantity) > _POSITION_TOLERANCE for quantity in initial_quantities.values())
            if action_kind == "partial" and len(planned_actions) != nonzero_positions:
                plan_blockers.append("partial_plan_incomplete")

    # 7) Execute and reconcile, or emit a strictly read-only diagnostic plan.
    reconciliation: dict[str, object] = {
        "status": "not_required",
        "reconciled": action_kind is None,
        "reasons": [],
    }
    transition_reconciled = action_kind is None
    execution_failed = False
    if not confirm_actions:
        action_records = [
            {
                **entry,
                "submitted": False,
                "skipped": True,
                "status": "report_only",
                "terminal_status": None,
                "filled_quantity": 0.0,
                "reconciled": False,
                "reasons": ["report_only"],
            }
            for entry in planned_actions
        ]
        if action_kind is not None:
            reconciliation = {
                "status": "report_only",
                "reconciled": False,
                "reasons": [*state_read.diagnostics, *plan_blockers, "report_only"],
                "initial_positions": initial_positions,
            }
            transition_reconciled = False
    elif action_kind is None:
        action_records = []
    elif plan_blockers:
        action_records = []
        execution_failed = True
        reconciliation = {
            "status": "failed",
            "reconciled": False,
            "reasons": plan_blockers,
            "initial_positions": initial_positions,
        }
    else:
        action_records = _execute_actions(
            planned=planned_actions,
            broker=broker,
            risk_context=risk_context,
            poll_attempts=poll_attempts,
            poll_interval_seconds=poll_interval_seconds,
            sleep=sleep,
        )
        reconciliation = _reconcile_action_transition(
            action_kind=action_kind,
            planned=planned_actions,
            records=action_records,
            initial_quantities=initial_quantities,
            broker=broker,
            universe_symbols=universe_symbols,
        )
        transition_reconciled = bool(reconciliation.get("reconciled"))
        execution_failed = not transition_reconciled

    if confirm_actions and action_kind is not None:
        if transition_reconciled:
            stage_after = proposed_stage
            paused_after = proposed_paused
            first_breach_after = proposed_first_breach
            events.append(f"{action_kind}_reconciled")
        else:
            # Preserve the durable stage and latch all further automation.
            stage_after = stage_before
            paused_after = True
            first_breach_after = first_breach_at_raw
            events.append(f"{action_kind}_failed")
            blockers.extend(str(reason) for reason in reconciliation.get("reasons", []))

    # 8) Persist only reconciled transitions or a conservative failure latch.
    transition = (
        stage_after != stage_before
        or paused_after != paused_before
        or (first_breach_after or None) != (first_breach_at_raw or None)
    )
    if confirm_actions and transition:
        try:
            _save_breaker_state(
                {
                    "stage": stage_after,
                    "paused": paused_after,
                    "first_breach_at": first_breach_after,
                    "updated_at": _now_utc(now).isoformat(),
                },
                state_path_obj,
            )
            state_persisted = True
        except Exception as exc:  # noqa: BLE001 - state storage boundary
            state_persisted = False
            execution_failed = True
            blockers.append(f"breaker_state_persistence_failed:{type(exc).__name__}")
            events.append("state_persistence_failed")

    # 9) A confirmed ambiguous/failed transition is BLOCKED. Report-only
    #    diagnostics remain WARN and never imply a broker mutation.
    blockers = list(dict.fromkeys(blockers))
    if confirm_actions and paused_before:
        blockers.append("circuit_breaker_paused")
        blockers = list(dict.fromkeys(blockers))
        status = PAPER_BLOCKED
    elif confirm_actions and execution_failed:
        status = PAPER_BLOCKED
    elif breached or action_records or state_read.status != "ok":
        status = PAPER_WARN
    else:
        status = PAPER_OK

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "account_risk": risk_context,
        "breached": breached,
        "stage_before": stage_before,
        "stage_after": stage_after,
        "proposed_stage": proposed_stage,
        "paused": paused_after,
        "proposed_paused": proposed_paused,
        "first_breach_at": first_breach_after,
        "proposed_first_breach_at": proposed_first_breach,
        "state_load_status": state_read.status,
        "state_diagnostics": list(state_read.diagnostics),
        "state_persisted": state_persisted,
        "transition_reconciled": transition_reconciled,
        "actions": action_records,
        "planned_actions": planned_actions,
        "events": events,
        "blockers": blockers,
        "reconciliation": {
            **reconciliation,
            "initial_positions": initial_positions,
        },
        "safety": {
            "read_only": not confirm_actions,
            "paper_only": True,
            "actions_executed": any(bool(record.get("submitted")) for record in action_records),
            "accepted_is_not_transitioned": True,
            "terminal_fill_required": True,
            "post_trade_reconciliation_required": True,
        },
        "status": status,
    }
    write_json_artifact(payload, output_path)

    if telegram_artifact is not None:
        telegram_payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "as_of_date": as_of.isoformat(),
            "status": status if status != PAPER_BLOCKED else PAPER_WARN,
            "message": _render_telegram_message(
                as_of=as_of,
                breached=breached,
                stage_before=stage_before,
                stage_after=stage_after,
                paused=paused_after,
                actions=action_records,
                risk_context=risk_context,
                events=events,
            ),
            "safety": {
                "paper_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": any(bool(record.get("submitted")) for record in action_records),
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
        write_json_artifact(telegram_payload, Path(telegram_artifact))

    return SleeveCircuitBreakerResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


__all__ = [
    "DEFAULT_POLL_ATTEMPTS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "SCHEMA_VERSION",
    "SleeveCircuitBreakerResult",
    "run_sleeve_circuit_breaker",
]
