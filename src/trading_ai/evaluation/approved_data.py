"""Reproducible evaluation package for approved local datasets."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from trading_ai.backtest.engine import BacktestConfig, BacktestResult, run_momentum_vol_target_backtest
from trading_ai.config import load_risk_config, load_universe_config
from trading_ai.data.catalog import SUPPORTED_FREQUENCIES
from trading_ai.data.io import read_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.features.engineering import FeatureConfig, build_features
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    build_supervised_examples,
    evaluate_classifier,
    temporal_train_test_split,
    train_logistic_baseline,
    walk_forward_evaluate,
)
from trading_ai.models.promotion import PromotionPolicy, evaluate_promotion
from trading_ai.reports.markdown import render_backtest_report


SCHEMA_VERSION = 1
APPROVED_EVAL_STATUS_APPROVED = "APPROVED"
APPROVED_EVAL_STATUS_REJECTED = "REJECTED"
APPROVED_EVAL_STATUS_BLOCKED = "BLOCKED"
AUTO_PERIODS_PER_YEAR = {"1d": 252, "1h": 1638}
REQUIRED_APPROVED_FILES = ("ohlcv.parquet", "manifest.json", "catalog_entry.json")
REQUIRED_MANIFEST_FIELDS = (
    "dataset_id",
    "frequency",
    "dataset_hash",
    "source_sha256",
    "row_count",
    "start",
    "end",
    "symbols",
    "columns",
)


class ApprovedEvaluationOperationalError(RuntimeError):
    """Raised for operational failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class ApprovedEvaluationResult:
    exit_code: int
    status: str
    output_dir: Path
    summary_path: Path
    summary_markdown_path: Path
    data_quality_path: Path
    promotion_decision_path: Path | None


