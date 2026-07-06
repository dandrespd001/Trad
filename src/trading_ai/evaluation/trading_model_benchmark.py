"""Risk-adjusted model benchmark for approved daily ETF datasets."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_ai.backtest.engine import BacktestConfig, run_signal_policy_backtest
from trading_ai.config import load_risk_config, load_universe_config, load_yaml_file
from trading_ai.data.io import read_records
from trading_ai.data.manifest import build_dataset_manifest
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


@dataclass(frozen=True)
class TradingModelBenchmarkResult:
    exit_code: int
    status: str
    output_dir: Path
    ranking_path: Path
    markdown_path: Path
    candidate_spec_path: Path


def build_benchmark_candidates(available_features: tuple[str, ...]) -> list[dict[str, Any]]:
    available = set(available_features)
    extended = [name for name in EXTENDED_FEATURE_CANDIDATES if name in available]
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
            "features": [name for name in ("momentum_20", "realized_volatility_20", "relative_volume_20") if name in available],
        },
        {
            "candidate_id": "logreg_extended_technical",
            "family": "logistic",
            "model_type": "logistic-baseline",
            "baseline_role": "challenger",
            "features": extended,
        },
        {
            "candidate_id": "sklearn_random_forest",
            "family": "sklearn",
            "model_type": "random-forest-classifier",
            "baseline_role": "challenger",
            "features": [name for name in ("momentum_20", "realized_volatility_20", "relative_volume_20", *extended) if name in available],
        },
        {
            "candidate_id": "lightgbm_classifier",
            "family": "lightgbm",
            "model_type": "lightgbm-classifier",
            "baseline_role": "challenger",
            "features": [name for name in ("momentum_20", "realized_volatility_20", "relative_volume_20", *extended) if name in available],
        },
        {
            "candidate_id": "xgboost_classifier",
            "family": "xgboost",
            "model_type": "xgboost-classifier",
            "baseline_role": "challenger",
            "features": [name for name in ("momentum_20", "realized_volatility_20", "relative_volume_20", *extended) if name in available],
        },
    ]
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
                threshold=0.5 + risk_limits.min_signal_margin,
                min_signal_margin=risk_limits.min_signal_margin,
                max_buy_signals=risk_limits.max_buy_signals,
                backtest_config=backtest_config,
                embargo=embargo,
            )
        )
    ranked = sorted(rows, key=_ranking_key, reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    best = next((row for row in ranked if row.get("status") == "OK"), ranked[0] if ranked else {})
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
    try:
        model = _candidate_model(candidate, feature_records=feature_records, signal_model=signal_model, embargo=embargo)
        result = run_signal_policy_backtest(
            feature_records,
            model,
            threshold=threshold,
            min_signal_margin=min_signal_margin,
            max_buy_signals=max_buy_signals,
            config=backtest_config,
        )
        metrics = dict(result.metrics)
        metrics["calmar"] = _calmar(metrics)
        return {
            **dict(candidate),
            "status": "OK",
            "dependency_missing": False,
            "metrics": metrics,
            "score": _score(metrics),
            "reason_codes": [],
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
) -> Any:
    if candidate["candidate_id"] == "champion_latest_model":
        return load_model(str(signal_model))
    features = tuple(str(name) for name in candidate.get("features", []) if str(name))
    if not features:
        raise ValueError("candidate has no available features")
    examples = build_supervised_examples(feature_records, feature_names=features)
    split = temporal_train_test_split(examples, test_fraction=0.25, embargo=embargo)
    family = str(candidate["family"])
    if family == "logistic":
        return train_logistic_baseline(split.train, LogisticBaselineConfig(feature_names=features))
    if family == "lightgbm":
        from trading_ai.models.baseline import LightGBMBaselineConfig

        return train_lightgbm_baseline(split.train, LightGBMBaselineConfig(feature_names=features))
    if family == "xgboost":
        from trading_ai.models.baseline import XGBoostBaselineConfig

        return train_xgboost_baseline(split.train, XGBoostBaselineConfig(feature_names=features))
    if family == "sklearn":
        return _train_sklearn_random_forest(split.train, features)
    raise ValueError(f"unknown candidate family: {family}")


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
    payload = load_yaml_file(path)
    costs = payload.get("costs", {})
    if not isinstance(costs, Mapping):
        return 0.0, 0.0
    return float(costs.get("cost_bps", 0.0)), float(costs.get("slippage_bps", 0.0))


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
        "feature_names": best.get("features", []),
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
        "| Rank | Candidate | Status | Sharpe | Calmar | Max DD | Costs | Turnover |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in payload.get("candidates", []):
        item = row if isinstance(row, Mapping) else {}
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        assert isinstance(metrics, Mapping)
        lines.append(
            "| {rank} | `{candidate}` | `{status}` | {sharpe:.4f} | {calmar:.4f} | {dd:.4f} | {costs:.4f} | {turnover:.4f} |".format(
                rank=item.get("rank", ""),
                candidate=item.get("candidate_id", ""),
                status=item.get("status", ""),
                sharpe=float(metrics.get("sharpe", 0.0)),
                calmar=float(metrics.get("calmar", 0.0)),
                dd=float(metrics.get("max_drawdown", 0.0)),
                costs=float(metrics.get("estimated_costs", 0.0)),
                turnover=float(metrics.get("turnover", 0.0)),
            )
        )
    lines.append("")
    return "\n".join(lines)
