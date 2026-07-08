"""Pure-Python logistic baseline for temporal model evaluation."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast


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
    # Optional train-only standardization stats. When both are present, the
    # model expects already-standardized inputs at inference time; ``train_logistic_baseline``
    # attaches them when ``standardize=True`` and ``predict_probability`` applies them.
    # NOTE: kept as ``tuple[float, ...] | None`` to keep the default (no standardization)
    # byte-identical with the pre-H1 artifact schema (see to_dict()).
    feature_means: tuple[float, ...] | None = None
    feature_stds: tuple[float, ...] | None = None

    def predict_probability(self, features: tuple[float, ...]) -> float:
        transformed = _apply_standardization(features, self.feature_means, self.feature_stds)
        score = self.intercept + sum(
            weight * value for weight, value in zip(self.coefficients, transformed, strict=False)
        )
        return _sigmoid(score)

    def predict(self, features: tuple[float, ...], *, threshold: float = 0.5) -> int:
        return int(self.predict_probability(features) >= threshold)

    def to_dict(self) -> dict[str, object]:
        # NOTE: legacy models (no standardization) must serialize byte-identically
        # to the pre-H1 schema. We therefore only emit the stats keys when both
        # are present, keeping default-path artifacts unchanged.
        payload: dict[str, object] = {
            "feature_names": list(self.feature_names),
            "intercept": self.intercept,
            "coefficients": list(self.coefficients),
        }
        if self.feature_means is not None and self.feature_stds is not None:
            payload["feature_means"] = list(self.feature_means)
            payload["feature_stds"] = list(self.feature_stds)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LogisticBaselineModel:
        validate_logistic_model_payload(payload)
        feature_names = payload["feature_names"]
        coefficients = payload["coefficients"]
        if not isinstance(feature_names, (list, tuple)) or not isinstance(coefficients, (list, tuple)):
            raise ValueError("model payload failed validation")
        means, stds = _optional_stats_pair(payload, expected_length=len(feature_names))
        return cls(
            feature_names=tuple(str(name) for name in feature_names),
            intercept=_required_float(payload["intercept"], "model intercept"),
            coefficients=tuple(_required_float(value, "model coefficient") for value in coefficients),
            feature_means=means,
            feature_stds=stds,
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
        intercept = _required_float(payload.get("intercept"), "model intercept")
    except (TypeError, ValueError) as exc:
        raise ValueError("model intercept must be numeric") from exc
    if not math.isfinite(intercept):
        raise ValueError("model intercept must be finite")
    for value in coefficients:
        try:
            coefficient = _required_float(value, "model coefficient")
        except (TypeError, ValueError) as exc:
            raise ValueError("model coefficients must be numeric") from exc
        if not math.isfinite(coefficient):
            raise ValueError("model coefficients must be finite")
    # Optional standardization stats: validated only when present. When both
    # are present, they must be the right length, finite, and ``feature_stds``
    # must not contain zero (zero would divide by zero at inference time).
    has_means = "feature_means" in payload
    has_stds = "feature_stds" in payload
    if has_means != has_stds:
        raise ValueError("model feature_means and feature_stds must be provided together")
    if has_means and has_stds:
        means = payload["feature_means"]
        stds = payload["feature_stds"]
        if not isinstance(means, (list, tuple)) or not isinstance(stds, (list, tuple)):
            raise ValueError("model feature_means and feature_stds must be lists")
        if len(means) != len(feature_names) or len(stds) != len(feature_names):
            raise ValueError("model feature_means/feature_stds length must match feature_names")
        for value in means:
            try:
                mean = _required_float(value, "model feature_means")
            except (TypeError, ValueError) as exc:
                raise ValueError("model feature_means must be numeric") from exc
            if not math.isfinite(mean):
                raise ValueError("model feature_means must be finite")
        for index, value in enumerate(stds):
            try:
                std = _required_float(value, "model feature_stds")
            except (TypeError, ValueError) as exc:
                raise ValueError("model feature_stds must be numeric") from exc
            if not math.isfinite(std):
                raise ValueError("model feature_stds must be finite")
            if std == 0.0:
                raise ValueError(
                    f"model feature_stds[{index}] must be non-zero (zero division at inference)"
                )


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
            target = int(_required_float(next_row["close"], "next close") > _required_float(row["close"], "close"))
            examples.append(
                SupervisedExample(
                    timestamp=str(row["timestamp"]),
                    symbol=symbol,
                    features=features,
                    target=target,
                )
            )
    return tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))


def build_triple_barrier_examples(
    records: Iterable[Mapping[str, object]],
    *,
    feature_names: tuple[str, ...],
    horizon: int,
    atr_mult: float,
    vol_column: str = "atr_14",
) -> tuple[SupervisedExample, ...]:
    """Triple-barrier vol-scaled binary labeling (López de Prado).

    For each row ``i`` the entry price is ``close_i`` and the unit width is
    ``row_i[vol_column]``. The upper and lower barriers are
    ``entry ± atr_mult * unit``. Walking forward in ``[i+1, i+horizon]`` we
    inspect ``close_j`` only (not high/low, to avoid intrabar optimism and
    double-touch ambiguity on a single bar):

    - First ``j`` with ``close_j >= upper`` → label ``1``, stop.
    - First ``j`` with ``close_j <= lower`` → label ``0``, stop.
    - Otherwise (time-out): ``label = int(close_{i+horizon} > entry)``.

    Rows with missing/non-finite/non-positive ``vol_column`` are skipped (no
    barrier can be scaled); rows where ``i + horizon`` runs past the end of
    the per-symbol series are skipped (no full lookahead). Output is sorted
    by ``(timestamp, symbol)`` exactly like ``build_supervised_examples``.
    """
    # NOTE: argument validation is explicit (not buried in an inner loop) so
    # misconfigured CLI flags raise before we read a single row.
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if atr_mult <= 0:
        raise ValueError("atr_mult must be > 0")

    by_symbol: dict[str, list[Mapping[str, object]]] = {}
    for row in records:
        by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)

    examples: list[SupervisedExample] = []
    for symbol, rows in by_symbol.items():
        sorted_rows = sorted(rows, key=lambda row: str(row["timestamp"]))
        # The tail of the series has no full lookahead window of size
        # ``horizon``; we cannot label without it, so we skip those rows
        # outright. ``index + horizon`` must be a valid row index.
        last_labelable = len(sorted_rows) - horizon - 1
        for index, row in enumerate(sorted_rows):
            if index > last_labelable:
                break

            features = _extract_features(row, feature_names)
            if features is None:
                continue

            entry = _required_float(row["close"], "close")

            raw_unit = row.get(vol_column)
            if raw_unit in (None, ""):
                continue
            try:
                unit = float(raw_unit)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(unit) or unit <= 0:
                # Without a finite positive unit we cannot scale the barriers;
                # do NOT invent a fallback (silent default would distort OOS).
                continue

            upper = entry + atr_mult * unit
            lower = entry - atr_mult * unit

            label: int | None = None
            for j in range(index + 1, index + horizon + 1):
                future_close = _required_float(sorted_rows[j]["close"], "future close")
                if future_close >= upper:
                    label = 1
                    break
                if future_close <= lower:
                    label = 0
                    break

            if label is None:
                # Time-out: sign of the close at the end of the window.
                final_close = _required_float(
                    sorted_rows[index + horizon]["close"], "future close"
                )
                label = int(final_close > entry)

            examples.append(
                SupervisedExample(
                    timestamp=str(row["timestamp"]),
                    symbol=symbol,
                    features=features,
                    target=label,
                )
            )
    return tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))


def temporal_train_test_split(
    examples: Iterable[SupervisedExample],
    *,
    test_fraction: float,
    embargo: int = 0,
) -> TemporalSplit:
    """Chronological train/test split with an optional embargo.

    ``embargo`` purges that many examples at the train/test boundary (dropped
    from the end of train). With a one-bar-ahead label the last training
    example's target falls on the first test bar, so an embargo of at least 1
    removes that contiguity leakage between the sets.
    """

    if embargo < 0:
        raise ValueError("embargo must be non-negative")
    rows = tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))
    if len(rows) < 2:
        raise ValueError("at least two examples are required")
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be between 0 and 1")
    test_size = max(1, int(math.ceil(len(rows) * test_fraction)))
    train_size = len(rows) - test_size
    train_end = train_size - embargo
    if train_end < 1:
        raise ValueError("temporal split leaves no training examples after embargo")
    return TemporalSplit(train=rows[:train_end], test=rows[train_size:])


def train_logistic_baseline(
    examples: Iterable[SupervisedExample],
    config: LogisticBaselineConfig,
    *,
    standardize: bool = False,
) -> LogisticBaselineModel:
    """Train the logistic baseline via plain SGD.

    When ``standardize=True`` the function computes per-feature mean/std on the
    provided training examples only (no leakage), transforms each feature vector
    in place before the gradient update, and attaches the resulting stats to the
    serialized model so inference applies the same transformation. When the flag
    is omitted the trainer behaves byte-identically to the pre-H1 implementation.
    """
    rows = tuple(examples)
    if not rows:
        raise ValueError("at least one training example is required")
    feature_count = len(config.feature_names)
    means: tuple[float, ...] | None = None
    stds: tuple[float, ...] | None = None
    if standardize:
        means, stds = compute_feature_stats(rows, expected_length=feature_count)
    weights = [0.0 for _ in config.feature_names]
    intercept = 0.0
    for _ in range(config.epochs):
        for row in rows:
            transformed = _apply_standardization(row.features, means, stds)
            probability = _sigmoid(
                intercept + sum(weight * value for weight, value in zip(weights, transformed, strict=False))
            )
            error = probability - row.target
            intercept -= config.learning_rate * error
            for index, value in enumerate(transformed):
                gradient = error * value + config.l2 * weights[index]
                weights[index] -= config.learning_rate * gradient
    return LogisticBaselineModel(
        feature_names=config.feature_names,
        intercept=intercept,
        coefficients=tuple(weights),
        feature_means=means,
        feature_stds=stds,
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
    embargo: int = 0,
    standardize: bool = False,
) -> dict[str, object]:
    if embargo < 0:
        raise ValueError("embargo must be non-negative")
    rows = tuple(sorted(examples, key=lambda example: (example.timestamp, example.symbol)))
    windows: list[dict[str, object]] = []
    accuracies: list[float] = []
    cursor = min_train_size
    while cursor < len(rows):
        test_end = min(cursor + test_size, len(rows))
        train_rows = rows[: max(0, cursor - embargo)]
        test_rows = rows[cursor:test_end]
        if not test_rows:
            break
        if not train_rows:
            cursor = test_end
            continue
        # Per-window standardization: each window computes its own stats from
        # its own train slice, so no test information leaks into the scaler.
        model = train_logistic_baseline(train_rows, config, standardize=standardize)
        metrics = evaluate_classifier(model, test_rows)
        accuracies.append(metrics["accuracy"])
        windows.append(
            {
                "train_end": train_rows[-1].timestamp,
                "test_start": test_rows[0].timestamp,
                "test_end": test_rows[-1].timestamp,
                "metrics": metrics,
            }
        )
        cursor = test_end
    if not windows:
        return {"window_count": 0.0, "mean_accuracy": 0.0, "windows": []}
    return {
        "window_count": float(len(windows)),
        "mean_accuracy": sum(accuracies) / len(accuracies),
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
            values.append(_required_float(value, name))
        except (TypeError, ValueError):
            return None
    return tuple(values)


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _required_float(value: object, label: str) -> float:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc


# ---------------------------------------------------------------------------
# Train-only standardization helpers (H1)
# ---------------------------------------------------------------------------

# Threshold below which a feature's standard deviation is treated as zero
# (constant feature): we fall back to ``std=1.0`` so the feature is centered
# but never divides by zero. Pure 1e-12 rather than math.ulp to avoid
# surprises on different hardware.
_STD_FLOOR = 1e-12


def compute_feature_stats(
    examples: Iterable[SupervisedExample],
    *,
    expected_length: int,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Compute per-feature mean/std over the provided examples only.

    The function never raises on degenerate features: when ``std`` collapses to
    zero or becomes non-finite, it substitutes ``1.0`` (no scaling, only
    centering); when the mean itself is non-finite, ``0.0``. This guarantees
    ``(x - mean) / std`` is always finite downstream.
    """
    rows = tuple(examples)
    if not rows:
        raise ValueError("at least one training example is required to compute stats")
    sums = [0.0 for _ in range(expected_length)]
    counts = 0
    for row in rows:
        counts += 1
        for index, value in enumerate(row.features):
            sums[index] += float(value)
    means: list[float] = []
    for total in sums:
        raw = total / counts
        means.append(raw if math.isfinite(raw) else 0.0)
    centered_sums = [0.0 for _ in range(expected_length)]
    for row in rows:
        for index, value in enumerate(row.features):
            diff = float(value) - means[index]
            centered_sums[index] += diff * diff
    stds: list[float] = []
    for index, total in enumerate(centered_sums):
        # Population std is fine here: these stats are only used to (a) scale
        # the gradient signal during training and (b) center/scale inputs at
        # inference. The downstream scorer is invariant to that constant.
        raw = math.sqrt(total / counts)
        if not math.isfinite(raw) or raw <= _STD_FLOOR:
            stds.append(1.0)
        else:
            stds.append(raw)
    return tuple(means), tuple(stds)


