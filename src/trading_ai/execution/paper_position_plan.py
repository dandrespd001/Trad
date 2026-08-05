"""Paper-only dynamic position planning from model signals and broker positions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from trading_ai.execution.position_sizing import FIXED_NOTIONAL, compute_open_notional


def build_position_plan(
    *,
    signals: Sequence[object],
    selected_signal: object | None,
    positions: Sequence[object],
    signal_quality: Mapping[str, object] | None,
    paper_notional_usd: float,
    stop_loss_atr_mult: float = 0.0,
    take_profit_atr_mult: float = 0.0,
    trailing_atr_mult: float = 0.0,
    breakeven_trigger_atr_mult: float = 0.0,
    breakeven_buffer_atr_mult: float = 0.0,
    trailing_high_by_symbol: Mapping[str, float] | None = None,
    sizing_mode: str = FIXED_NOTIONAL,
    account_equity: float = 0.0,
    target_volatility: float = 0.0,
    max_leverage: float = 1.0,
    max_single_position: float = 1.0,
    stage_cap_usd: float | None = None,
) -> dict[str, object]:
    signal_by_symbol = {_symbol(signal): signal for signal in signals if _symbol(signal)}
    selected_symbol = _symbol(selected_signal)
    selected_action = _action(selected_signal)
    signal_quality_allowed = signal_quality is None or signal_quality.get("allowed") is True
    actions: list[dict[str, object]] = []
    held_symbols = {_symbol(position) for position in positions if _symbol(position)}
    trailing_highs = _trailing_highs(positions, trailing_high_by_symbol)

    for position in positions:
        symbol = _symbol(position)
        if not symbol:
            continue
        symbol_signal = signal_by_symbol.get(symbol)
        symbol_action = _action(symbol_signal)
        quantity = _float_or_none(_get(position, "quantity", _get(position, "qty", None)))
        if quantity is not None and quantity < 0:
            actions.append(
                _close_action(
                    symbol=symbol,
                    quantity=quantity,
                    reason="unexpected_short_position",
                    signal=symbol_signal,
                )
            )
            continue
        atr = _float_or_none(_get(symbol_signal, "atr", None))
        trailing_high = trailing_highs.get(symbol)
        protective_levels = _protective_levels(
            position=position,
            atr=atr,
            stop_loss_atr_mult=stop_loss_atr_mult,
            take_profit_atr_mult=take_profit_atr_mult,
            trailing_atr_mult=trailing_atr_mult,
            trailing_high=trailing_high,
            breakeven_trigger_atr_mult=breakeven_trigger_atr_mult,
            breakeven_buffer_atr_mult=breakeven_buffer_atr_mult,
        )
        # Protective exits take priority over signal-driven hold/rotation.
        protective = _protective_exit_reason(
            position=position,
            atr=atr,
            stop_loss_atr_mult=stop_loss_atr_mult,
            take_profit_atr_mult=take_profit_atr_mult,
            trailing_atr_mult=trailing_atr_mult,
            trailing_high=trailing_high,
            breakeven_trigger_atr_mult=breakeven_trigger_atr_mult,
            breakeven_buffer_atr_mult=breakeven_buffer_atr_mult,
        )
        if protective is not None:
            actions.append(
                _close_action(
                    symbol=symbol,
                    quantity=quantity,
                    reason=protective,
                    signal=symbol_signal,
                    protective_levels=protective_levels,
                )
            )
            continue
        if symbol_action != "buy":
            actions.append(
                _close_action(
                    symbol=symbol,
                    quantity=quantity,
                    reason="signal_not_buy",
                    signal=symbol_signal,
                    protective_levels=protective_levels,
                )
            )
            continue
        if selected_symbol and symbol != selected_symbol and signal_quality_allowed:
            actions.append(
                _close_action(
                    symbol=symbol,
                    quantity=quantity,
                    reason="rotation_to_selected_signal",
                    signal=symbol_signal,
                    protective_levels=protective_levels,
                )
            )
            continue
        hold_action = {
            "action": "HOLD",
            "symbol": symbol,
            "reason": "position_matches_buy_signal",
            "quantity": quantity,
            "signal": _signal_summary(symbol_signal),
        }
        if protective_levels is not None:
            hold_action["protective_levels"] = protective_levels
        actions.append(hold_action)

    if (
        selected_symbol
        and selected_action == "buy"
        and signal_quality_allowed
        and selected_symbol not in held_symbols
    ):
        open_notional = compute_open_notional(
            sizing_mode=sizing_mode,
            paper_notional_usd=paper_notional_usd,
            account_equity=account_equity,
            realized_annual_volatility=_float_or_none(_get(selected_signal, "realized_volatility", None)),
            target_volatility=target_volatility,
            max_leverage=max_leverage,
            max_single_position=max_single_position,
            stage_cap_usd=stage_cap_usd,
        )
        actions.append(
            {
                "action": "OPEN",
                "symbol": selected_symbol,
                "side": "buy",
                "notional": float(open_notional),
                "reason": "selected_buy_signal",
                "signal": _signal_summary(selected_signal),
            }
        )

    close_count = sum(1 for action in actions if action.get("action") == "CLOSE")
    open_count = sum(1 for action in actions if action.get("action") == "OPEN")
    hold_count = sum(1 for action in actions if action.get("action") == "HOLD")
    return {
        "actions": actions,
        "summary": {
            "close_count": close_count,
            "open_count": open_count,
            "hold_count": hold_count,
            "position_count": len(held_symbols),
            "selected_symbol": selected_symbol or None,
            "signal_quality_allowed": signal_quality_allowed,
            "trailing_highs": trailing_highs,
        },
    }


def _protective_exit_reason(
    *,
    position: object,
    atr: float | None,
    stop_loss_atr_mult: float,
    take_profit_atr_mult: float,
    trailing_atr_mult: float,
    trailing_high: float | None,
    breakeven_trigger_atr_mult: float = 0.0,
    breakeven_buffer_atr_mult: float = 0.0,
) -> str | None:
    """Return a protective-exit reason if a stop level is breached, else ``None``.

    Priority: stop_loss > take_profit > (breakeven_stop | trailing_stop, whichever
    level is higher). A multiplier of ``0`` disables that exit type. Requires a
    positive ATR, entry price and current price; otherwise no protective action is
    taken.

    The breakeven stop arms once the high-water mark (max of entry, current price
    and the persisted trailing high) reaches ``entry + breakeven_trigger_atr_mult *
    atr``; once armed it stays armed regardless of subsequent price pullbacks
    because arming depends on the high-water mark, not the current price. This
    makes the ratchet monotonic across runs when the high-water mark is persisted.
    """

    entry = _float_or_none(_get(position, "avg_entry_price", None))
    price = _float_or_none(_get(position, "current_price", None))
    if atr is None or atr <= 0 or entry is None or entry <= 0 or price is None or price <= 0:
        return None
    if stop_loss_atr_mult > 0 and price <= entry - stop_loss_atr_mult * atr:
        return "stop_loss"
    if take_profit_atr_mult > 0 and price >= entry + take_profit_atr_mult * atr:
        return "take_profit"

    high = max(entry, price, trailing_high or 0.0)
    candidates: dict[str, float] = {}
    if breakeven_trigger_atr_mult > 0 and high >= entry + breakeven_trigger_atr_mult * atr:
        candidates["breakeven_stop"] = entry + breakeven_buffer_atr_mult * atr
    if trailing_atr_mult > 0:
        candidates["trailing_stop"] = high - trailing_atr_mult * atr

    if not candidates:
        return None
    max_level = max(candidates.values())
    if price > max_level:
        return None
    if "breakeven_stop" in candidates and candidates["breakeven_stop"] == max_level:
        return "breakeven_stop"
    return "trailing_stop"


def _protective_levels(
    *,
    position: object,
    atr: float | None,
    stop_loss_atr_mult: float,
    take_profit_atr_mult: float,
    trailing_atr_mult: float,
    trailing_high: float | None,
    breakeven_trigger_atr_mult: float = 0.0,
    breakeven_buffer_atr_mult: float = 0.0,
) -> dict[str, float | None] | None:
    entry = _float_or_none(_get(position, "avg_entry_price", None))
    price = _float_or_none(_get(position, "current_price", None))
    if atr is None or atr <= 0 or entry is None or entry <= 0 or price is None or price <= 0:
        return None
    high = max(entry, price, trailing_high or 0.0)
    stop_loss_price = entry - stop_loss_atr_mult * atr if stop_loss_atr_mult > 0 else None
    trailing_stop_price = high - trailing_atr_mult * atr if trailing_atr_mult > 0 else None
    breakeven_armed = breakeven_trigger_atr_mult > 0 and high >= entry + breakeven_trigger_atr_mult * atr
    breakeven_stop_price = entry + breakeven_buffer_atr_mult * atr if breakeven_armed else None
    effective_candidates = [
        level for level in (stop_loss_price, breakeven_stop_price, trailing_stop_price) if level is not None
    ]
    effective_stop_price = max(effective_candidates) if effective_candidates else None
    return {
        "avg_entry_price": entry,
        "current_price": price,
        "atr": atr,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": entry + take_profit_atr_mult * atr if take_profit_atr_mult > 0 else None,
        "trailing_high": high,
        "trailing_stop_price": trailing_stop_price,
        "breakeven_stop_price": breakeven_stop_price,
        "effective_stop_price": effective_stop_price,
    }


def _trailing_highs(
    positions: Sequence[object],
    trailing_high_by_symbol: Mapping[str, float] | None,
) -> dict[str, float]:
    """High-water mark per held symbol, carried forward across runs."""

    prior = trailing_high_by_symbol or {}
    highs: dict[str, float] = {}
    for position in positions:
        symbol = _symbol(position)
        if not symbol:
            continue
        candidates = [
            value
            for value in (
                _float_or_none(_get(position, "current_price", None)),
                _float_or_none(_get(position, "avg_entry_price", None)),
                _float_or_none(prior.get(symbol)),
            )
            if value is not None and value > 0
        ]
        if candidates:
            highs[symbol] = max(candidates)
    return highs


def close_actions(position_plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [
        action
        for action in _action_list(position_plan)
        if str(action.get("action") or "").upper() == "CLOSE"
    ]


def open_actions(position_plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [
        action
        for action in _action_list(position_plan)
        if str(action.get("action") or "").upper() == "OPEN"
    ]


def hold_actions(position_plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [
        action
        for action in _action_list(position_plan)
        if str(action.get("action") or "").upper() == "HOLD"
    ]


def dynamic_client_order_id(
    *,
    prefix: str,
    symbol: str,
    as_of_date: str,
    intent_key: str | None = None,
) -> str:
    compact_date = "".join(character for character in as_of_date if character.isalnum())
    base = f"{prefix}-{symbol.lower()}-{compact_date[:16]}"
    if intent_key is None:
        return base
    digest = hashlib.sha256(intent_key.encode("utf-8")).hexdigest()[:10]
    return f"{base[:37]}-{digest}"


def _close_action(
    *,
    symbol: str,
    quantity: float | None,
    reason: str,
    signal: object | None,
    protective_levels: Mapping[str, object] | None = None,
) -> dict[str, object]:
    action: dict[str, object] = {
        "action": "CLOSE",
        "symbol": symbol,
        "side": "buy" if quantity is not None and quantity < 0 else "sell",
        "quantity": quantity,
        "reason": reason,
        "signal": _signal_summary(signal),
    }
    if protective_levels is not None:
        action["protective_levels"] = dict(protective_levels)
    return action


def _signal_summary(signal: object | None) -> dict[str, object] | None:
    if signal is None:
        return None
    return {
        "timestamp": _get(signal, "timestamp", None),
        "symbol": _symbol(signal) or None,
        "probability": _float_or_none(_get(signal, "probability", None)),
        "threshold": _float_or_none(_get(signal, "threshold", None)),
        "action": _action(signal) or None,
    }


def _action_list(position_plan: Mapping[str, object]) -> list[Mapping[str, object]]:
    actions = position_plan.get("actions")
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, Mapping)]


def _symbol(value: object | None) -> str:
    symbol = _get(value, "symbol", "")
    return str(symbol or "").upper()


def _action(value: object | None) -> str:
    action = _get(value, "action", "")
    return str(action or "").lower()


def _get(value: object | None, key: str, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, (Mapping, list)):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