def evaluate_approved_data(
    *,
    approved_dir: str | Path,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    output_dir: str | Path = "reports/tmp/approved_eval",
    as_of_date: str | date,
    periods_per_year: str | int = "auto",
    min_accuracy_lift: float = 0.02,
    min_test_samples: int = 30,
) -> ApprovedEvaluationResult:
    """Evaluate a governed approved dataset without network, broker, or model mutation."""

    resolved_as_of_date = _parse_date(as_of_date, "as_of_date").isoformat()
    approved_path = Path(approved_dir)
    paths = _approved_paths(approved_path)
    manifest = _read_required_json(paths["manifest"])
    catalog_entry = _read_required_json(paths["catalog_entry"])
    _validate_approved_metadata(manifest, catalog_entry)

    dataset_id = str(manifest["dataset_id"])
    frequency = str(manifest["frequency"])
    resolved_periods = _resolve_periods_per_year(periods_per_year, frequency)
    run_dir = Path(output_dir) / dataset_id / frequency / resolved_as_of_date
    run_dir.mkdir(parents=True, exist_ok=True)

    records = read_records(paths["dataset"])
    actual_manifest = build_dataset_manifest(records, source=str(paths["dataset"]))
    if actual_manifest["dataset_hash"] != manifest["dataset_hash"]:
        raise ApprovedEvaluationOperationalError(
            "approved dataset hash mismatch: "
            f"manifest={manifest['dataset_hash']} actual={actual_manifest['dataset_hash']}"
        )

    metadata = _approved_metadata(
        manifest,
        catalog_entry,
        approved_dir=approved_path,
        dataset_path=paths["dataset"],
        manifest_path=paths["manifest"],
        catalog_entry_path=paths["catalog_entry"],
        periods_per_year=resolved_periods,
    )
    data_quality = _evaluate_data_quality(records, metadata=metadata, config=config)
    data_quality_path = run_dir / "data_quality.json"
    _write_json(data_quality, data_quality_path)
    if not data_quality["passed"]:
        summary = _build_blocked_summary(
            status=APPROVED_EVAL_STATUS_BLOCKED,
            reasons=list(data_quality["reasons"]),
            metadata=metadata,
            data_quality=data_quality,
            artifact_paths={"data_quality": data_quality_path},
        )
        summary_path = run_dir / "evaluation_summary.json"
        summary_markdown_path = run_dir / "evaluation_summary.md"
        _write_json(summary, summary_path)
        _write_text(render_evaluation_summary_markdown(summary), summary_markdown_path)
        return ApprovedEvaluationResult(
            exit_code=1,
            status=APPROVED_EVAL_STATUS_BLOCKED,
            output_dir=run_dir,
            summary_path=summary_path,
            summary_markdown_path=summary_markdown_path,
            data_quality_path=data_quality_path,
            promotion_decision_path=None,
        )

    risk_limits = load_risk_config(risk)
    backtest = run_momentum_vol_target_backtest(
        records,
        BacktestConfig(
            max_gross_exposure=risk_limits.max_gross_exposure,
            max_single_position=risk_limits.max_single_position,
            periods_per_year=resolved_periods,
        ),
    )
    backtest = _with_metadata(backtest, metadata)
    backtest_path = run_dir / "backtest.json"
    backtest_markdown_path = run_dir / "backtest.md"
    backtest_payload = backtest.to_dict()
    backtest_payload["schema_version"] = SCHEMA_VERSION
    backtest_payload["approved_dataset"] = dict(metadata)
    _write_json(backtest_payload, backtest_path)
    _write_text(render_backtest_report(backtest, title="Approved Data Backtest"), backtest_markdown_path)

    feature_records = build_features(records, FeatureConfig(periods_per_year=resolved_periods))
    model_run, model_eval, promotion_decision = _run_model_evaluation(
        feature_records=feature_records,
        metadata=metadata,
        policy=PromotionPolicy(
            min_accuracy_lift=min_accuracy_lift,
            min_test_samples=min_test_samples,
        ),
    )
    walk_forward_report = _walk_forward_report(
        model_run=_mapping(model_run),
        model_eval=model_eval,
        metadata=metadata,
        policy=PromotionPolicy(min_accuracy_lift=min_accuracy_lift, min_test_samples=min_test_samples),
    )
    regime_slices = _regime_slices_report(
        feature_records=feature_records,
        metadata=metadata,
    )
    promotion_decision = _apply_challenger_robustness(
        promotion_decision=promotion_decision,
        backtest=backtest,
        walk_forward_report=walk_forward_report,
        regime_slices=regime_slices,
        feature_records=feature_records,
        policy=PromotionPolicy(min_accuracy_lift=min_accuracy_lift, min_test_samples=min_test_samples),
    )
    model_run_path = run_dir / "model_run.json"
    model_eval_path = run_dir / "model_eval.json"
    promotion_decision_path = run_dir / "promotion_decision.json"
    walk_forward_path = run_dir / "walk_forward.json"
    regime_slices_path = run_dir / "regime_slices.json"
    _write_json(model_run, model_run_path)
    _write_json(model_eval, model_eval_path)
    _write_json(promotion_decision, promotion_decision_path)
    _write_json(walk_forward_report, walk_forward_path)
    _write_json(regime_slices, regime_slices_path)

    status = APPROVED_EVAL_STATUS_APPROVED if promotion_decision["eligible_for_paper_challenger"] else APPROVED_EVAL_STATUS_REJECTED
    reasons = list(promotion_decision["reasons"])
    artifact_paths = {
        "data_quality": data_quality_path,
        "backtest": backtest_path,
        "backtest_markdown": backtest_markdown_path,
        "model_run": model_run_path,
        "model_eval": model_eval_path,
        "promotion_decision": promotion_decision_path,
        "walk_forward": walk_forward_path,
        "regime_slices": regime_slices_path,
    }
    summary = _build_completed_summary(
        status=status,
        reasons=reasons,
        metadata=metadata,
        data_quality=data_quality,
        backtest=backtest,
        model_eval=model_eval,
        promotion_decision=promotion_decision,
        artifact_paths=artifact_paths,
    )
    summary_path = run_dir / "evaluation_summary.json"
    summary_markdown_path = run_dir / "evaluation_summary.md"
    _write_json(summary, summary_path)
    _write_text(render_evaluation_summary_markdown(summary), summary_markdown_path)

    return ApprovedEvaluationResult(
        exit_code=0 if status == APPROVED_EVAL_STATUS_APPROVED else 1,
        status=status,
        output_dir=run_dir,
        summary_path=summary_path,
        summary_markdown_path=summary_markdown_path,
        data_quality_path=data_quality_path,
        promotion_decision_path=promotion_decision_path,
    )


