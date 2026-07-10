"""Governed escalation of the sleeve risk kill-switches (Sprint M11, §33).

When the daily-loss or drawdown kill-switch trips on a real broker account,
the existing M7 behavior only blocks NEW orders — it does not reduce live
exposure. This module layers a three-state escalation on top of those
signals so that, when the risk context says "breached":

* stage ``none``     → sell HALF of every sleeve position (PARTIAL).
* stage ``partial_done`` AND >=24h since first breach -> FLATTEN the rest,
  mark ``paused=True`` so subsequent rebalance cycles BLOCK until a human
  removes the state file.
* stage ``flattened`` -> stay paused (no action — the human must intervene).

A clean breach (no breach AND ``paused=False``) resets ``stage`` to ``none``
and emits ``breach_cleared``. A clean breach while ``paused=True`` is
INTENTIONALLY IGNORED: only the operator can resume by deleting the state
file. That fail-closed posture is documented in the module docstring so
operators don't expect the breaker to auto-recover from a manual pause.

The state lives in a small JSON file (``breaker_state.json`` by default).
The file is fail-closed — if it is missing or unreadable, we treat it as
``stage="none"`` and never auto-mutate the file unless ``confirm_actions``
was explicitly opted in (M11 follows the repo's "report-only by default"
idiom).

Submitting real orders requires THREE confirmations: ``--real-paper``,
``--confirm-paper``, AND ``--confirm-actions``. With ``confirm_actions=False``
the module computes the full plan and writes it under ``planned_actions``
without calling ``submit_order`` and without mutating the state file.
"""

from __future__ import annotations

import json
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
    _read_broker_positions_by_pair,
    map_broker_symbol_to_pair,
)

SCHEMA_VERSION = "1.0"
# §33: persistent breaches past this window escalate PARTIAL -> FLATTEN.
_ESCALATION_HOURS = 24.0
_BREAKER_UNIVERSE_CONFIGS = ("configs/universe.yml", "configs/crypto_alpaca.yml")


@dataclass(frozen=True)
class SleeveCircuitBreakerResult:
    exit_code: int
    status: str  # "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


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


def _load_breaker_state(state_path: Path) -> dict[str, object]:
    """Return a normalized prior-state dict; missing/corrupt -> stage none.

    Per the fail-closed idiom, an unreadable state file is NOT a hard error
    here — the breaker simply starts fresh. That keeps an I/O glitch from
    trapping the portfolio in ``flattened/paused`` mode forever.
    """
    defaults: dict[str, object] = {
        "stage": "none",
        "paused": False,
        "first_breach_at": None,
        "updated_at": None,
    }
    if not state_path.exists():
        return defaults
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(payload, dict):
        return defaults
    stage = str(payload.get("stage") or "none")
    if stage not in {"none", "partial_done", "flattened"}:
        stage = "none"
    raw_first = payload.get("first_breach_at")
    raw_updated = payload.get("updated_at")
    return {
        "stage": stage,
        "paused": bool(payload.get("paused", False)),
        "first_breach_at": raw_first if isinstance(raw_first, str) else None,
        "updated_at": raw_updated if isinstance(raw_updated, str) else None,
    }


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
        raw: Any
        if isinstance(position, dict):
            raw = position.get(attr)
        else:
            raw = getattr(position, attr, None)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _position_avg_price(position: Any) -> float | None:
    """Tolerantly extract ``avg_entry_price`` from a broker position."""
    raw: Any
    if isinstance(position, dict):
        raw = position.get("avg_entry_price")
    else:
        raw = getattr(position, "avg_entry_price", None)
    return _coerce_optional_float(raw)


# ----------------------------- order building ------------------------------


def _build_breaker_order(
    *,
    pair: str,
    qty: float,
    client_order_id: str,
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
        side="sell",
        client_order_id=client_order_id,
        quantity=qty,
        notional=None,
        daily_pnl_pct=float(risk_context.get("daily_pnl_pct", 0.0) or 0.0),
        current_drawdown_pct=float(risk_context.get("current_drawdown_pct", 0.0) or 0.0),
        estimated_position_weight=weight,
        projected_gross_exposure=weight,
        reference_price=avg_price,
    )


