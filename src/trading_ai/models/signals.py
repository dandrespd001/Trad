"""Convert local model predictions into paper-trading signals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from trading_ai.models.baseline import LogisticBaselineModel


@dataclass(frozen=True)
class ModelSignal:
    timestamp: str
    symbol: str
    probability: float
    threshold: float
    action: str


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
) -> tuple[ModelSignal, ...]:
    latest_rows = latest_valid_feature_rows(records, feature_names=model.feature_names, allowlist=allowlist)
    signals: list[ModelSignal] = []
    for symbol, row in sorted(latest_rows.items()):
        features = _extract_features(row, model.feature_names)
        if features is None:
            continue
        probability = model.predict_probability(features)
        signals.append(
            ModelSignal(
                timestamp=str(row["timestamp"]),
                symbol=symbol,
                probability=probability,
                threshold=threshold,
                action="buy" if probability >= threshold else "hold",
            )
        )
    return tuple(signals)


def _extract_features(row: Mapping[str, object], feature_names: tuple[str, ...]) -> tuple[float, ...] | None:
    values: list[float] = []
    for name in feature_names:
        value = row.get(name)
        if value in {None, ""}:
            return None
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return None
    return tuple(values)
