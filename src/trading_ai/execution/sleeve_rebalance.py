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

import time
from collections.abc import Callable, Iterable
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
DEFAULT_EQUITY_HIGHWATER_PATH = "reports/tmp/sleeve_rebalance/equity_highwater.json"

# Limit-maker (M10): the resting side of the spread, expressed in bps from
# the live trade. A 1 bp resting limit buys 1 bp of spread AND drops the
# execution from taker (≈25 bps) to maker (≈15 bps), which is most of the
# 10 bp cost edge the §30 backtest already validates against.
LIMIT_MAKER_OFFSET_BPS = 1.0
LIMIT_WAIT_SECONDS_DEFAULT = 180
LIMIT_POLL_SECONDS = 10

# Status codes surfaced on limit-maker submissions.
STYLE_LIMIT_MAKER = "limit-maker"
STYLE_MARKET = "market"
FILLED_VIA_LIMIT = "limit"
FILLED_VIA_MARKET_FALLBACK = "market_fallback"
FILLED_VIA_LIMIT_PARTIAL = "limit_partial"


def _is_crypto_pair(pair: str) -> bool:
    """Sleeve-side crypto check (independent of the Alpaca adapter helper)."""
    return "/" in pair


@dataclass(frozen=True)
class SleeveRebalanceResult:
    exit_code: int
    status: str  # "REPORT_ONLY" | "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def map_broker_symbol_to_pair(symbol: str, universe_symbols: Iterable[str]) -> str | None:
    """Map an Alpaca-style broker symbol ("BTCUSD") to its universe pair ("BTC/USD").

    Broker surfaces may return either notation ("BTCUSD" positions,
    "BTC/USD" orders), so lookup is by the compacted form of both sides.
    """
    by_compact = {pair.replace("/", ""): pair for pair in universe_symbols}
    return by_compact.get(symbol.upper().replace("/", ""))


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


def _load_high_water(path: Path) -> float | None:
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        value = float(payload.get("high_water_equity", 0.0))
        return value if value > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def _store_high_water(path: Path, value: float) -> None:
    write_json_artifact(
        {
            "high_water_equity": round(float(value), 2),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        path,
    )


def _account_risk_context(broker: Any, high_water_path: Path) -> dict[str, float] | None:
    """Real account risk inputs for the broker's kill-switch evaluation.

    Returns None when the account cannot be read or reports no equity — in
    that case the caller must NOT submit orders with fake 0.0 risk inputs,
    because that silently disarms the daily-loss and drawdown kill-switches.
    """
    try:
        account = broker.read_account()
        equity = float(getattr(account, "equity", 0.0))
    except Exception:  # noqa: BLE001 - any broker failure means "no reliable context"
        return None
    if equity <= 0:
        return None
    last_equity = float(getattr(account, "last_equity", 0.0) or 0.0)
    daily_pnl_pct = (equity - last_equity) / last_equity if last_equity > 0 else 0.0
    stored = _load_high_water(high_water_path)
    high_water = max(stored or 0.0, equity)
    current_drawdown_pct = (high_water - equity) / high_water if high_water > 0 else 0.0
    _store_high_water(high_water_path, high_water)
    return {
        "equity": round(equity, 6),
        "last_equity": round(last_equity, 6),
        "daily_pnl_pct": round(daily_pnl_pct, 6),
        "high_water_equity": round(high_water, 6),
        "current_drawdown_pct": round(current_drawdown_pct, 6),
    }


def _open_buy_notional_by_pair(broker: Any, universe_symbols: Iterable[str]) -> dict[str, float]:
    """Sum the notional of OPEN buy orders per universe pair.

    A submitted-but-unfilled buy (e.g. queued for the next equity open, or
    pending over a weekend) is committed exposure the position list does not
    show yet. Counting it as current value stops a later cycle from
    re-submitting the same delta and doubling the position once both fill.
    Sells are intentionally not netted (a duplicate exit fails on quantity at
    the broker; a duplicate entry silently doubles risk).
    """
    if not hasattr(broker, "list_orders"):
        return {}
    try:
        open_orders = broker.list_orders(status="open")
    except Exception:  # noqa: BLE001 - degraded broker: fail toward reporting nothing extra
        return {}
    pending: dict[str, float] = {}
    for order in open_orders:
        side = str(getattr(order, "side", "")).lower()
        if side != "buy":
            continue
        symbol = str(getattr(order, "symbol", "")).upper()
        pair = map_broker_symbol_to_pair(symbol, universe_symbols)
        if pair is None:
            continue
        notional = getattr(order, "notional", None)
        try:
            value = float(notional) if notional is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            pending[pair] = pending.get(pair, 0.0) + value
    return pending


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


def _record_market_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    order: PaperOrder,
    broker: Any,
    style: str | None = None,
) -> dict[str, object]:
    try:
        result = broker.submit_order(order)
    except Exception as exc:  # noqa: BLE001 - broker surface
        record: dict[str, object] = {
            "pair": pair,
            "action": action,
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "error",
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
        if style is not None:
            record["style"] = style
        return record
    accepted_attr = getattr(result, "accepted", False)
    status_attr = getattr(result, "status", "unknown")
    reasons_attr = getattr(result, "reasons", ())
    record = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": bool(accepted_attr),
        "skipped": False,
        "status": str(status_attr),
        "reasons": list(reasons_attr) if isinstance(reasons_attr, (tuple, list)) else [str(reasons_attr)],
    }
    if style is not None:
        record["style"] = style
    return record


