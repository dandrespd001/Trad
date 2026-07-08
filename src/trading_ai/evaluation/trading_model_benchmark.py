"""Risk-adjusted model benchmark for approved daily ETF datasets."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_ai.ai.features import AI_FEATURE_COLUMNS
from trading_ai.backtest.engine import BacktestConfig, run_signal_policy_backtest
from trading_ai.config import ConfigError, load_risk_config, load_universe_config, load_yaml_file
from trading_ai.data.io import read_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.evaluation.forecasting_challenger import FORECAST_FEATURE_COLUMNS
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.evaluation.model_research import (
    AUTO_PERIODS_PER_YEAR,
    ModelResearchOperationalError,
    _approved_metadata,
    _approved_paths,
    _filter_records_by_date,
    _read_json,
)
from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact
from trading_ai.features.engineering import EXTENDED_FEATURE_CANDIDATES, FeatureConfig, build_features
from trading_ai.research.metrics import annualized_sortino, directional_bias
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    load_model,
    temporal_train_test_split,
    train_lightgbm_baseline,
    train_logistic_baseline,
    train_xgboost_baseline,
)

SCHEMA_VERSION = 1

# Candidates whose declared semantics REQUIRE specific feature columns to be
# present in the dataset. When those columns are absent the candidate cannot be
# honestly evaluated -- it is a DECLARABLE condition (same family as
# ``lightgbm``/``xgboost`` when the optional ML dependency is missing), not a
# system error. The list below is the only source of truth for what each
# candidate_id declares as required; candidates not present here keep the
# legacy fallback (empty-features -> ValueError -> ERROR).
_REQUIRED_FEATURES_BY_CANDIDATE_ID: dict[str, tuple[str, ...]] = {
    "logreg_extended_technical": EXTENDED_FEATURE_CANDIDATES,
}

DEFAULT_LOGISTIC_TRAINING_CONFIG = {
    "learning_rate": 0.2,
    "epochs": 200,
    "l2": 0.001,
    "test_fraction": 0.25,
}
DEFAULT_RANDOM_FOREST_TRAINING_CONFIG = {
    "n_estimators": 100,
    "max_depth": 4,
    "random_state": 42,
    "test_fraction": 0.25,
}
BASE_SIGNAL_FEATURE_CANDIDATES = ("momentum_20", "realized_volatility_20", "relative_volume_20")
AI_SIGNAL_FEATURE_CANDIDATES = (
    "ai_sentiment_1d",
    "ai_risk_1d",
    "ai_confidence_1d",
    "ai_event_count_1d",
    "ai_sentiment_5d",
    "ai_risk_5d",
)
FORECAST_SIGNAL_FEATURE_CANDIDATES = (
    "forecast_return_1d",
    "forecast_volatility_5d",
    "forecast_confidence",
)
SUPPLEMENTAL_FEATURE_COLUMNS = tuple(
    dict.fromkeys((*AI_FEATURE_COLUMNS, *AI_SIGNAL_FEATURE_CANDIDATES, *FORECAST_FEATURE_COLUMNS))
)


@dataclass(frozen=True)
class TradingModelBenchmarkResult:
    exit_code: int
    status: str
    output_dir: Path
    ranking_path: Path
    markdown_path: Path
    candidate_spec_path: Path


@dataclass(frozen=True)
class _CandidateEvaluationInput:
    model: Any
    feature_names: tuple[str, ...]
    backtest_records: list[dict[str, Any]]
    train_sample_count: int
    test_sample_count: int
    test_start: str
    test_end: str


def build_benchmark_candidates(available_features: tuple[str, ...]) -> list[dict[str, Any]]:
    available = set(available_features)
    extended = [name for name in EXTENDED_FEATURE_CANDIDATES if name in available]
    base_signal_features = [name for name in BASE_SIGNAL_FEATURE_CANDIDATES if name in available]
    ai_signal_features = [name for name in AI_SIGNAL_FEATURE_CANDIDATES if name in available]
    forecast_signal_features = [name for name in FORECAST_SIGNAL_FEATURE_CANDIDATES if name in available]
    candidates: list[dict[str, Any]] = [
        {
            "candidate_id": "champion_latest_model",
            "family": "champion",
            "model_type": "logistic-baseline",
            "baseline_role": "champion",
            "features": [],
        },
        {
            "candidate_id": "logreg_current_features",
            "family": "logistic",
            "model_type": "logistic-baseline",
            "baseline_role": "challenger",
            "features": base_signal_features,
        },
        {
            "candidate_id": "logreg_extended_technical",
            "family": "logistic",
            "model_type": "logistic-baseline",
            "baseline_role": "challenger",
            "features": extended,
        },
    ]
    if ai_signal_features:
        candidates.append(
            {
                "candidate_id": "logreg_ai_features",
                "family": "logistic",
                "model_type": "logistic-baseline",
                "baseline_role": "challenger",
                "features": [*base_signal_features, *ai_signal_features],
            }
        )
    if forecast_signal_features:
        candidates.append(
            {
                "candidate_id": "logreg_forecast_challenger",
                "family": "logistic",
                "model_type": "logistic-baseline",
                "baseline_role": "challenger",
                "features": [*base_signal_features, *forecast_signal_features],
            }
        )
    tree_features = [name for name in (*BASE_SIGNAL_FEATURE_CANDIDATES, *extended) if name in available]
    candidates.extend(
        [
            {
                "candidate_id": "sklearn_random_forest",
                "family": "sklearn",
                "model_type": "random-forest-classifier",
                "baseline_role": "challenger",
                "features": tree_features,
            },
            {
                "candidate_id": "lightgbm_classifier",
                "family": "lightgbm",
                "model_type": "lightgbm-classifier",
                "baseline_role": "challenger",
                "features": tree_features,
            },
            {
                "candidate_id": "xgboost_classifier",
                "family": "xgboost",
                "model_type": "xgboost-classifier",
                "baseline_role": "challenger",
                "features": tree_features,
            },
        ]
    )
    return candidates


def run_trading_model_benchmark(
    *,
    approved_dir: str | Path,
    start: str,
    end: str,
    as_of_date: str,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    output_dir: str | Path = "reports/tmp/trading_model_benchmark",
    signal_model: str | Path = "models/latest_model.json",
    embargo: int = 1,
    ai_features: str | Path | None = None,
    forecast_features: str | Path | None = None,
) -> TradingModelBenchmarkResult:
    approved_path = Path(approved_dir)
    paths = _approved_paths(approved_path)
    manifest = _read_json(paths["manifest"])
    catalog_entry = _read_json(paths["catalog_entry"])
    metadata = _approved_metadata(manifest, catalog_entry, approved_dir=approved_path)
    if str(metadata.get("as_of_date")) != as_of_date:
        raise ModelResearchOperationalError(
            f"approved dataset as_of_date mismatch: requested={as_of_date} approved={metadata.get('as_of_date')}"
        )

    universe = load_universe_config(config)
    risk_limits = load_risk_config(risk, allow_live=False)
    cost_bps, slippage_bps = _load_costs(risk)
    records = read_records(paths["dataset"])
    actual_manifest = build_dataset_manifest(records, source=str(paths["dataset"]))
    if actual_manifest["dataset_hash"] != metadata["dataset_hash"]:
        raise ModelResearchOperationalError("approved dataset hash mismatch")
    validation = validate_ohlcv_records(records, allowed_symbols=universe.symbols)
    if not validation.valid:
        raise ModelResearchOperationalError("approved dataset validation failed: " + ", ".join(validation.errors))
    window_records = _filter_records_by_date(records, start=start, end=end)
    frequency = str(metadata.get("frequency") or "1d")
    periods_per_year = AUTO_PERIODS_PER_YEAR.get(frequency, 252)
    features = build_features(
        window_records,
        FeatureConfig(periods_per_year=periods_per_year, rsi_window=14, macd_fast=12, bb_window=20),
    )
    feature_sources = {"base_features": _feature_source_summary("approved_dataset_features", features)}
    if ai_features is not None:
        features, source_summary = _merge_supplemental_feature_file(features, ai_features)
        feature_sources.setdefault("ai_features", []).append(source_summary)
    if forecast_features is not None:
        features, source_summary = _merge_supplemental_feature_file(features, forecast_features)
        feature_sources.setdefault("forecast_features", []).append(source_summary)
    available_features = _available_features(features)
    run_dir = Path(output_dir) / str(metadata["dataset_id"]) / frequency / as_of_date
    run_dir.mkdir(parents=True, exist_ok=True)

    backtest_config = BacktestConfig(
        max_gross_exposure=risk_limits.max_gross_exposure,
        max_single_position=risk_limits.max_single_position,
        periods_per_year=periods_per_year,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    rows: list[dict[str, Any]] = []
    for candidate in build_benchmark_candidates(available_features):
        rows.append(
            _evaluate_candidate(
                candidate,
                feature_records=features,
                signal_model=Path(signal_model),
                threshold=0.5,
                min_signal_margin=risk_limits.min_signal_margin,
                max_buy_signals=risk_limits.max_buy_signals,
                backtest_config=backtest_config,
                embargo=embargo,
            )
        )
    ranked = sorted(rows, key=_ranking_key, reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    best = next(
        (row for row in ranked if row.get("status") == "OK" and _row_feature_names(row)),
        ranked[0] if ranked else {},
    )
    status = "CANDIDATE_READY" if best.get("status") == "OK" else "NO_CANDIDATE_READY"

    ranking = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "objective": "risk_adjusted_return",
        "approved_dataset": {**metadata, "window_start": start, "window_end": end},
        "rank_by": ["sharpe", "calmar", "max_drawdown", "estimated_costs", "turnover", "trade_count"],
        "split": {"type": "temporal", "embargo": embargo},
        "risk": {
            "cost_bps": cost_bps,
            "slippage_bps": slippage_bps,
            "max_positions": risk_limits.max_buy_signals,
            "max_gross_exposure": risk_limits.max_gross_exposure,
        },
        "feature_sources": feature_sources,
        "candidates": ranked,
        "best_candidate_id": best.get("candidate_id"),
        "authority": _authority(),
        "safety": _safety(),
    }
    candidate_spec = _candidate_spec(best, metadata=metadata, as_of_date=as_of_date, embargo=embargo)
    ranking_path = run_dir / "ranking.json"
    markdown_path = run_dir / "ranking.md"
    candidate_spec_path = run_dir / "candidate_spec.json"
    write_json_artifact(ranking, ranking_path)
    write_text_artifact(_render_markdown(ranking), markdown_path)
    write_json_artifact(candidate_spec, candidate_spec_path)
    return TradingModelBenchmarkResult(
        0 if status == "CANDIDATE_READY" else 1,
        status,
        run_dir,
        ranking_path,
        markdown_path,
        candidate_spec_path,
    )


def _evaluate_candidate(
    candidate: Mapping[str, Any],
    *,
    feature_records: list[dict[str, Any]],
    signal_model: Path,
    threshold: float,
    min_signal_margin: float,
    max_buy_signals: int,
    backtest_config: BacktestConfig,
    embargo: int,
) -> dict[str, Any]:
    missing_required = _missing_required_features(candidate, feature_records)
    if missing_required:
        return {
            **dict(candidate),
            "status": "SKIPPED",
            "dependency_missing": False,
            "metrics": {},
            "score": -math.inf,
            "reason_codes": [f"missing_required_features:{','.join(missing_required)}"],
        }
    try:
        prepared = _candidate_model(
            candidate,
            feature_records=feature_records,
            signal_model=signal_model,
            embargo=embargo,
        )
        result = run_signal_policy_backtest(
            prepared.backtest_records,
            prepared.model,
            threshold=threshold,
            min_signal_margin=min_signal_margin,
            max_buy_signals=max_buy_signals,
            config=backtest_config,
        )
        metrics = dict(result.metrics)
        metrics["calmar"] = _calmar(metrics)
        # Sprint G2: add the standard Sortino (downside-deviation) and the
        # directional_bias to the row's metrics. Both are computed on the
        # SAME period-return series that produced ``metrics["sharpe"]``
        # (i.e. ``result.daily_returns`` from ``run_signal_policy_backtest``),
        # so we never duplicate the backtest construction. ``sortino``
        # OVERRIDES the backtest engine's non-standard sortino key with the
        # textbook convention; ``directional_bias`` is a new field. The
        # score formula (_score) and ranking key (_ranking_key) are
        # unchanged, so the ranking cannot move on this sprint -- only the
        # report's visibility expands.
        metrics["sortino"] = annualized_sortino(
            result.daily_returns,
            periods_per_year=backtest_config.periods_per_year,
        )
        metrics["directional_bias"] = directional_bias(result.daily_returns)
        return {
            **dict(candidate),
            "features": list(prepared.feature_names),
            "feature_names": list(prepared.feature_names),
            "status": "OK",
            "dependency_missing": False,
            "metrics": metrics,
            "score": _score(metrics),
            "reason_codes": [],
            "split": {
                "train_sample_count": prepared.train_sample_count,
                "test_sample_count": prepared.test_sample_count,
                "backtest_window_start": prepared.test_start,
                "backtest_window_end": prepared.test_end,
            },
        }
    except ImportError as exc:
        return {
            **dict(candidate),
            "status": "SKIPPED",
            "dependency_missing": True,
            "metrics": {},
            "score": -math.inf,
            "reason_codes": [f"dependency_missing:{type(exc).__name__}"],
        }
    except Exception as exc:
        return {
            **dict(candidate),
            "status": "ERROR",
            "dependency_missing": False,
            "metrics": {},
            "score": -math.inf,
            "reason_codes": [f"evaluation_error:{type(exc).__name__}"],
        }


def _candidate_model(
    candidate: Mapping[str, Any],
    *,
    feature_records: list[dict[str, Any]],
    signal_model: Path,
    embargo: int,
) -> _CandidateEvaluationInput:
    if candidate["candidate_id"] == "champion_latest_model":
        model = load_model(str(signal_model))
        features = _model_feature_names(model)
        split = _temporal_split_for_features(feature_records, features=features, embargo=embargo)
        return _CandidateEvaluationInput(
            model=model,
            feature_names=features,
            backtest_records=_held_out_feature_records(feature_records, split.test),
            train_sample_count=len(split.train),
            test_sample_count=len(split.test),
            test_start=split.test[0].timestamp,
            test_end=split.test[-1].timestamp,
        )
    features = tuple(str(name) for name in candidate.get("features", []) if str(name))
    if not features:
        raise ValueError("candidate has no available features")
    split = _temporal_split_for_features(feature_records, features=features, embargo=embargo)
    family = str(candidate["family"])
    if family == "logistic":
        model = train_logistic_baseline(split.train, LogisticBaselineConfig(feature_names=features))
    elif family == "lightgbm":
        from trading_ai.models.baseline import LightGBMBaselineConfig

        model = train_lightgbm_baseline(split.train, LightGBMBaselineConfig(feature_names=features))
    elif family == "xgboost":
        from trading_ai.models.baseline import XGBoostBaselineConfig

        model = train_xgboost_baseline(split.train, XGBoostBaselineConfig(feature_names=features))
    elif family == "sklearn":
        model = _train_sklearn_random_forest(split.train, features)
    else:
        raise ValueError(f"unknown candidate family: {family}")
    return _CandidateEvaluationInput(
        model=model,
        feature_names=features,
        backtest_records=_held_out_feature_records(feature_records, split.test),
        train_sample_count=len(split.train),
        test_sample_count=len(split.test),
        test_start=split.test[0].timestamp,
        test_end=split.test[-1].timestamp,
    )


def _model_feature_names(model: Any) -> tuple[str, ...]:
    features = tuple(str(name).strip() for name in getattr(model, "feature_names", ()) if str(name).strip())
    if not features:
        raise ValueError("candidate model has no feature names")
    return features


def _temporal_split_for_features(
    feature_records: list[dict[str, Any]],
    *,
    features: tuple[str, ...],
    embargo: int,
):
    examples = build_supervised_examples(feature_records, feature_names=features)
    return temporal_train_test_split(examples, test_fraction=0.25, embargo=embargo)


def _held_out_feature_records(
    feature_records: list[dict[str, Any]],
    test_examples: tuple[Any, ...],
) -> list[dict[str, Any]]:
    if not test_examples:
        raise ValueError("temporal split has no test examples")
    first_test_timestamp = min(str(example.timestamp) for example in test_examples)
    rows = [row for row in feature_records if str(row.get("timestamp") or "") >= first_test_timestamp]
    if len(rows) < 2:
        raise ValueError("held-out backtest window has fewer than two rows")
    return rows


def _train_sklearn_random_forest(examples: Any, features: tuple[str, ...]) -> Any:
    try:
        import numpy as np  # noqa: PLC0415
        from sklearn.ensemble import RandomForestClassifier  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("sklearn random forest requires scikit-learn") from exc
    rows = tuple(examples)
    clf = RandomForestClassifier(n_estimators=100, max_depth=4, random_state=42)
    clf.fit(np.array([row.features for row in rows], dtype=float), np.array([row.target for row in rows], dtype=int))

    class SklearnModel:
        feature_names = features

        def predict_probability(self, row_features: tuple[float, ...]) -> float:
            proba = clf.predict_proba(np.array([row_features], dtype=float))[0]
            return float(proba[1])

    return SklearnModel()


def _load_costs(path: str | Path) -> tuple[float, float]:
    default = BacktestConfig()
    try:
        payload = load_yaml_file(path)
    except ConfigError:
        return default.cost_bps, default.slippage_bps
    costs = payload.get("costs", {})
    if not isinstance(costs, Mapping):
        return default.cost_bps, default.slippage_bps
    return (
        _non_negative_cost(costs.get("cost_bps"), default.cost_bps),
        _non_negative_cost(costs.get("slippage_bps"), default.slippage_bps),
    )


def _non_negative_cost(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def _merge_supplemental_feature_file(
    feature_records: list[dict[str, Any]],
    supplemental_path: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    supplemental_rows = read_records(supplemental_path)
    source_summary = _feature_source_summary(str(Path(supplemental_path)), supplemental_rows)
    source_summary["path"] = str(Path(supplemental_path))
    source_summary["merged_columns"] = [
        column for column in SUPPLEMENTAL_FEATURE_COLUMNS if _has_finite_column_value(supplemental_rows, column)
    ]
    return _merge_supplemental_feature_rows(feature_records, supplemental_rows), source_summary


def _merge_supplemental_feature_rows(
    feature_records: list[dict[str, Any]],
    supplemental_rows: list[dict[str, object]],
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, float]] = {}
    for row in supplemental_rows:
        key = (str(row.get("timestamp") or ""), str(row.get("symbol") or "").upper())
        if not key[0] or not key[1]:
            continue
        values: dict[str, float] = {}
        for column in SUPPLEMENTAL_FEATURE_COLUMNS:
            if column not in row:
                continue
            value = _finite_float(row.get(column))
            if value is not None:
                values[column] = value
        if values:
            by_key[key] = values
    merged: list[dict[str, Any]] = []
    for row in feature_records:
        output = dict(row)
        key = (str(output.get("timestamp") or ""), str(output.get("symbol") or "").upper())
        output.update(by_key.get(key, {}))
        merged.append(output)
    return merged


def _feature_source_summary(source: str, rows: list[dict[str, object]]) -> dict[str, Any]:
    manifest = (
        build_dataset_manifest(rows, source=source)
        if rows
        else {"dataset_hash": None, "columns": [], "row_count": 0}
    )
    return {
        "source": source,
        "row_count": manifest.get("row_count", len(rows)),
        "dataset_hash": manifest.get("dataset_hash"),
        "columns": manifest.get("columns", []),
    }


def _has_finite_column_value(rows: list[dict[str, object]], column: str) -> bool:
    return any(_finite_float(row.get(column)) is not None for row in rows)


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _available_features(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    names: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if value in {None, ""}:
                continue
            try:
                if math.isfinite(float(value)):
                    names.add(str(key))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(names))


def _missing_required_features(
    candidate: Mapping[str, Any],
    feature_records: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Return the subset of a candidate's declared required features that are
    missing from ``feature_records``.

    Only candidates listed in ``_REQUIRED_FEATURES_BY_CANDIDATE_ID`` declare
    required features (the ``logreg_extended_technical`` candidate requires
    ``rsi_14``/``macd_hist``/``bb_pct_b``). Any other candidate returns an
    empty tuple so the caller falls through to the regular try/except path,
    preserving the legacy ``ERROR`` semantics for empty-features or genuine
    training failures on candidates that do NOT carry a declared requirement.
    """

    required = _REQUIRED_FEATURES_BY_CANDIDATE_ID.get(str(candidate.get("candidate_id") or ""))
    if not required:
        return ()
    available = set(_available_features(feature_records))
    return tuple(name for name in required if name not in available)