def render_evaluation_summary_markdown(summary: Mapping[str, object]) -> str:
    metadata = _mapping(summary.get("approved_dataset"))
    metrics = _mapping(summary.get("metrics"))
    artifacts = _mapping(summary.get("artifacts"))
    reasons = [str(reason) for reason in summary.get("reasons", [])] if isinstance(summary.get("reasons"), list) else []

    lines = [
        "# Approved Data Evaluation",
        "",
        f"- Status: {summary.get('status')}",
        f"- Eligible for paper challenger: {str(summary.get('eligible_for_paper_challenger')).lower()}",
        f"- Dataset: `{metadata.get('dataset_id')}` `{metadata.get('frequency')}`",
        f"- Dataset hash: `{metadata.get('dataset_hash')}`",
        f"- Source SHA-256: `{metadata.get('source_sha256')}`",
        f"- Range: {metadata.get('start')} to {metadata.get('end')}",
        f"- Rows: {metadata.get('row_count')}",
        f"- Symbols: {', '.join(str(symbol) for symbol in metadata.get('symbols', []))}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in (
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "turnover",
        "estimated_costs",
        "trade_count",
        "accuracy",
        "baseline_accuracy",
        "accuracy_lift",
        "log_loss",
        "sample_count",
    ):
        if key in metrics:
            lines.append(f"| {key} | {_format_summary_value(metrics[key])} |")
    if reasons:
        lines.extend(["", "## Reasons", ""])
        for reason in reasons:
            lines.append(f"- {reason}")
    lines.extend(["", "## Artifacts", ""])
    for name, payload in artifacts.items():
        artifact = _mapping(payload)
        lines.append(f"- {name}: `{artifact.get('path')}` (`{artifact.get('sha256')}`)")
    return "\n".join(lines) + "\n"


def _approved_paths(approved_dir: Path) -> dict[str, Path]:
    missing = [name for name in REQUIRED_APPROVED_FILES if not (approved_dir / name).exists()]
    if missing:
        raise ApprovedEvaluationOperationalError(
            "approved dataset package is missing required file(s): " + ", ".join(missing)
        )
    return {
        "dataset": approved_dir / "ohlcv.parquet",
        "manifest": approved_dir / "manifest.json",
        "catalog_entry": approved_dir / "catalog_entry.json",
    }