def _record_limit_maker_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    result: Any,
    style: str,
    filled_via: str | None = None,
    style_note: str | None = None,
    limit_filled_notional: float | None = None,
    market_client_order_id: str | None = None,
) -> dict[str, object]:
    accepted_attr = getattr(result, "accepted", False)
    status_attr = getattr(result, "status", "unknown")
    reasons_attr = getattr(result, "reasons", ())
    record: dict[str, object] = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": bool(accepted_attr),
        "skipped": False,
        "status": str(status_attr),
        "reasons": list(reasons_attr) if isinstance(reasons_attr, (tuple, list)) else [str(reasons_attr)],
        "style": style,
    }
    if market_client_order_id is not None:
        record["market_client_order_id"] = market_client_order_id
    if filled_via is not None:
        record["filled_via"] = filled_via
    if style_note is not None:
        record["style_note"] = style_note
    if limit_filled_notional is not None:
        record["limit_filled_notional"] = round(float(limit_filled_notional), 4)
    return record


def _record_error_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    error: BaseException,
    style: str,
    filled_via: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": False,
        "skipped": False,
        "status": "error",
        "reasons": [f"{type(error).__name__}: {error}"],
        "style": style,
    }
    if filled_via is not None:
        record["filled_via"] = filled_via
    return record


def _limit_maker_offset_factor(side: str) -> float:
    return 1.0 - (LIMIT_MAKER_OFFSET_BPS / 1e4) if side == "buy" else 1.0 + (LIMIT_MAKER_OFFSET_BPS / 1e4)