def _ranking_key(row: Mapping[str, Any]) -> tuple[float, float, float, float, float, float]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    assert isinstance(metrics, Mapping)
    return (
        float(metrics.get("sharpe", -math.inf)),
        float(metrics.get("calmar", -math.inf)),
        -float(metrics.get("max_drawdown", math.inf)),
        -float(metrics.get("estimated_costs", math.inf)),
        -float(metrics.get("turnover", math.inf)),
        float(metrics.get("trade_count", 0.0)),
    )


def _score(metrics: Mapping[str, Any]) -> float:
    return (
        float(metrics.get("sharpe", 0.0))
        + 0.5 * float(metrics.get("calmar", 0.0))
        - float(metrics.get("max_drawdown", 0.0))
        - float(metrics.get("estimated_costs", 0.0))
        - 0.001 * float(metrics.get("turnover", 0.0))
    )


def _calmar(metrics: Mapping[str, Any]) -> float:
    drawdown = float(metrics.get("max_drawdown", 0.0))
    cagr = float(metrics.get("cagr", 0.0))
    return cagr / drawdown if drawdown > 1e-12 else 0.0


def _candidate_spec(
    best: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    as_of_date: str,
    embargo: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": best.get("candidate_id"),
        "model_type": best.get("model_type"),
        "feature_names": _row_feature_names(best),
        "preprocessing": {"type": "none"},
        "training_config": _candidate_training_config(best, embargo=embargo),
        "objective": "risk_adjusted_return",
        "dataset_hash": metadata.get("dataset_hash"),
        "source_sha256": metadata.get("source_sha256"),
        "as_of_date": as_of_date,
        "rank": best.get("rank"),
        "metrics": best.get("metrics", {}),
        "authority": _authority(),
        "safety": _safety(),
    }