def _record_breaker_submission(
    *,
    pair: str,
    client_order_id: str,
    qty: float,
    broker: Any,
    risk_context: Mapping[str, object],
    avg_price: float | None,
) -> dict[str, object]:
    """Submit one breaker sell order, recording the outcome in the standard shape."""
    order = _build_breaker_order(
        pair=pair,
        qty=qty,
        client_order_id=client_order_id,
        risk_context=risk_context,
        avg_price=avg_price,
    )
    try:
        result = broker.submit_order(order)
    except Exception as exc:  # noqa: BLE001 - broker surface (M7 idiom)
        return {
            "pair": pair,
            "side": "sell",
            "quantity": round(float(qty), 8),
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "error",
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    accepted = bool(getattr(result, "accepted", False))
    status = str(getattr(result, "status", "unknown"))
    reasons_attr = getattr(result, "reasons", ())
    reasons = list(reasons_attr) if isinstance(reasons_attr, (tuple, list)) else [str(reasons_attr)]
    return {
        "pair": pair,
        "side": "sell",
        "quantity": round(float(qty), 8),
        "client_order_id": client_order_id,
        "submitted": accepted,
        "skipped": False,
        "status": status,
        "reasons": reasons,
    }


def _build_breaker_actions(
    *,
    broker: Any,
    universe_symbols: list[str],
    risk_context: Mapping[str, object],
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
    current_by_pair, _ignored = _read_broker_positions_by_pair(broker, universe_symbols)
    raw_positions = broker.read_positions()
    actions: list[dict[str, object]] = []
    for position in raw_positions:
        if isinstance(position, dict):
            broker_symbol = str(position.get("symbol", "")).upper()
        else:
            broker_symbol = str(getattr(position, "symbol", "")).upper()
        if not broker_symbol:
            continue
        pair = map_broker_symbol_to_pair(broker_symbol, universe_symbols)
        if pair is None:
            continue
        qty = _position_qty(position)
        if qty is None or qty <= 0:
            continue
        if stage_after == "partial_done":
            # PARTIAL: sell half of the current quantity. Round down to the
            # smallest positive step so we never oversell.
            sell_qty = qty / 2.0
            cid = f"breaker-{as_of.isoformat()}-{pair.replace('/', '')}-half"
        else:  # "flattened"
            # FLATTEN: sell 100% of the current quantity (which is the rest
            # of what PARTIAL did not already dispose of).
            sell_qty = qty
            cid = f"breaker-{as_of.isoformat()}-{pair.replace('/', '')}-all"
        actions.append(
            {
                "pair": pair,
                "side": "sell",
                "quantity": round(sell_qty, 8),
                "client_order_id": cid,
                "stage_at_action": stage_after,
                "current_market_value": round(float(current_by_pair.get(pair, 0.0)), 2),
            }
        )
    return actions


def _execute_actions(
    *,
    planned: list[dict[str, object]],
    broker: Any,
    risk_context: Mapping[str, object],
) -> list[dict[str, object]]:
    """Submit the planned actions through the broker, capturing each outcome.

    The avg entry price used for risk kwargs is taken from the broker's
    current positions. A degraded snapshot (no avg_entry_price) just
    submits with 0.0 notional — the broker still receives the order, the
    M7 risk gates still see daily_pnl and drawdown.
    """
    raw_positions = broker.read_positions()
    avg_by_broker_symbol: dict[str, float | None] = {}
    for position in raw_positions:
        if isinstance(position, dict):
            broker_symbol = str(position.get("symbol", "")).upper()
        else:
            broker_symbol = str(getattr(position, "symbol", "")).upper()
        if broker_symbol:
            avg_by_broker_symbol[broker_symbol] = _position_avg_price(position)
    records: list[dict[str, object]] = []
    for entry in planned:
        pair = str(entry.get("pair"))
        compact = pair.replace("/", "").upper()
        record = _record_breaker_submission(
            pair=pair,
            client_order_id=str(entry.get("client_order_id")),
            qty=float(entry.get("quantity") or 0.0),
            broker=broker,
            risk_context=risk_context,
            avg_price=avg_by_broker_symbol.get(compact) or avg_by_broker_symbol.get(pair),
        )
        records.append(record)
    return records


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
    lines.append(
        f"stage {stage_before}->{stage_after} breached={str(breached).lower()} "
        f"paused={str(paused).lower()}"
    )
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
            qty = _coerce_optional_float(action.get("quantity"))
            cid = str(action.get("client_order_id") or "")
            qty_text = f"{qty:g}" if qty is not None else "?"
            lines.append(f"{pair} sell {qty_text} {cid}")
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

    # 1) Risk config — fail-closed on a missing/invalid risk YAML.
    try:
        risk = load_risk_config(risk_config, allow_live=False)
    except ConfigError as exc:
        payload: dict[str, object] = {
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
    risk_context = _account_risk_context(broker, Path(equity_highwater_path))
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

    # 3) Prior state — missing/unreadable becomes the default ``stage=none``.
    prior = _load_breaker_state(state_path_obj)
    stage_before = str(prior.get("stage") or "none")
    paused_before = bool(prior.get("paused", False))
    first_breach_at_raw = prior.get("first_breach_at")

    # 4) Breach check.
    daily_pnl_pct = float(risk_context.get("daily_pnl_pct", 0.0))
    current_drawdown_pct = float(risk_context.get("current_drawdown_pct", 0.0))
    breached = bool(
        daily_pnl_pct <= -abs(float(risk.max_daily_loss_pct))
        or current_drawdown_pct >= abs(float(risk.max_drawdown_pct))
    )

    # 5) State machine.
    stage_after = stage_before
    paused_after = paused_before
    first_breach_after = first_breach_at_raw
    action_kind: str | None = None  # "partial" | "flatten" | None

    if not breached and stage_before == "none" and not paused_before:
        events.append("noop_clean")
    elif not breached and paused_before:
        # Manual pause in effect; the operator must remove the file.
        events.append("paused")
    elif not breached and stage_before != "none":
        # Clean breach with a prior escalation => reset.
        stage_after = "none"
        first_breach_after = None
        events.append("breach_cleared")
    elif breached and stage_before == "none":
        # New breach => PARTIAL.
        stage_after = "partial_done"
        first_breach_after = _now_utc(now).isoformat()
        action_kind = "partial"
        events.append("partial_triggered")
    elif breached and stage_before == "partial_done":
        # Already partial: check whether the 24h escalation window elapsed.
        try:
            first_dt = (
                datetime.fromisoformat(str(first_breach_at_raw))
                if first_breach_at_raw
                else None
            )
        except ValueError:
            first_dt = None
        elapsed_hours: float | None = None
        if first_dt is not None:
            elapsed = _now_utc(now) - first_dt
            elapsed_hours = elapsed.total_seconds() / 3600.0
        if (
            first_dt is not None
            and elapsed_hours is not None
            and elapsed_hours >= _ESCALATION_HOURS
        ):
            stage_after = "flattened"
            paused_after = True
            action_kind = "flatten"
            events.append("flatten_triggered")
        else:
            events.append("awaiting_escalation")
    elif breached and stage_before == "flattened":
        paused_after = True
        events.append("paused")
    else:
        events.append("noop_unhandled_state")

    # 6) Build the planned actions (always — report-only just won't submit).
    universe_symbols = _union_universe_symbols()
    planned_actions: list[dict[str, object]] = []
    if action_kind is not None:
        stage_for_action = "partial_done" if action_kind == "partial" else "flattened"
        planned_actions = _build_breaker_actions(
            broker=broker,
            universe_symbols=universe_symbols,
            risk_context=risk_context,
            stage_after=stage_for_action,
            as_of=as_of,
        )

    # 7) Execute (or just record) the actions.
    if confirm_actions and planned_actions:
        action_records = _execute_actions(
            planned=planned_actions,
            broker=broker,
            risk_context=risk_context,
        )
    else:
        # Report-only path: keep the planned shape, mark as not executed.
        action_records = [
            {
                **entry,
                "submitted": False,
                "skipped": True,
                "status": "report_only",
                "reasons": ["report_only"],
            }
            for entry in planned_actions
        ]

    # 8) Persist state ONLY on a confirmed transition.
    transition = (
        stage_after != stage_before
        or paused_after != paused_before
        or (first_breach_after or None) != (first_breach_at_raw or None)
    )
    if confirm_actions and transition:
        _save_breaker_state(
            {
                "stage": stage_after,
                "paused": paused_after,
                "first_breach_at": first_breach_after,
                "updated_at": _now_utc(now).isoformat(),
            },
            state_path_obj,
        )

    # 9) Status: WARN when breached or any actions; OK otherwise; BLOCKED only
    #    for the account-context-absent path (handled above). State-only
    #    transitions like ``breach_cleared`` keep the OK status — the
    #    account is healthy, we're just rolling back escalation memory.
    status = PAPER_WARN if (breached or action_records) else PAPER_OK

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "account_risk": risk_context,
        "breached": breached,
        "stage_before": stage_before,
        "stage_after": stage_after,
        "paused": paused_after,
        "first_breach_at": first_breach_after,
        "actions": action_records,
        "planned_actions": planned_actions,
        "events": events,
        "safety": {
            "read_only": not confirm_actions,
            "paper_only": True,
            "actions_executed": bool(confirm_actions and action_records),
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
                "orders_submitted": False,
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
    "SCHEMA_VERSION",
    "SleeveCircuitBreakerResult",
    "run_sleeve_circuit_breaker",
]
