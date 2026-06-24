"""Persistent paper risk state for live risk-gate inputs and the kill-switch.

This module keeps a small JSON file (default ``reports/tmp/paper_risk_state.json``)
with the account equity baselines needed to turn the previously dormant
``evaluate_risk_state`` limits into real, account-driven gates, plus the
persisted kill-switch flags consumed by the daily operator.

Design notes:
- The state is account-scoped, not session-scoped, so it persists across cron runs.
- Risk inputs are computed for *new exposure* (buy orders) only. Sell/close orders
  return benign zeros so protective or rotation exits are never trapped by the
  daily-loss or drawdown limits (the limits exist to throttle new risk, not to
  block de-risking).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

SCHEMA_VERSION = "1.0"
DEFAULT_RISK_STATE_PATH = "reports/tmp/paper_risk_state.json"


@dataclass(frozen=True)
class RiskState:
    """Account-scoped risk baselines and kill-switch flags."""

    as_of_date: str | None = None
    opening_equity: float | None = None
    peak_equity: float | None = None
    last_equity: float | None = None
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    kill_switch_tripped_at: str | None = None
    consecutive_error_days: int = 0
    trailing_stops: dict[str, float] = field(default_factory=dict)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "as_of_date": self.as_of_date,
            "opening_equity": self.opening_equity,
            "peak_equity": self.peak_equity,
            "last_equity": self.last_equity,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reason": self.kill_switch_reason,
            "kill_switch_tripped_at": self.kill_switch_tripped_at,
            "consecutive_error_days": self.consecutive_error_days,
            "trailing_stops": dict(self.trailing_stops),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RiskState:
        trailing_raw = payload.get("trailing_stops")
        trailing: dict[str, float] = {}
        if isinstance(trailing_raw, Mapping):
            for symbol, value in trailing_raw.items():
                level = _float_or_none(value)
                if level is not None:
                    trailing[str(symbol).upper()] = level
        return cls(
            as_of_date=_str_or_none(payload.get("as_of_date")),
            opening_equity=_float_or_none(payload.get("opening_equity")),
            peak_equity=_float_or_none(payload.get("peak_equity")),
            last_equity=_float_or_none(payload.get("last_equity")),
            kill_switch_active=bool(payload.get("kill_switch_active", False)),
            kill_switch_reason=_str_or_none(payload.get("kill_switch_reason")),
            kill_switch_tripped_at=_str_or_none(payload.get("kill_switch_tripped_at")),
            consecutive_error_days=_int_value(payload.get("consecutive_error_days")),
            trailing_stops=trailing,
            updated_at=_str_or_none(payload.get("updated_at")),
        )


@dataclass(frozen=True)
class OrderRiskInputs:
    daily_pnl_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    projected_gross_exposure: float = 0.0
    estimated_position_weight: float = 0.0

    def as_order_kwargs(self) -> dict[str, float]:
        return {
            "daily_pnl_pct": self.daily_pnl_pct,
            "current_drawdown_pct": self.current_drawdown_pct,
            "projected_gross_exposure": self.projected_gross_exposure,
            "estimated_position_weight": self.estimated_position_weight,
        }


def load_risk_state(path: str | Path = DEFAULT_RISK_STATE_PATH) -> RiskState:
    state_path = Path(path)
    if not state_path.exists():
        return RiskState()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return RiskState()
    if not isinstance(payload, Mapping):
        return RiskState()
    return RiskState.from_dict(payload)


def save_risk_state(state: RiskState, path: str | Path = DEFAULT_RISK_STATE_PATH) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stamped = replace(state, updated_at=datetime.now(UTC).isoformat())
    state_path.write_text(json.dumps(stamped.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def roll_daily_equity(state: RiskState, *, equity: float, as_of_date: str | date) -> RiskState:
    """Refresh equity baselines for the current trading day.

    On the first observation of a new day, ``opening_equity`` resets to the
    current equity. ``peak_equity`` is the all-time high-water mark.
    """

    iso_date = as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)
    if equity <= 0:
        # Cannot establish meaningful baselines (e.g. dry-run account); keep state.
        return replace(state, last_equity=equity)
    new_day = state.as_of_date != iso_date or state.opening_equity is None
    opening = equity if new_day else state.opening_equity
    peak = equity if state.peak_equity is None else max(state.peak_equity, equity)
    return replace(
        state,
        as_of_date=iso_date,
        opening_equity=opening,
        peak_equity=peak,
        last_equity=equity,
    )


def compute_order_risk_inputs(
    *,
    side: str,
    symbol: str,
    notional: float | None,
    quantity: float | None,
    account_equity: float,
    positions: Sequence[object],
    state: RiskState,
) -> OrderRiskInputs:
    """Build the four risk-gate inputs for an order.

    Sells/closes always return benign zeros so de-risking exits are never blocked
    by the daily-loss, drawdown, exposure or single-position limits.
    """

    if side.strip().lower() != "buy":
        return OrderRiskInputs()
    if account_equity <= 0:
        return OrderRiskInputs()

    opening = state.opening_equity if state.opening_equity and state.opening_equity > 0 else account_equity
    peak = state.peak_equity if state.peak_equity and state.peak_equity > 0 else account_equity

    daily_pnl_pct = (account_equity - opening) / opening
    current_drawdown_pct = max(0.0, (peak - account_equity) / peak)

    order_value = _order_value(notional=notional, quantity=quantity, positions=positions, symbol=symbol)
    current_gross = sum(abs(_market_value(position)) for position in positions)
    projected_gross_exposure = (current_gross + order_value) / account_equity

    symbol_value = _symbol_market_value(positions, symbol) + order_value
    estimated_position_weight = symbol_value / account_equity

    return OrderRiskInputs(
        daily_pnl_pct=daily_pnl_pct,
        current_drawdown_pct=current_drawdown_pct,
        projected_gross_exposure=projected_gross_exposure,
        estimated_position_weight=estimated_position_weight,
    )


def trip_kill_switch(state: RiskState, *, reason: str) -> RiskState:
    if state.kill_switch_active:
        return state
    return replace(
        state,
        kill_switch_active=True,
        kill_switch_reason=reason,
        kill_switch_tripped_at=datetime.now(UTC).isoformat(),
    )


def reset_kill_switch(state: RiskState) -> RiskState:
    return replace(
        state,
        kill_switch_active=False,
        kill_switch_reason=None,
        kill_switch_tripped_at=None,
        consecutive_error_days=0,
    )


def evaluate_kill_switch(
    state: RiskState,
    *,
    max_drawdown_pct: float,
    equity: float | None = None,
    max_consecutive_error_days: int = 0,
) -> RiskState:
    """Latch the kill-switch when the account drawdown or error streak breaches limits.

    The switch is a latching "safe mode": once tripped it stays active until an
    operator explicitly resets it (e.g. via ``paper-safe-flatten``). It is a
    stronger control than the per-order drawdown gate, which re-evaluates each
    run and would silently re-enable trading once equity recovers.
    """

    if state.kill_switch_active:
        return state
    if max_consecutive_error_days > 0 and state.consecutive_error_days >= max_consecutive_error_days:
        return trip_kill_switch(
            state,
            reason=f"consecutive_error_days_{state.consecutive_error_days}_reached_{max_consecutive_error_days}",
        )
    current = equity if equity is not None else state.last_equity
    peak = state.peak_equity
    if current is None or peak is None or peak <= 0 or current <= 0:
        return state
    drawdown = (peak - current) / peak
    if max_drawdown_pct > 0 and drawdown >= max_drawdown_pct:
        return trip_kill_switch(
            state,
            reason=f"account_drawdown_{drawdown:.4f}_breached_limit_{max_drawdown_pct:.4f}",
        )
    return state


def _order_value(
    *,
    notional: float | None,
    quantity: float | None,
    positions: Sequence[object],
    symbol: str,
) -> float:
    if notional is not None:
        return abs(float(notional))
    if quantity is not None:
        # Approximate quantity orders using the current market value per share.
        price = _symbol_price(positions, symbol)
        if price is not None:
            return abs(float(quantity)) * price
    return 0.0


def _market_value(position: object) -> float:
    return _float_or_none(_get(position, "market_value", 0.0)) or 0.0


def _symbol_market_value(positions: Sequence[object], symbol: str) -> float:
    target = symbol.upper()
    for position in positions:
        if str(_get(position, "symbol", "")).upper() == target:
            return abs(_market_value(position))
    return 0.0


def _symbol_price(positions: Sequence[object], symbol: str) -> float | None:
    target = symbol.upper()
    for position in positions:
        if str(_get(position, "symbol", "")).upper() != target:
            continue
        price = _float_or_none(_get(position, "current_price", None))
        if price is not None and price > 0:
            return price
        quantity = _float_or_none(_get(position, "quantity", _get(position, "qty", None)))
        market_value = _float_or_none(_get(position, "market_value", None))
        if quantity and market_value is not None and quantity != 0:
            return abs(market_value / quantity)
    return None


def _get(value: object, key: str, default: object) -> object:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, (Mapping, list, tuple)):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
