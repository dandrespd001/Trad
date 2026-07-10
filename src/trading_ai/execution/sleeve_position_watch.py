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

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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

# M11 watchdog (WS2b/c): match both ``cycle_<sleeve>_<date>.json`` (M3/M5)
# and the bare ``cycle_<date>.json`` shape. Anything else is ignored.
_CYCLE_WATCH_PATTERN = re.compile(r"^cycle_(?:[a-z0-9_]+_)?(\d{4}-\d{2}-\d{2})\.json$")
# M13 reconciliation (WS4): only ``cycle_<sleeve>_<date>.json`` files carry
# a per-sleeve plan we can reconcile against. Bare ``cycle_<date>.json`` is
# the M9 reporting shape and has no sleeve context, so it is skipped here.
_CYCLE_PER_SLEEVE_PATTERN = re.compile(r"^cycle_([a-z0-9_]+)_(\d{4}-\d{2}-\d{2})\.json$")

# Drift threshold: notional differences smaller than this are not incidents.
# Combines an absolute floor ($50, to absorb crypto quote noise) with a
# relative floor (20% of the larger side, to scale with the size of the
# position). Either trigger alone fires the incident.
DRIFT_ABSOLUTE_FLOOR_USD = 50.0
DRIFT_RELATIVE_FLOOR_FRACTION = 0.20


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