def _read_required_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovedEvaluationOperationalError(f"invalid approved dataset JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ApprovedEvaluationOperationalError(f"approved dataset JSON must be an object: {path}")
    return payload


def _validate_approved_metadata(manifest: Mapping[str, object], catalog_entry: Mapping[str, object]) -> None:
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise ApprovedEvaluationOperationalError("manifest missing required field(s): " + ", ".join(missing))
    dataset_id = str(manifest["dataset_id"])
    frequency = str(manifest["frequency"])
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ApprovedEvaluationOperationalError(f"unsupported approved dataset frequency: {frequency}")
    for field in ("dataset_id", "frequency", "dataset_hash"):
        if str(catalog_entry.get(field)) != str(manifest[field]):
            raise ApprovedEvaluationOperationalError(f"catalog entry does not match manifest field: {field}")
    if not dataset_id:
        raise ApprovedEvaluationOperationalError("manifest dataset_id is empty")
    if not isinstance(manifest.get("symbols"), list) or not manifest["symbols"]:
        raise ApprovedEvaluationOperationalError("manifest symbols must be a non-empty list")
    if not isinstance(manifest.get("columns"), list) or not manifest["columns"]:
        raise ApprovedEvaluationOperationalError("manifest columns must be a non-empty list")
    if len(str(manifest.get("dataset_hash", ""))) != 64:
        raise ApprovedEvaluationOperationalError("manifest dataset_hash must be a SHA-256 hex digest")
    if len(str(manifest.get("source_sha256", ""))) != 64:
        raise ApprovedEvaluationOperationalError("manifest source_sha256 must be a SHA-256 hex digest")


def _approved_metadata(
    manifest: Mapping[str, object],
    catalog_entry: Mapping[str, object],
    *,
    approved_dir: Path,
    dataset_path: Path,
    manifest_path: Path,
    catalog_entry_path: Path,
    periods_per_year: int,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": str(manifest["dataset_id"]),
        "frequency": str(manifest["frequency"]),
        "dataset_hash": str(manifest["dataset_hash"]),
        "source_sha256": str(manifest["source_sha256"]),
        "start": manifest.get("start"),
        "end": manifest.get("end"),
        "symbols": [str(symbol).upper() for symbol in manifest.get("symbols", [])],
        "row_count": int(manifest.get("row_count", 0)),
        "columns": [str(column) for column in manifest.get("columns", [])],
        "approved_dir": str(approved_dir),
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "catalog_entry_path": str(catalog_entry_path),
        "catalog_entry": {
            "provider": catalog_entry.get("provider"),
            "provider_kind": catalog_entry.get("provider_kind"),
            "network_allowed": bool(catalog_entry.get("network_allowed", False)),
            "license_note": catalog_entry.get("license_note"),
            "as_of_date": catalog_entry.get("as_of_date"),
        },
        "periods_per_year": periods_per_year,
    }


def _evaluate_data_quality(
    records: list[dict[str, object]],
    *,
    metadata: Mapping[str, object],
    config: str | Path,
) -> dict[str, object]:
    universe = load_universe_config(config)
    validation = validate_ohlcv_records(records, allowed_symbols=universe.symbols)
    timestamp_errors = _validate_frequency_timestamps(records, frequency=str(metadata["frequency"]))
    manifest_errors = _validate_manifest_consistency(records, metadata)
    reasons = [*validation.errors, *timestamp_errors, *manifest_errors]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": APPROVED_EVAL_STATUS_APPROVED if not reasons else APPROVED_EVAL_STATUS_BLOCKED,
        "passed": not reasons,
        "reasons": reasons,
        "approved_dataset": dict(metadata),
        "validation": {
            "valid": validation.valid and not timestamp_errors and not manifest_errors,
            "errors": reasons,
            "row_count": validation.row_count,
            "symbols": list(validation.symbols),
        },
        "universe": {
            "config_path": str(config),
            "symbols": list(universe.symbols),
        },
    }


def _validate_frequency_timestamps(records: Iterable[Mapping[str, object]], *, frequency: str) -> list[str]:
    errors: list[str] = []
    for index, row in enumerate(records):
        if "timestamp" not in row or row["timestamp"] in {None, ""}:
            continue
        if not _timestamp_matches_frequency(row["timestamp"], frequency=frequency):
            errors.append(f"row {index} timestamp incompatible with {frequency}: {row['timestamp']}")
    return errors


def _timestamp_matches_frequency(value: object, *, frequency: str) -> bool:
    resolved = _coerce_datetime(value)
    if resolved is None:
        return False
    raw = str(value).strip()
    has_time = "T" in raw or " " in raw or isinstance(value, datetime)
    if frequency == "1d":
        return not any((resolved.hour, resolved.minute, resolved.second, resolved.microsecond))
    if frequency == "1h":
        return has_time and resolved.minute == 0 and resolved.second == 0 and resolved.microsecond == 0
    return False


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        converted = to_pydatetime()
        if isinstance(converted, datetime):
            return converted
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    if "T" not in raw and " " not in raw:
        try:
            return datetime.fromisoformat(f"{raw}T00:00:00")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _validate_manifest_consistency(records: list[dict[str, object]], metadata: Mapping[str, object]) -> list[str]:
    actual = build_dataset_manifest(records, source=str(metadata["dataset_path"]))
    errors: list[str] = []
    for key in ("row_count", "start", "end"):
        if actual[key] != metadata[key]:
            errors.append(f"manifest {key} mismatch: manifest={metadata[key]} actual={actual[key]}")
    if list(actual["symbols"]) != list(metadata["symbols"]):
        errors.append(f"manifest symbols mismatch: manifest={metadata['symbols']} actual={actual['symbols']}")
    if list(actual["columns"]) != list(metadata["columns"]):
        errors.append(f"manifest columns mismatch: manifest={metadata['columns']} actual={actual['columns']}")
    return errors


