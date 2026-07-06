"""Convert local model predictions into paper-trading signals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from trading_ai.models.baseline import LogisticBaselineModel


@dataclass(frozen=True)
class ModelSignal:
    timestamp: str
    symbol: str
    probability: float
    threshold: float
    action: str
    atr: float | None = None
    realized_volatility: float | None = None
    reference_price: float | None = None
    reason_codes: tuple[str, ...] = ()
    model_id: str | None = None
    policy_action: str | None = None
    open_score: float | None = None
    close_score: float | None = None
    risk_inputs: Mapping[str, object] | None = None
    safety: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SignalPolicyConfig:
    model_id: str = "latest_model"
    close_threshold: float | None = None
    max_positions: int = 0
    max_gross_exposure: float = 0.0
    stop_loss_atr_mult: float = 0.0
    take_profit_atr_mult: float = 0.0
    trailing_atr_mult: float = 0.0
    current_positions: Mapping[str, Mapping[str, object]] | None = None


def latest_valid_feature_rows(
    records: Iterable[Mapping[str, object]],
    *,
    feature_names: tuple[str, ...],
    allowlist: tuple[str, ...],
) -> dict[str, Mapping[str, object]]:
    allowed = {symbol.upper() for symbol in allowlist}
    latest: dict[str, Mapping[str, object]] = {}
    for row in sorted(records, key=lambda item: (str(item["timestamp"]), str(item["symbol"]).upper())):
        symbol = str(row["symbol"]).upper()
        if symbol not in allowed:
            continue
        if _extract_features(row, feature_names) is None:
            continue
        latest[symbol] = row
    return latest


def generate_model_signals(
    records: Iterable[Mapping[str, object]],
    *,
    model: LogisticBaselineModel,
    allowlist: tuple[str, ...],
    threshold: float = 0.5,
    policy: SignalPolicyConfig | None = None,
) -> tuple[ModelSignal, ...]:
    latest_rows = latest_valid_feature_rows(records, feature_names=model.feature_names, allowlist=allowlist)
    signals: list[ModelSignal] = []
    for symbol, row in sorted(latest_rows.items()):
        features = _extract_features(row, model.feature_names)
        if features is None:
            continue
        probability = model.predict_probability(features)
        atr = _optional_feature_float(row.get("atr_14"))
        reference_price = _optional_feature_float(row.get("close"))
        realized_volatility = _optional_feature_float(row.get("realized_volatility_20"))
        if policy is not None:
            policy_action, reason_codes = _policy_action(
                symbol=symbol,
                probability=probability,
                threshold=threshold,
                row=row,
                policy=policy,
                atr=atr,
                reference_price=reference_price,
            )
            close_threshold = policy.close_threshold if policy.close_threshold is not None else threshold
            action = "buy" if policy_action == "open_long" else "hold"
            signals.append(
                ModelSignal(
                    timestamp=str(row["timestamp"]),
                    symbol=symbol,
                    probability=probability,
                    threshold=threshold,
                    action=action,
                    atr=atr,
                    realized_volatility=realized_volatility,
                    reference_price=reference_price,
                    reason_codes=tuple(reason_codes),
                    model_id=policy.model_id,
                    policy_action=policy_action,
                    open_score=probability,
                    close_score=1.0 - probability if policy_action == "close_long" else probability,
                    risk_inputs={
                        "max_positions": policy.max_positions,
                        "max_gross_exposure": policy.max_gross_exposure,
                        "stop_loss_atr_mult": policy.stop_loss_atr_mult,
                        "take_profit_atr_mult": policy.take_profit_atr_mult,
                        "trailing_atr_mult": policy.trailing_atr_mult,
                        "open_threshold": threshold,
                        "close_threshold": close_threshold,
                    },
                    safety={"paper_only": True, "orders_submitted": False, "llm_authority": "none"},
                )
            )
            continue
        signals.append(
            ModelSignal(
                timestamp=str(row["timestamp"]),
                symbol=symbol,
                probability=probability,
                threshold=threshold,
                action="buy" if probability >= threshold else "hold",
                atr=atr,
                realized_volatility=realized_volatility,
                reference_price=reference_price,
            )
        )
    return tuple(signals)


def _policy_action(
    *,
    symbol: str,
    probability: float,
    threshold: float,
    row: Mapping[str, object],
    policy: SignalPolicyConfig,
    atr: float | None,
    reference_price: float | None,
) -> tuple[str, list[str]]:
    current_positions = policy.current_positions or {}
    position = current_positions.get(symbol.upper()) or current_positions.get(symbol)
    reasons: list[str] = []
    close_threshold = policy.close_threshold if policy.close_threshold is not None else threshold
    if position:
        if probability < close_threshold:
            reasons.append("probability_below_close_threshold")
        reasons.extend(_atr_exit_reasons(position, atr=atr, reference_price=reference_price, policy=policy))
        if reasons:
            return "close_long", reasons
        return "hold", ["position_held"]
    if probability >= threshold:
        reasons.append("probability_above_open_threshold")
        if policy.max_positions > 0:
            open_count = len(current_positions)
            if open_count >= policy.max_positions:
                return "hold", ["max_positions_reached"]
        return "open_long", reasons
    return "hold", ["probability_below_open_threshold"]


def _atr_exit_reasons(
    position: Mapping[str, object],
    *,
    atr: float | None,
    reference_price: float | None,
    policy: SignalPolicyConfig,
) -> list[str]:
    if atr is None or atr <= 0 or reference_price is None:
        return []
    entry_price = _optional_feature_float(position.get("entry_price"))
    highest_price = _optional_feature_float(position.get("highest_price")) or entry_price
    reasons: list[str] = []
    if entry_price is None:
        return reasons
    if policy.stop_loss_atr_mult > 0 and reference_price <= entry_price - policy.stop_loss_atr_mult * atr:
        reasons.append("stop_loss_atr")
    if policy.take_profit_atr_mult > 0 and reference_price >= entry_price + policy.take_profit_atr_mult * atr:
        reasons.append("take_profit_atr")
    if (
        policy.trailing_atr_mult > 0
        and highest_price is not None
        and reference_price <= highest_price - policy.trailing_atr_mult * atr
    ):
        reasons.append("trailing_atr")
    return reasons


def _optional_feature_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _extract_features(row: Mapping[str, object], feature_names: tuple[str, ...]) -> tuple[float, ...] | None:
    values: list[float] = []
    for name in feature_names:
        value = row.get(name)
        if value in {None, ""}:
            return None
        try:
            values.append(_required_float(value))
        except (TypeError, ValueError):
            return None
    return tuple(values)


def _required_float(value: object) -> float:
    return float(cast(Any, value))