def _collect_orders_today(
    broker: Any,
    *,
    as_of: date,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    """Return ``(fills, expired_orders, incidents)`` for today, with graceful degradation.

    Scans ``list_orders(status="closed")`` once and splits into:

    - ``fills`` — ``sleeve-`` prefixed orders with positive ``filled_quantity``
      and ``updated_at`` date equal to ``as_of``.
    - ``expired_orders`` — ``sleeve-`` or ``breaker-`` prefixed orders whose
      status contains ``"expired"`` (case-insensitive) and ``updated_at`` date
      equal to ``as_of`` or ``as_of - 1`` day. Informative incidents: a DAY
      order that timed out before fill — the next cycle will re-plan.

    A raising ``list_orders`` is recorded as ``orders_list_failed`` — both
    lists become empty but the rest of the report still ships.
    """
    incidents: list[str] = []
    try:
        closed_orders = broker.list_orders(status="closed")
    except Exception as exc:  # noqa: BLE001 - broker failures degrade to an incident
        incidents.append(f"orders_list_failed:{type(exc).__name__}:{exc}")
        return [], [], incidents
    fills: list[dict[str, object]] = []
    expired_orders: list[dict[str, object]] = []
    as_of_iso = as_of.isoformat()
    yesterday_iso = (as_of - timedelta(days=1)).isoformat()
    for order in closed_orders or []:
        client_order_id = str(_coerce_position_value(order, "client_order_id", default="") or "")
        updated_at = str(_coerce_position_value(order, "updated_at", default="") or "")
        updated_date = updated_at[:10] if updated_at else ""
        status_text = str(_coerce_position_value(order, "status", default="") or "")
        # Expired-order detection: status contains "expired" (any case),
        # sleeve-/breaker- prefix, and updated_at on as_of or as_of - 1 day.
        if (
            updated_date in (as_of_iso, yesterday_iso)
            and client_order_id
            and (client_order_id.startswith("sleeve-") or client_order_id.startswith("breaker-"))
            and "expired" in status_text.lower()
        ):
            expired_orders.append(
                {
                    "client_order_id": client_order_id,
                    "symbol": str(_coerce_position_value(order, "symbol", default="") or "").upper(),
                    "side": str(_coerce_position_value(order, "side", default="") or "").lower(),
                    "status": status_text,
                    "updated_at": updated_at,
                }
            )
            continue
        if not client_order_id.startswith("sleeve-"):
            continue
        filled_qty = _coerce_optional_float(_coerce_position_value(order, "filled_quantity", default=None))
        if filled_qty is None or filled_qty <= 0:
            continue
        if not updated_at:
            continue
        # updated_at is an ISO timestamp — keep the date prefix.
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
    return fills, expired_orders, incidents


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


def _latest_cycle_date(cycles_dir: Path) -> date | None:
    """Return the most recent cycle date observed in ``cycles_dir``.

    A cycle file is ``cycle_<sleeve>_<YYYY-MM-DD>.json`` (M5) or the bare
    ``cycle_<YYYY-MM-DD>.json`` shape. Anything else is ignored. Returns
    ``None`` when the directory is missing or contains no cycle files.
    """
    if not cycles_dir.exists() or not cycles_dir.is_dir():
        return None
    latest: date | None = None
    for entry in cycles_dir.iterdir():
        if not entry.is_file():
            continue
        match = _CYCLE_WATCH_PATTERN.match(entry.name)
        if not match:
            continue
        try:
            cycle_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if latest is None or cycle_date > latest:
            latest = cycle_date
    return latest


def _check_daily_cycle(
    cycles_dir: Path,
    *,
    as_of: date,
) -> str | None:
    """Return the ``daily_cycle_missing:<date|none>`` incident slug, or None.

    Triggers when no cycle file is newer than ``as_of - 1 day`` — i.e. we
    haven't seen a rebalance artifact for today OR yesterday. Both shapes
    are accepted by :func:`_latest_cycle_date`.
    """
    latest = _latest_cycle_date(cycles_dir)
    if latest is None:
        return "daily_cycle_missing:none"
    cutoff = as_of - timedelta(days=1)
    if latest < cutoff:
        return f"daily_cycle_missing:{latest.isoformat()}"
    return None


def _latest_cycle_per_sleeve(
    cycles_dir: Path,
    *,
    as_of: date,
) -> dict[str, Path]:
    """Return ``{sleeve: latest_cycle_path}`` for cycle files dated ≤ ``as_of``.

    Only ``cycle_<sleeve>_<date>.json`` files are considered (the per-sleeve
    shape). Bare ``cycle_<date>.json`` is the M9 reporting shape and has no
    sleeve context, so it is skipped. The most recent file per sleeve wins.
    Files with a future date (date > as_of) are also skipped — those would
    be a misnamed file from a test fixture or a clock-skewed broker.
    """
    if not cycles_dir.exists() or not cycles_dir.is_dir():
        return {}
    latest_per_sleeve: dict[str, tuple[date, Path]] = {}
    for entry in cycles_dir.iterdir():
        if not entry.is_file():
            continue
        match = _CYCLE_PER_SLEEVE_PATTERN.match(entry.name)
        if not match:
            continue
        sleeve, date_str = match.group(1), match.group(2)
        try:
            cycle_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        if cycle_date > as_of:
            continue
        existing = latest_per_sleeve.get(sleeve)
        if existing is None or cycle_date > existing[0]:
            latest_per_sleeve[sleeve] = (cycle_date, entry)
    return {sleeve: path for sleeve, (_, path) in latest_per_sleeve.items()}


def _load_plan_targets(cycle_path: Path) -> dict[str, float]:
    """Read a cycle file and return ``{pair: target_notional}``.

    Malformed JSON, missing ``plan`` field, or non-numeric ``target_notional``
    values all degrade silently — the watch is read-only and must never crash
    the operator dashboard. Callers receive an empty dict and the file is
    reported as ``cycle_file_unreadable:<name>`` separately.
    """
    try:
        payload = json.loads(cycle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plan_entries = payload.get("plan") if isinstance(payload, Mapping) else None
    if not isinstance(plan_entries, list):
        return {}
    targets: dict[str, float] = {}
    for entry in plan_entries:
        if not isinstance(entry, Mapping):
            continue
        pair = str(entry.get("pair", "")).upper()
        if not pair:
            continue
        target = _coerce_optional_float(entry.get("target_notional"))
        if target is None:
            continue
        targets[pair] = target
    return targets


def _open_orders_by_pair(
    broker: Any,
    *,
    known_pairs_upper: set[str],
) -> dict[str, list[str]]:
    """Return ``{pair_upper: [client_order_id, ...]}`` for open sleeve-/breaker- orders.

    ``known_pairs_upper`` is the set of uppercase pair slugs the watch
    understands (``BTC/USD``, ``IWM`` …). Order symbols are matched by the
    compacted form (pair without ``/``) so a broker returning ``BTCUSD``
    lines up with the universe pair ``BTC/USD``. A raising ``list_orders``
    degrades to an empty dict — the reconciliation simply treats the pair
    as not pending.
    """
    if not hasattr(broker, "list_orders"):
        return {}
    try:
        open_orders = broker.list_orders(status="open")
    except Exception:  # noqa: BLE001 - degraded broker: report nothing pending
        return {}
    compact_to_pair: dict[str, str] = {
        pair.replace("/", ""): pair for pair in known_pairs_upper
    }
    pending: dict[str, list[str]] = {}
    for order in open_orders or []:
        client_order_id = str(_coerce_position_value(order, "client_order_id", default="") or "")
        if not (
            client_order_id.startswith("sleeve-")
            or client_order_id.startswith("breaker-")
        ):
            continue
        symbol = str(_coerce_position_value(order, "symbol", default="") or "").upper()
        if not symbol:
            continue
        pair = compact_to_pair.get(symbol.replace("/", ""))
        if pair is None:
            continue
        pending.setdefault(pair, []).append(client_order_id)
    return pending


def _is_drift(target: float, actual: float) -> bool:
    """Return True when |actual - target| exceeds the dual drift threshold.

    Threshold = ``max(50.0, 0.20 * max(target, actual))``. Both legs count
    positive and negative drift (target $1000 with actual $600 fires the
    same as target $600 with actual $1000).
    """
    threshold = max(DRIFT_ABSOLUTE_FLOOR_USD, DRIFT_RELATIVE_FLOOR_FRACTION * max(target, actual))
    return abs(actual - target) > threshold


def _reconcile_positions(
    *,
    cycles_dir: Path,
    as_of: date,
    positions: list[Mapping[str, object]],
    broker: Any,
) -> tuple[dict[str, object], list[str], list[str]]:
    """Build the reconciliation payload between cycle targets and broker positions.

    Returns ``(reconciliation_payload, incident_slugs, pending_events)``:

    - ``reconciliation_payload`` — ``{checked, drifts, pending}`` describing
      how many pairs were checked, which drifted, and which had pending
      orders (no incident — the in-flight order explains the gap).
    - ``incident_slugs`` — ``position_drift:<pair>:target=<t>:actual=<a>``
      strings, one per drift. These become WARN-level incidents.
    - ``pending_events`` — ``drift_pending_order:<pair>`` strings for
      informational surfacing only; they are NOT incidents.

    Failures degrade gracefully: unreadable cycle files become
    ``cycle_file_unreadable:<name>`` incidents, never crashes.
    """
    cycle_paths = _latest_cycle_per_sleeve(cycles_dir, as_of=as_of)
    targets_by_pair: dict[str, float] = {}
    cycle_incidents: list[str] = []
    for sleeve, cycle_path in sorted(cycle_paths.items()):
        loaded = _load_plan_targets(cycle_path)
        if not loaded and cycle_path.stat().st_size > 0:  # empty file = no plan, not unreadable
            # distinguish "no targets" (empty plan) from "could not parse".
            # If the file is non-empty but produced no targets, record it.
            try:
                payload = json.loads(cycle_path.read_text(encoding="utf-8"))
                plan_entries = payload.get("plan") if isinstance(payload, Mapping) else None
                if plan_entries is not None:
                    cycle_incidents.append(f"cycle_file_unreadable:{cycle_path.name}")
            except (OSError, ValueError):
                cycle_incidents.append(f"cycle_file_unreadable:{cycle_path.name}")
        for pair, target in loaded.items():
            # If multiple sleeves overlap on a pair, keep the larger target
            # (the most aggressive position is what would fail first).
            if pair not in targets_by_pair or target > targets_by_pair[pair]:
                targets_by_pair[pair] = target
        # Reference ``sleeve`` so the variable is read; suppresses lint about
        # unused loop variable while keeping the iteration order deterministic.
        _ = sleeve

    # Build actuals from broker positions, mapped by compact symbol.
    compact_to_pair: dict[str, str] = {
        pair.replace("/", ""): pair for pair in targets_by_pair
    }
    actual_by_pair: dict[str, float] = {pair: 0.0 for pair in targets_by_pair}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if not symbol:
            continue
        pair = compact_to_pair.get(symbol.replace("/", ""))
        if pair is None:
            continue
        market_value = _coerce_optional_float(position.get("market_value"))
        if market_value is None:
            continue
        # If the same compact symbol somehow appears twice, sum the
        # market_value — broker splits are unusual but defensive.
        actual_by_pair[pair] = actual_by_pair.get(pair, 0.0) + market_value

    pending_by_pair = _open_orders_by_pair(
        broker,
        known_pairs_upper=set(targets_by_pair.keys()),
    )

    drifts: list[dict[str, object]] = []
    drift_incidents: list[str] = []
    pending_events: list[str] = []
    checked = 0
    for pair in sorted(targets_by_pair):
        target = targets_by_pair[pair]
        actual = actual_by_pair.get(pair, 0.0)
        checked += 1
        # Pairs with zero target AND zero actual are not interesting; skip
        # the entry entirely to keep the payload tight.
        if target == 0.0 and actual == 0.0:
            continue
        if pending_by_pair.get(pair):
            pending_events.append(f"drift_pending_order:{pair}")
            continue
        if _is_drift(target, actual):
            drifts.append(
                {
                    "pair": pair,
                    "target": round(target, 2),
                    "actual": round(actual, 2),
                    "delta": round(actual - target, 2),
                }
            )
            drift_incidents.append(
                f"position_drift:{pair}:target={round(target, 2)}:actual={round(actual, 2)}"
            )

    reconciliation = {
        "checked": checked,
        "drifts": drifts,
        "pending": pending_events,
    }
    return reconciliation, drift_incidents + cycle_incidents, pending_events


def _render_telegram_message(
    *,
    as_of: date,
    positions: list[Mapping[str, object]],
    fills: list[Mapping[str, object]],
    risk_context: Mapping[str, object] | None,
    status: str,
    warnings: list[str],
    cycle_incident: str | None = None,
    expired_orders: list[Mapping[str, object]] | None = None,
    pending_events: list[str] | None = None,
    extra_aviso: list[str] | None = None,
) -> str:
    """Build the multiline Telegram message body (Telegram-safe ASCII).

    ``warnings`` are the risk-side warnings (kill-switch proximity). ``extra_aviso``
    carries every other WARN-level incident the operator needs to read —
    typically the drift / expired / cycle-file slugs from the M13 payloads.
    Both lists render as ``AVISO: <slug>`` lines.
    """
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
    if cycle_incident is not None:
        # M11 watchdog: report missing daily cycle as a dedicated "AVISO"
        # line with the last seen date, e.g. "AVISO: ciclo diario ausente
        # desde 2026-07-08".
        if cycle_incident == "daily_cycle_missing:none":
            lines.append("AVISO: ciclo diario ausente (sin ciclos previos)")
        else:
            parts = cycle_incident.split(":", 1)
            last_seen = parts[1] if len(parts) == 2 else "?"
            lines.append(f"AVISO: ciclo diario ausente desde {last_seen}")
    # M13 WS4: expired DAY orders surface with a dedicated line so the
    # operator knows to expect a re-plan on the next cycle.
    if expired_orders:
        for entry in expired_orders:
            client_order_id = str(entry.get("client_order_id") or "?")
            lines.append(f"AVISO: orden expirada {client_order_id} (el ciclo re-planeará)")
    if pending_events:
        for event in pending_events:
            # ``drift_pending_order:<pair>`` — informational only: an
            # in-flight order is the explanation for any size gap.
            pair = event.split(":", 1)[1] if ":" in event else "?"
            lines.append(f"AVISO: drift pendiente por orden en vuelo {pair}")
    if status == PAPER_WARN:
        for blocker in warnings:
            lines.append(f"AVISO: {blocker}")
        for extra in extra_aviso or []:
            lines.append(f"AVISO: {extra}")
    return "\n".join(lines)


def run_sleeve_position_watch(
    *,
    risk_config: str | Path,
    output: str | Path,
    telegram_artifact: str | Path | None = None,
    broker: Any,
    as_of_date: date | None = None,
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    cycles_dir: str | Path | None = None,
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

    fills, expired_orders, order_incidents = _collect_orders_today(broker, as_of=as_of)
    incidents.extend(order_incidents)
    # Expired sleeve-/breaker- orders are informative incidents: each one
    # produces a ``order_expired:<client_order_id>`` slug. They still WARN
    # — the operator wants to know that a DAY order timed out unfilled.
    for entry in expired_orders:
        client_order_id = str(entry.get("client_order_id") or "?")
        incidents.append(f"order_expired:{client_order_id}")

    risk_context = _account_risk_context(broker, Path(equity_highwater_path))
    if risk_context is None:
        incidents.append("account_risk_context_unavailable")

    risk_warnings = _resolve_risk_warnings(
        risk_context,
        max_daily_loss_pct=risk.max_daily_loss_pct,
        max_drawdown_pct=risk.max_drawdown_pct,
    )

    # M13 WS4 reconciliation: when --cycles-dir is provided, compare the
    # latest per-sleeve cycle plan against the live broker positions and
    # surface drift as incidents. Pending orders explain the gap and are
    # not incidents themselves.
    reconciliation: dict[str, object] | None = None
    pending_events: list[str] = []
    if cycles_dir is not None:
        reconciliation, recon_incidents, pending_events = _reconcile_positions(
            cycles_dir=Path(cycles_dir),
            as_of=as_of,
            positions=positions,
            broker=broker,
        )
        incidents.extend(recon_incidents)

    all_incidents = sorted(set(incidents + risk_warnings))
    blockers = all_incidents
    if all_incidents:
        status = PAPER_WARN
    else:
        status = PAPER_OK

    # M11 watchdog: when --cycles-dir is provided, surface a daily_cycle_missing
    # incident if we haven't seen a cycle for today or yesterday.
    cycle_incident: str | None = None
    if cycles_dir is not None:
        cycle_incident = _check_daily_cycle(Path(cycles_dir), as_of=as_of)
        if cycle_incident is not None:
            all_incidents = sorted(set(all_incidents + [cycle_incident]))
            blockers = all_incidents
            status = PAPER_WARN

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "positions": positions,
        "fills_today": fills,
        "expired_orders": expired_orders,
        "account_risk": risk_context,
        "incidents": all_incidents,
        "blockers": blockers,
        "status": status,
        "safety": {"read_only": True, "orders_submitted": False},
    }
    if reconciliation is not None:
        payload["reconciliation"] = reconciliation
    write_json_artifact(payload, output_path)

    if telegram_artifact is not None:
        # Risk warnings (kill-switch proximity) are surfaced separately;
        # every other WARN incident — drift, expired, cycle_file_unreadable,
        # cycle_missing — is rolled into ``extra_aviso`` so the operator
        # sees the full WARN picture without us hand-rendering each shape.
        risk_only_warnings = set(risk_warnings)
        extra_aviso = sorted(set(all_incidents) - risk_only_warnings)
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
                cycle_incident=cycle_incident,
                expired_orders=expired_orders,
                pending_events=pending_events,
                extra_aviso=extra_aviso,
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
    "DRIFT_ABSOLUTE_FLOOR_USD",
    "DRIFT_RELATIVE_FLOOR_FRACTION",
    "SleevePositionWatchResult",
    "run_sleeve_position_watch",
]