def _run_model_evaluation(
    *,
    feature_records: list[dict[str, object]],
    metadata: Mapping[str, object],
    policy: PromotionPolicy,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    feature_manifest = build_dataset_manifest(feature_records, source=str(metadata["dataset_path"]))
    feature_names = _default_feature_names(feature_records)
    model_config = LogisticBaselineConfig(feature_names=feature_names)
    examples = build_supervised_examples(feature_records, feature_names=model_config.feature_names)
    if len(examples) < 2:
        return _insufficient_model_evaluation(
            metadata=metadata,
            feature_dataset_hash=str(feature_manifest["dataset_hash"]),
            feature_names=model_config.feature_names,
            policy=policy,
        )
    split = temporal_train_test_split(examples, test_fraction=model_config.test_fraction)
    model = train_logistic_baseline(split.train, model_config)
    challenger_train = evaluate_classifier(model, split.train)
    challenger_test = evaluate_classifier(model, split.test)
    majority_baseline = _majority_classifier_metrics(split.test)
    walk_forward = walk_forward_evaluate(
        examples,
        model_config,
        min_train_size=max(2, len(split.train) // 2),
        test_size=max(1, len(split.test)),
    )
    decision = evaluate_promotion(
        challenger_metrics=challenger_test,
        baseline_metrics=majority_baseline,
        policy=policy,
    )

    common = {"schema_version": SCHEMA_VERSION, "approved_dataset": dict(metadata)}
    model_run = {
        **common,
        "model_type": "logistic-baseline",
        "model": model.to_dict(),
        "feature_dataset_hash": feature_manifest["dataset_hash"],
        "feature_names": list(model_config.feature_names),
        "train_range": [split.train[0].timestamp, split.train[-1].timestamp],
        "test_range": [split.test[0].timestamp, split.test[-1].timestamp],
        "metrics": {
            "train": challenger_train,
            "test": challenger_test,
            "walk_forward": walk_forward,
        },
    }
    model_eval = {
        **common,
        "model_type": "logistic-baseline",
        "feature_dataset_hash": feature_manifest["dataset_hash"],
        "feature_names": list(model_config.feature_names),
        "metrics": {
            "train": challenger_train,
            "test": challenger_test,
            "majority_baseline": majority_baseline,
        },
        "baseline": {
            "type": "majority_class_classifier",
            "scope": "same_temporal_test_set",
            "metrics": majority_baseline,
        },
    }
    promotion_payload = {
        **common,
        **decision.to_dict(),
        "eligible_for_paper_challenger": decision.approved,
        "policy": {
            "min_accuracy_lift": policy.min_accuracy_lift,
            "min_test_samples": policy.min_test_samples,
        },
        "baseline": {
            "type": "majority_class_classifier",
            "scope": "same_temporal_test_set",
        },
    }
    return model_run, model_eval, promotion_payload


def _insufficient_model_evaluation(
    *,
    metadata: Mapping[str, object],
    feature_dataset_hash: str,
    feature_names: tuple[str, ...],
    policy: PromotionPolicy,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    empty_metrics = _empty_classifier_metrics()
    decision = evaluate_promotion(
        challenger_metrics=empty_metrics,
        baseline_metrics=empty_metrics,
        policy=policy,
    )
    reasons = list(decision.reasons)
    if "insufficient_supervised_examples" not in reasons:
        reasons.append("insufficient_supervised_examples")
    common = {"schema_version": SCHEMA_VERSION, "approved_dataset": dict(metadata)}
    model_run = {
        **common,
        "model_type": "logistic-baseline",
        "status": "SKIPPED",
        "reasons": reasons,
        "model": None,
        "feature_dataset_hash": feature_dataset_hash,
        "feature_names": list(feature_names),
        "train_range": [None, None],
        "test_range": [None, None],
        "metrics": {
            "train": empty_metrics,
            "test": empty_metrics,
            "walk_forward": {"window_count": 0.0, "mean_accuracy": 0.0, "windows": []},
        },
    }
    model_eval = {
        **common,
        "model_type": "logistic-baseline",
        "status": "SKIPPED",
        "reasons": reasons,
        "feature_dataset_hash": feature_dataset_hash,
        "feature_names": list(feature_names),
        "metrics": {
            "train": empty_metrics,
            "test": empty_metrics,
            "majority_baseline": empty_metrics,
        },
        "baseline": {
            "type": "majority_class_classifier",
            "scope": "same_temporal_test_set",
            "metrics": empty_metrics,
        },
    }
    promotion_payload = {
        **common,
        **decision.to_dict(),
        "reasons": reasons,
        "eligible_for_paper_challenger": False,
        "policy": {
            "min_accuracy_lift": policy.min_accuracy_lift,
            "min_test_samples": policy.min_test_samples,
        },
        "baseline": {
            "type": "majority_class_classifier",
            "scope": "same_temporal_test_set",
        },
    }
    return model_run, model_eval, promotion_payload


def _walk_forward_report(
    *,
    model_run: Mapping[str, object],
    model_eval: Mapping[str, object],
    metadata: Mapping[str, object],
    policy: PromotionPolicy,
) -> dict[str, object]:
    walk_forward = _mapping(_mapping(model_run.get("metrics")).get("walk_forward"))
    baseline = _mapping(_mapping(model_eval.get("metrics")).get("majority_baseline"))
    baseline_accuracy = float(baseline.get("accuracy", 0.0) or 0.0)
    mean_accuracy = float(walk_forward.get("mean_accuracy", 0.0) or 0.0)
    window_count = int(float(walk_forward.get("window_count", 0.0) or 0.0))
    lift = mean_accuracy - baseline_accuracy
    return {
        "schema_version": SCHEMA_VERSION,
        "approved_dataset": dict(metadata),
        "summary": {
            "window_count": window_count,
            "mean_accuracy": mean_accuracy,
            "baseline_accuracy": baseline_accuracy,
            "accuracy_lift": lift,
            "min_accuracy_lift": policy.min_accuracy_lift,
            "robust_lift": window_count > 0 and lift >= policy.min_accuracy_lift,
        },
        "windows": list(walk_forward.get("windows", [])) if isinstance(walk_forward.get("windows"), list) else [],
    }


def _regime_slices_report(
    *,
    feature_records: list[dict[str, object]],
    metadata: Mapping[str, object],
) -> dict[str, object]:
    slices: dict[str, dict[str, object]] = {}
    for row in feature_records:
        year = str(row.get("timestamp", ""))[:4] or "unknown"
        vol = _float_or_none(row.get("realized_volatility_20"))
        regime = "unknown_vol"
        if vol is not None:
            regime = "high_vol" if vol >= 0.25 else "normal_vol"
        for name in (f"year:{year}", f"volatility:{regime}"):
            item = slices.setdefault(name, {"name": name, "row_count": 0, "symbols": set()})
            item["row_count"] = int(item["row_count"]) + 1
            if row.get("symbol"):
                item["symbols"].add(str(row["symbol"]).upper())
    normalized = []
    for item in slices.values():
        normalized.append(
            {
                "name": item["name"],
                "row_count": item["row_count"],
                "symbols": sorted(item["symbols"]),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "approved_dataset": dict(metadata),
        "summary": {"slice_count": len(normalized)},
        "slices": sorted(normalized, key=lambda item: str(item["name"])),
    }


def _apply_challenger_robustness(
    *,
    promotion_decision: Mapping[str, object],
    backtest: BacktestResult,
    walk_forward_report: Mapping[str, object],
    regime_slices: Mapping[str, object],
    feature_records: list[dict[str, object]],
    policy: PromotionPolicy,
) -> dict[str, object]:
    payload = dict(promotion_decision)
    reasons = _dedupe_strings(payload.get("reasons", []))
    actions = _dedupe_strings(payload.get("actions", []))
    costs = _costs_payload(backtest)
    robustness = _robustness_payload(walk_forward_report, regime_slices, backtest)
    reasons.extend(_temporal_leakage_reasons(feature_records))
    trade_count = float(backtest.metrics.get("trade_count", 0.0) or 0.0)
    if trade_count < 1:
        reasons.append("insufficient_trade_count")
    if not _mapping(walk_forward_report.get("summary")).get("robust_lift"):
        if float(policy.min_accuracy_lift) >= 0:
            reasons.append("walk_forward_lift_not_robust")
    if float(backtest.metrics.get("max_drawdown", 0.0) or 0.0) > 0.50:
        reasons.append("max_drawdown_excessive")
    if costs["net_cagr_after_estimated_costs"] is not None and costs["net_cagr_after_estimated_costs"] < 0:
        reasons.append("costs_slippage_turn_candidate_negative")
    reasons = _dedupe_strings(reasons)
    if reasons:
        payload["eligible_for_paper_challenger"] = False
        payload["approved"] = False
        payload["reasons"] = reasons
        payload["actions"] = _dedupe_strings([*actions, "keep_current_champion"])
    else:
        payload["reasons"] = []
        payload["actions"] = actions
    payload["costs"] = costs
    payload["robustness"] = robustness
    payload["authority"] = {
        "mutates_latest_model": False,
        "automatic_champion_replacement": False,
        "mlflow_paper_candidate_is_authority": False,
    }
    return payload


def _costs_payload(backtest: BacktestResult) -> dict[str, object]:
    cagr = _float_or_none(backtest.metrics.get("cagr"))
    estimated_costs = _float_or_none(backtest.metrics.get("estimated_costs")) or 0.0
    return {
        "turnover": backtest.metrics.get("turnover", 0.0),
        "estimated_costs": estimated_costs,
        "slippage": {"source": "backtest_estimated_costs", "explicit": True},
        "net_cagr_after_estimated_costs": cagr - estimated_costs if cagr is not None else None,
    }


def _robustness_payload(
    walk_forward_report: Mapping[str, object],
    regime_slices: Mapping[str, object],
    backtest: BacktestResult,
) -> dict[str, object]:
    return {
        "walk_forward": dict(_mapping(walk_forward_report.get("summary"))),
        "regime_slices": dict(_mapping(regime_slices.get("summary"))),
        "backtest": {
            "trade_count": backtest.metrics.get("trade_count", 0.0),
            "max_drawdown": backtest.metrics.get("max_drawdown", 0.0),
        },
    }


def _temporal_leakage_reasons(feature_records: list[dict[str, object]]) -> list[str]:
    suspicious_prefixes = ("future_", "lead_", "lookahead_", "target")
    suspicious_fragments = ("future", "lookahead", "next_return", "next_close")
    for row in feature_records:
        for key in row.keys():
            normalized = str(key).lower()
            if normalized.startswith(suspicious_prefixes) or any(fragment in normalized for fragment in suspicious_fragments):
                return ["temporal_leakage_detected"]
    return []


def _majority_classifier_metrics(examples: Iterable[object]) -> dict[str, float]:
    rows = tuple(examples)
    if not rows:
        return _empty_classifier_metrics()
    positive_rate = sum(int(row.target) for row in rows) / len(rows)
    majority_class = int(positive_rate >= 0.5)
    probability = min(max(positive_rate, 1e-12), 1.0 - 1e-12)
    correct = sum(1 for row in rows if int(row.target) == majority_class)
    log_loss = 0.0
    for row in rows:
        log_loss += -(row.target * math.log(probability) + (1 - row.target) * math.log(1 - probability))
    return {
        "sample_count": float(len(rows)),
        "accuracy": correct / len(rows),
        "log_loss": log_loss / len(rows),
        "positive_rate": positive_rate,
    }


def _empty_classifier_metrics() -> dict[str, float]:
    return {"sample_count": 0.0, "accuracy": 0.0, "log_loss": 0.0, "positive_rate": 0.0}


def _default_feature_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    first = records[0] if records else {}
    candidates = (
        "momentum_20",
        "momentum_2",
        "realized_volatility_20",
        "realized_volatility_3",
        "relative_volume_20",
        "relative_volume_2",
    )
    names = tuple(name for name in candidates if name in first)
    if not names:
        raise ValueError("dataset does not contain supported feature columns")
    return names


def _with_metadata(result: BacktestResult, metadata: Mapping[str, object]) -> BacktestResult:
    return BacktestResult(
        config=result.config,
        daily_returns=result.daily_returns,
        equity_curve=result.equity_curve,
        positions=result.positions,
        trades=result.trades,
        metrics=result.metrics,
        metadata=dict(metadata),
    )


def _build_blocked_summary(
    *,
    status: str,
    reasons: list[str],
    metadata: Mapping[str, object],
    data_quality: Mapping[str, object],
    artifact_paths: Mapping[str, Path],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "eligible_for_paper_challenger": False,
        "reasons": reasons,
        "approved_dataset": dict(metadata),
        "data_quality": data_quality,
        "metrics": {},
        "artifacts": _artifact_index(artifact_paths),
    }


def _build_completed_summary(
    *,
    status: str,
    reasons: list[str],
    metadata: Mapping[str, object],
    data_quality: Mapping[str, object],
    backtest: BacktestResult,
    model_eval: Mapping[str, object],
    promotion_decision: Mapping[str, object],
    artifact_paths: Mapping[str, Path],
) -> dict[str, object]:
    test_metrics = _mapping(_mapping(model_eval.get("metrics")).get("test"))
    baseline_metrics = _mapping(_mapping(model_eval.get("metrics")).get("majority_baseline"))
    metrics = {
        "cagr": backtest.metrics.get("cagr", 0.0),
        "sharpe": backtest.metrics.get("sharpe", 0.0),
        "sortino": backtest.metrics.get("sortino", 0.0),
        "max_drawdown": backtest.metrics.get("max_drawdown", 0.0),
        "turnover": backtest.metrics.get("turnover", 0.0),
        "estimated_costs": backtest.metrics.get("estimated_costs", 0.0),
        "trade_count": backtest.metrics.get("trade_count", 0.0),
        "accuracy": test_metrics.get("accuracy", 0.0),
        "baseline_accuracy": baseline_metrics.get("accuracy", 0.0),
        "accuracy_lift": promotion_decision.get("accuracy_lift", 0.0),
        "log_loss": test_metrics.get("log_loss", 0.0),
        "sample_count": test_metrics.get("sample_count", 0.0),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "eligible_for_paper_challenger": bool(promotion_decision.get("eligible_for_paper_challenger")),
        "reasons": reasons,
        "approved_dataset": dict(metadata),
        "data_quality": data_quality,
        "metrics": metrics,
        "artifacts": _artifact_index(artifact_paths),
    }


def _artifact_index(paths: Mapping[str, Path]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "path": str(path),
            "sha256": _file_sha256(path),
        }
        for name, path in paths.items()
        if path.exists()
    }


def _resolve_periods_per_year(value: str | int, frequency: str) -> int:
    if value == "auto":
        return AUTO_PERIODS_PER_YEAR[frequency]
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ApprovedEvaluationOperationalError(f"invalid periods_per_year: {value}") from exc
    if resolved <= 0:
        raise ApprovedEvaluationOperationalError("periods_per_year must be positive")
    return resolved


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ApprovedEvaluationOperationalError(f"invalid {field_name}: {value}") from exc


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _dedupe_strings(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in {None, ""}:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _float_or_none(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _format_summary_value(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) < 10 and not number.is_integer():
        return f"{number:.6f}"
    return f"{number:.2f}"


def _write_json(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_text(payload: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