def _apply_standardization(
    features: tuple[float, ...],
    means: tuple[float, ...] | None,
    stds: tuple[float, ...] | None,
) -> tuple[float, ...]:
    """Return ``(x - mean) / std`` for each feature. Identity when stats absent.

    Always returns a finite tuple: if any input is non-finite, it is replaced
    with the centered-and-scaled mean (``0.0``) before division, so the model
    never sees NaN/inf even on pathological inputs.
    """
    if means is None or stds is None:
        return features
    scaled: list[float] = []
    for index, value in enumerate(features):
        if not math.isfinite(value):
            scaled.append(0.0)
            continue
        mean = means[index]
        std = stds[index]
        scaled.append((value - mean) / std if std != 0.0 else (value - mean))
    return tuple(scaled)


def _optional_stats_pair(
    payload: Mapping[str, object],
    *,
    expected_length: int,
) -> tuple[tuple[float, ...] | None, tuple[float, ...] | None]:
    """Extract optional ``feature_means``/``feature_stds`` from a model payload.

    Validation of structure/values is performed by ``validate_logistic_model_payload``;
    this helper only re-coerces the already-validated lists into tuples. When
    neither key is present, returns ``(None, None)`` (legacy model).
    """
    if "feature_means" not in payload and "feature_stds" not in payload:
        return None, None
    raw_means = payload["feature_means"]
    raw_stds = payload["feature_stds"]
    if not isinstance(raw_means, (list, tuple)) or not isinstance(raw_stds, (list, tuple)):
        raise ValueError("model feature_means and feature_stds must be lists")
    if len(raw_means) != expected_length or len(raw_stds) != expected_length:
        raise ValueError("model feature_means/feature_stds length must match feature_names")
    return (
        tuple(float(value) for value in raw_means),
        tuple(float(value) for value in raw_stds),
    )