def _row_feature_names(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("feature_names") or row.get("features") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(name).strip() for name in raw if str(name).strip()]


def _candidate_training_config(best: Mapping[str, Any], *, embargo: int) -> dict[str, Any]:
    family = str(best.get("family") or "")
    if family == "sklearn":
        return {**DEFAULT_RANDOM_FOREST_TRAINING_CONFIG, "embargo": embargo}
    if family == "lightgbm":
        return {"test_fraction": 0.25, "embargo": embargo, "random_state": 42}
    if family == "xgboost":
        return {"test_fraction": 0.25, "embargo": embargo, "random_state": 42}
    return {**DEFAULT_LOGISTIC_TRAINING_CONFIG, "embargo": embargo}


def _authority() -> dict[str, object]:
    return {
        "llm_authority": "none",
        "mutates_latest_model": False,
        "orders_submitted": False,
        "broker_client_built": False,
        "credentials_read": False,
    }


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "orders_submitted": False,
        "live_trading_allowed": False,
        "futures_forex_execution": False,
        "llm_authority": "none",
        "llm_order_authority": "none",
    }


def _render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Trading Model Benchmark",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Objective: `{payload.get('objective')}`",
        f"- Best candidate: `{payload.get('best_candidate_id')}`",
        "",
        "| Rank | Candidate | Status | Sharpe | Calmar | Sortino | Dir Bias | Max DD | Costs | Turnover |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in payload.get("candidates", []):
        item = row if isinstance(row, Mapping) else {}
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        assert isinstance(metrics, Mapping)
        lines.append(
            (
                "| {rank} | `{candidate}` | `{status}` | {sharpe:.4f} | "
                "{calmar:.4f} | {sortino:.4f} | {bias:.4f} | {dd:.4f} | "
                "{costs:.4f} | {turnover:.4f} |"
            ).format(
                rank=item.get("rank", ""),
                candidate=item.get("candidate_id", ""),
                status=item.get("status", ""),
                sharpe=float(metrics.get("sharpe", 0.0)),
                calmar=float(metrics.get("calmar", 0.0)),
                sortino=float(metrics.get("sortino", 0.0)),
                bias=float(metrics.get("directional_bias", 0.0)),
                dd=float(metrics.get("max_drawdown", 0.0)),
                costs=float(metrics.get("estimated_costs", 0.0)),
                turnover=float(metrics.get("turnover", 0.0)),
            )
        )
    lines.append("")
    return "\n".join(lines)
