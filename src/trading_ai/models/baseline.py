"""Pure-Python logistic baseline for temporal model evaluation."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SupervisedExample:
    timestamp: str
    symbol: str
    features: tuple[float, ...]
    target: int


@dataclass(frozen=True)
class TemporalSplit:
    train: tuple[SupervisedExample, ...]
    test: tuple[SupervisedExample, ...]


@dataclass(frozen=True)
class LogisticBaselineConfig:
    feature_names: tuple[str, ...] = ("momentum_20", "realized_volatility_20", "relative_volume_20")
    learning_rate: float = 0.2
    epochs: int = 200
    l2: float = 0.001
    test_fraction: float = 0.25


@dataclass(frozen=True)
class LogisticBaselineModel:
    feature_names: tuple[str, ...]
    intercept: float
    coefficients: tuple[float, ...]

    def predict_probability(self, features: tuple[float, ...]) -> float:
        score = self.intercept + sum(weight * value for weight, value in zip(self.coefficients, features, strict=False))
        return _sigmoid(score)

    def predict(self, features: tuple[float, ...], *, threshold: float = 0.5) -> int:
        return int(self.predict_probability(features) >= threshold)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LogisticBaselineModel:
        validate_logistic_model_payload(payload)
        return cls(
            feature_names=tuple(str(name) for name in payload["feature_names"]),
            intercept=float(payload["intercept"]),
            coefficients=tuple(float(value) for value in payload["coefficients"]),
        )


def validate_logistic_model_payload(payload: Mapping[str, object]) -> None:
    feature_names = payload.get("feature_names")
    coefficients = payload.get("coefficients")
    if not isinstance(feature_names, (list, tuple)) or not feature_names:
        raise ValueError("model feature_names must be a non-empty list")
    if not all(str(name).strip() for name in feature_names):
        raise ValueError("model feature_names must be non-empty strings")
    if not isinstance(coefficients, (list, tuple)):
        raise ValueError("model coefficients must be a list")
    if len(coefficients) != len(feature_names):
        raise ValueError("model coefficients length must match feature_names")
    try:
        intercept = float(payload.get("intercept"))
    except (TypeError, ValueError) as exc:
        raise ValueError("model intercept must be numeric") from exc
    if not math.isfinite(intercept):
        raise ValueError("model intercept must be finite")
    for value in coefficients:
        try:
            coefficient = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("model coefficients must be numeric") from exc
        if not math.isfinite(coefficient):
            raise ValueError("model coefficients must be finite")


def build_supervised_examples(
    records: Iterable[Mapping[str, object]],
    *,
    feature_names: tuple[str, ...],
) -> tuple[SupervisedExample, ...]:
    by_symbol: dict[str, list[Mapping[str, object]]] = {}
    for row in records:
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)

    examples: list[SupervisedExample] = []
    for symbol, rows in by_symbol.items():
        sorted_rows = sorted(rows, key=lambda row: str(row["timestamp"]))
        for index, row in enumerate(sorted_rows[:-1]):
            next_row = sorted_rows[index + 1]
            features = _extract_features(row, feature_names)
            if features is None:
                continue
            target = int(float(next_row["close"]) > float(row["close"]))
            examples.append(
                SupervisedExample(
                    timestamp=str(row["timestamp"]),
                    symbol=symbol,
                    features=features,
                    target=target,
                )
            )
    return tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))


def temporal_train_test_split(
    examples: Iterable[SupervisedExample],
    *,
    test_fraction: float,
) -> TemporalSplit:
    rows = tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))
    if len(rows) < 2:
        raise ValueError("at least two examples are required")
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be between 0 and 1")
    test_size = max(1, int(math.ceil(len(rows) * test_fraction)))
    train_size = len(rows) - test_size
    if train_size < 1:
        raise ValueError("temporal split leaves no training examples")
    return TemporalSplit(train=rows[:train_size], test=rows[train_size:])


def train_logistic_baseline(
    examples: Iterable[SupervisedExample],
    config: LogisticBaselineConfig,
) -> LogisticBaselineModel:
    rows = tuple(examples)
    if not rows:
        raise ValueError("at least one training example is required")
    weights = [0.0 for _ in config.feature_names]
    intercept = 0.0
    for _ in range(config.epochs):
        for row in rows:
            probability = _sigmoid(
                intercept + sum(weight * value for weight, value in zip(weights, row.features, strict=False))
            )
            error = probability - row.target
            intercept -= config.learning_rate * error
            for index, value in enumerate(row.features):
                gradient = error * value + config.l2 * weights[index]
                weights[index] -= config.learning_rate * gradient
    return LogisticBaselineModel(
        feature_names=config.feature_names,
        intercept=intercept,
        coefficients=tuple(weights),
    )


def evaluate_classifier(
    model: LogisticBaselineModel,
    examples: Iterable[SupervisedExample],
) -> dict[str, float]:
    rows = tuple(examples)
    if not rows:
        return {"sample_count": 0.0, "accuracy": 0.0, "log_loss": 0.0, "positive_rate": 0.0}
    correct = 0
    log_loss = 0.0
    positives = 0
    for row in rows:
        probability = min(max(model.predict_probability(row.features), 1e-12), 1.0 - 1e-12)
        prediction = int(probability >= 0.5)
        correct += int(prediction == row.target)
        positives += row.target
        log_loss += -(row.target * math.log(probability) + (1 - row.target) * math.log(1 - probability))
    return {
        "sample_count": float(len(rows)),
        "accuracy": correct / len(rows),
        "log_loss": log_loss / len(rows),
        "positive_rate": positives / len(rows),
    }


def walk_forward_evaluate(
    examples: Iterable[SupervisedExample],
    config: LogisticBaselineConfig,
    *,
    min_train_size: int,
    test_size: int,
) -> dict[str, object]:
    rows = tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))
    windows: list[dict[str, object]] = []
    cursor = min_train_size
    while cursor < len(rows):
        test_end = min(cursor + test_size, len(rows))
        train_rows = rows[:cursor]
        test_rows = rows[cursor:test_end]
        if not test_rows:
            break
        model = train_logistic_baseline(train_rows, config)
        windows.append(
            {
                "train_end": train_rows[-1].timestamp,
                "test_start": test_rows[0].timestamp,
                "test_end": test_rows[-1].timestamp,
                "metrics": evaluate_classifier(model, test_rows),
            }
        )
        cursor = test_end
    if not windows:
        return {"window_count": 0.0, "mean_accuracy": 0.0, "windows": []}
    return {
        "window_count": float(len(windows)),
        "mean_accuracy": sum(float(window["metrics"]["accuracy"]) for window in windows) / len(windows),
        "windows": windows,
    }


def save_model(model: LogisticBaselineModel, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(model.to_dict(), handle, indent=2, sort_keys=True)


def load_model(path: str) -> LogisticBaselineModel:
    with open(path, encoding="utf-8") as handle:
        return LogisticBaselineModel.from_dict(json.load(handle))


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


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)