# ---------------------------------------------------------------------------
# LightGBM and XGBoost wrappers (require the 'ml' optional extras)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LightGBMBaselineConfig:
    feature_names: tuple[str, ...] = ("momentum_20", "realized_volatility_20", "relative_volume_20")
    n_estimators: int = 100
    learning_rate: float = 0.05
    num_leaves: int = 31
    test_fraction: float = 0.25


@dataclass
class LightGBMBaselineModel:
    """LightGBM classifier with the same predict interface as LogisticBaselineModel."""

    feature_names: tuple[str, ...]
    _clf: Any = None

    def predict_probability(self, features: tuple[float, ...]) -> float:
        import numpy as np  # noqa: PLC0415

        proba = self._clf.predict_proba(np.array([features]))[0]
        return float(proba[1])

    def predict(self, features: tuple[float, ...], *, threshold: float = 0.5) -> int:
        return int(self.predict_probability(features) >= threshold)


@dataclass
class XGBoostBaselineModel:
    """XGBoost classifier with the same predict interface as LogisticBaselineModel."""

    feature_names: tuple[str, ...]
    _clf: Any = None

    def predict_probability(self, features: tuple[float, ...]) -> float:
        import numpy as np  # noqa: PLC0415

        proba = self._clf.predict_proba(np.array([features]))[0]
        return float(proba[1])

    def predict(self, features: tuple[float, ...], *, threshold: float = 0.5) -> int:
        return int(self.predict_probability(features) >= threshold)