def _poll_limit_until_filled_or_timeout(
    *,
    broker: Any,
    limit_id: str,
    deadline: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> Any | None:
    """Poll ``broker.get_order_by_client_id(limit_id)`` until filled or deadline.

    Returns the snapshot if it reads as "filled", else ``None``. Time is read
    via the injected ``now`` clock and ``sleep`` waits between polls — both
    are fakeable for offline tests.
    """
    while now() < deadline:
        sleep(LIMIT_POLL_SECONDS)
        try:
            snapshot = broker.get_order_by_client_id(limit_id)
        except Exception:  # noqa: BLE001 - degraded broker: keep polling until deadline
            continue
        status = str(getattr(snapshot, "status", "") or "").lower()
        if status == "filled":
            return snapshot
    return None


def _execute_submissions(
    *,
    plan: list[dict[str, object]],
    broker: Any,
    as_of_date: date,
    universe_name: str,
    risk_context: dict[str, float] | None = None,
    gross_current: float = 0.0,
    order_style: str = "market",
    limit_wait_seconds: int = LIMIT_WAIT_SECONDS_DEFAULT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> list[dict[str, object]]:
    equity = float(risk_context["equity"]) if risk_context else 0.0
    daily_pnl_pct = float(risk_context["daily_pnl_pct"]) if risk_context else 0.0
    current_drawdown_pct = float(risk_context["current_drawdown_pct"]) if risk_context else 0.0
    submissions: list[dict[str, object]] = []
    use_limit_maker = order_style == "limit-maker"
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
        id_base = f"sleeve-{as_of_date.isoformat()}-{pair.replace('/', '')}-{side}"
        client_order_id = id_base  # byte-identical pre-M10 default
        order_kwargs: dict[str, Any] = {
            "symbol": pair,
            "side": side,
            "client_order_id": client_order_id,
            "reference_price": reference_price,
        }
        # Real account risk inputs so the broker's evaluate_risk_state can
        # actually trip the daily-loss/drawdown/position kill-switches.
        order_value = 0.0
        if action == "sell_all" and quantity_value is not None:
            try:
                ref = float(reference_price) if reference_price is not None else 0.0
                order_value = abs(float(quantity_value)) * ref
            except (TypeError, ValueError):
                order_value = 0.0
        elif notional_value is not None:
            try:
                order_value = abs(float(notional_value))
            except (TypeError, ValueError):
                order_value = 0.0
        if equity > 0:
            order_kwargs["daily_pnl_pct"] = daily_pnl_pct
            order_kwargs["current_drawdown_pct"] = current_drawdown_pct
            order_kwargs["estimated_position_weight"] = order_value / equity
            order_kwargs["projected_gross_exposure"] = (gross_current + order_value) / equity
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

        # ----- DEFAULT MARKET PATH (byte-identical pre-M10 behavior) -----
        # The default ``order_style="market"`` path is intentionally unchanged:
        # same ids (no ``-lim`` / ``-mkt`` suffix), same record shape, no new
        # ``style`` field. ``-lim``/``-mkt`` suffixes are only emitted on the
        # limit-maker path below.
        if not use_limit_maker or not _is_crypto_pair(pair):
            order = PaperOrder(**order_kwargs)
            record_style = STYLE_MARKET if use_limit_maker else None
            submissions.append(
                _record_market_submission(
                    pair=pair,
                    action=action,
                    client_order_id=client_order_id,
                    order=order,
                    broker=broker,
                    style=record_style,
                )
            )
            continue

        # ----- LIMIT-MAKER PATH (crypto pairs only) -----
        limit_id = f"{id_base}-lim"

        # a. Try to read the latest live trade price; degraded broker → None.
        price: float | None
        try:
            price = broker.latest_trade_price(pair)
        except Exception:  # noqa: BLE001 - degraded broker surface
            price = None

        if price is None or price <= 0:
            # Fall back to market with a style_note explaining why we did not
            # even try a limit. Same id (no ``-lim`` suffix).
            market_kwargs = dict(order_kwargs)
            market_kwargs["client_order_id"] = id_base
            market_order = PaperOrder(**market_kwargs)
            try:
                market_result = broker.submit_order(market_order)
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=id_base,
                        result=market_result,
                        style=STYLE_MARKET,
                        style_note="limit_price_unavailable",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=id_base,
                        error=exc,
                        style=STYLE_MARKET,
                    )
                )
            continue

        # b. Resting limit price on the maker side of the spread.
        limit_price = round(float(price) * _limit_maker_offset_factor(side), 4)

        # c. Submit the limit order. Broker-side rejection (risk / allowlist
        # / validation) is recorded verbatim — no fallback: the rejection is
        # not a liquidity problem.
        limit_kwargs = dict(order_kwargs)
        limit_kwargs["client_order_id"] = limit_id
        limit_kwargs["order_type"] = "limit"
        limit_kwargs["limit_price"] = limit_price
        limit_order = PaperOrder(**limit_kwargs)
        try:
            limit_result = broker.submit_order(limit_order)
        except Exception as exc:  # noqa: BLE001
            submissions.append(
                _record_error_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    error=exc,
                    style=STYLE_LIMIT_MAKER,
                )
            )
            continue
        if not bool(getattr(limit_result, "accepted", False)):
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                )
            )
            continue

        # d. Poll until filled or timeout. Time / sleep are injected so tests
        # can drive the loop deterministically without touching the wall clock.
        deadline = now() + float(limit_wait_seconds)
        filled_snapshot = _poll_limit_until_filled_or_timeout(
            broker=broker,
            limit_id=limit_id,
            deadline=deadline,
            sleep=sleep,
            now=now,
        )
        if filled_snapshot is not None:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT,
                )
            )
            continue

        # e. Timeout → cancel and re-read state; the limit may have filled in
        # the race window between the last poll and the cancel request.
        try:
            broker.cancel_order(client_order_id=limit_id)
        except Exception:  # noqa: BLE001 - cancel failure must not abort the cycle
            pass
        try:
            final_snapshot = broker.get_order_by_client_id(limit_id)
        except Exception:  # noqa: BLE001
            final_snapshot = None
        final_status = ""
        if final_snapshot is not None:
            final_status = str(getattr(final_snapshot, "status", "") or "").lower()
        if final_status == "filled":
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT,
                )
            )
            continue

        # Compute the remainder using the post-cancel snapshot's fill detail.
        filled_qty = 0.0
        filled_avg: float | None = None
        if final_snapshot is not None:
            try:
                filled_qty = float(getattr(final_snapshot, "filled_quantity", 0.0) or 0.0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            filled_avg_value = getattr(final_snapshot, "filled_avg_price", None)
            if filled_avg_value is not None:
                try:
                    filled_avg = float(filled_avg_value)
                except (TypeError, ValueError):
                    filled_avg = None
        if filled_qty > 0 and filled_avg is not None:
            limit_filled_notional = filled_qty * filled_avg
        else:
            limit_filled_notional = 0.0

        if action == "sell_all":
            try:
                original_qty = float(quantity_value) if quantity_value is not None else 0.0
            except (TypeError, ValueError):
                original_qty = 0.0
            remainder_qty = max(0.0, original_qty - filled_qty)
            if remainder_qty < 1e-9:
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=limit_id,
                        result=limit_result,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_LIMIT_PARTIAL,
                    )
                )
                continue
            mkt_id = f"{id_base}-mkt"
            mkt_kwargs = dict(order_kwargs)
            mkt_kwargs["client_order_id"] = mkt_id
            mkt_kwargs["quantity"] = remainder_qty
            mkt_order = PaperOrder(**mkt_kwargs)
            try:
                mkt_result = broker.submit_order(mkt_order)
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=limit_id,
                        result=mkt_result,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                        limit_filled_notional=limit_filled_notional,
                        market_client_order_id=mkt_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=mkt_id,
                        error=exc,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                    )
                )
            continue

        # buy / sell partial → compute remainder by notional (floor at 0).
        try:
            original_notional = float(notional_value) if notional_value is not None else 0.0
        except (TypeError, ValueError):
            original_notional = 0.0
        remainder = max(0.0, original_notional - limit_filled_notional)
        if remainder < MIN_DELTA_USD:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT_PARTIAL,
                )
            )
            continue
        if remainder < CRYPTO_MIN_NOTIONAL_USD_DEFAULT:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT_PARTIAL,
                    style_note="fallback_below_min",
                )
            )
            continue
        mkt_id = f"{id_base}-mkt"
        mkt_kwargs = dict(order_kwargs)
        mkt_kwargs["client_order_id"] = mkt_id
        mkt_kwargs["notional"] = round(remainder, 2)
        mkt_order = PaperOrder(**mkt_kwargs)
        try:
            mkt_result = broker.submit_order(mkt_order)
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=mkt_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_MARKET_FALLBACK,
                    limit_filled_notional=limit_filled_notional,
                    market_client_order_id=mkt_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=mkt_id,
                        error=exc,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                    )
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
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    order_style: str = "market",
    limit_wait_seconds: int = LIMIT_WAIT_SECONDS_DEFAULT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> SleeveRebalanceResult:
    """Run the governed crypto-sleeve rebalance cycle (report-only by default)."""

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()
    blockers: list[str] = []

    if order_style not in {"market", "limit-maker"}:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": ["invalid_order_style"],
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
        pending_buy_by_pair: dict[str, float] = {}
    else:
        current_by_pair, ignored_positions = _read_broker_positions_by_pair(broker, universe.symbols)
        pending_buy_by_pair = _open_buy_notional_by_pair(broker, universe.symbols)
        for pair, pending_value in pending_buy_by_pair.items():
            current_by_pair[pair] = current_by_pair.get(pair, 0.0) + pending_value
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

    risk_context: dict[str, float] | None = None
    if broker is not None:
        risk_context = _account_risk_context(broker, Path(equity_highwater_path))

    if confirm_submit and broker is not None and risk_context is None:
        # Fail-closed: submitting with fake 0.0 daily-loss/drawdown inputs
        # silently disarms the kill-switches — block instead.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of_iso,
            "universe": universe.name,
            "dataset": str(dataset),
            "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
            "plan": plan,
            "pending_buy_notional": {pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())},
            "ignored_positions": sorted(set(ignored_positions)),
            "submissions": [],
            "account_risk": None,
            "blockers": ["account_risk_context_unavailable"],
            "status": PAPER_BLOCKED,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": True,
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

    orders_submitted = False
    submissions: list[dict[str, object]] = []
    if confirm_submit and broker is not None:
        submissions = _execute_submissions(
            plan=plan,
            broker=broker,
            as_of_date=as_of,
            universe_name=universe.name,
            risk_context=risk_context,
            gross_current=sum(abs(value) for value in current_by_pair.values()),
            order_style=order_style,
            limit_wait_seconds=limit_wait_seconds,
            sleep=sleep,
            now=now,
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
            "order_style": str(order_style),
        },
        "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
        "plan": plan,
        "pending_buy_notional": {pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())},
        "ignored_positions": sorted(set(ignored_positions)),
        "account_risk": risk_context,
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