def train_lightgbm_baseline(
    examples: Iterable[SupervisedExample],
    config: LightGBMBaselineConfig,
) -> LightGBMBaselineModel:
    """Train a LightGBM classifier on supervised examples. Requires 'ml' extras."""
    try:
        import lightgbm as lgb  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("train_lightgbm_baseline requires lightgbm: pip install -e '.[ml]'") from exc

    rows = tuple(examples)
    if not rows:
        raise ValueError("at least one training example is required")
    X = np.array([list(ex.features) for ex in rows], dtype=float)
    y = np.array([ex.target for ex in rows], dtype=int)
    clf = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        num_leaves=config.num_leaves,
        verbose=-1,
        random_state=42,
    )
    clf.fit(X, y)
    model = LightGBMBaselineModel(feature_names=config.feature_names)
    model._clf = clf
    return model


def train_xgboost_baseline(
    examples: Iterable[SupervisedExample],
    config: "XGBoostBaselineConfig",
) -> XGBoostBaselineModel:
    """Train an XGBoost classifier on supervised examples. Requires 'ml' extras."""
    try:
        import numpy as np  # noqa: PLC0415
        import xgboost as xgb  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("train_xgboost_baseline requires xgboost: pip install -e '.[ml]'") from exc

    rows = tuple(examples)
    if not rows:
        raise ValueError("at least one training example is required")
    X = np.array([list(ex.features) for ex in rows], dtype=float)
    y = np.array([ex.target for ex in rows], dtype=int)
    clf = xgb.XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        max_depth=config.max_depth,
        verbosity=0,
        random_state=42,
    )
    clf.fit(X, y)
    model = XGBoostBaselineModel(feature_names=config.feature_names)
    model._clf = clf
    return model


@dataclass(frozen=True)
class XGBoostBaselineConfig:
    feature_names: tuple[str, ...] = ("momentum_20", "realized_volatility_20", "relative_volume_20")
    n_estimators: int = 100
    learning_rate: float = 0.05
    max_depth: int = 4
    test_fraction: float = 0.25
