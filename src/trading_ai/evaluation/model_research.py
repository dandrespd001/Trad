"""Offline logistic model research for governed approved datasets."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path

from trading_ai.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    run_momentum_vol_target_backtest,
    run_signal_policy_backtest,
)
from trading_ai.config import ConfigError, UniverseConfig, load_risk_config, load_universe_config, load_yaml_file
from trading_ai.data.io import read_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.evaluation.model_quality import (
    QUALITY_MODE_TRADING_FIRST,
    ModelQualityPolicy,
    load_model_quality_policy,
    quality_policy_payload,
    trading_gate_payload,
)
from trading_ai.features.engineering import FeatureConfig, build_features, default_model_feature_names
from trading_ai.models.baseline import (
    LogisticBaselineConfig,
    LogisticBaselineModel,
    SupervisedExample,
    build_supervised_examples,
    evaluate_classifier,
    temporal_train_test_split,
    train_logistic_baseline,
)
from trading_ai.models.promotion import (
    EconomicPromotionPolicy,
    PromotionPolicy,
    evaluate_economic_promotion,
    evaluate_promotion,
    rank_economic_candidates,
)
from trading_ai.risk.policy import RiskLimits

SCHEMA_VERSION = 1
STATUS_CANDIDATE_READY = "CANDIDATE_READY"
STATUS_NO_CANDIDATE_READY = "NO_CANDIDATE_READY"
AUTO_PERIODS_PER_YEAR = {"1d": 252, "1h": 1638}
REQUIRED_APPROVED_FILES = ("ohlcv.parquet", "manifest.json", "catalog_entry.json")


class ModelResearchOperationalError(RuntimeError):
    """Raised for operational failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class CandidateTrainingSpec:
    candidate_id: str
    feature_names: tuple[str, ...]
    preprocessing: Mapping[str, object]
    training_config: Mapping[str, object]


@dataclass(frozen=True)
class CandidateTrainingResult:
    model: LogisticBaselineModel
    standardized_model: LogisticBaselineModel
    preprocessing: dict[str, object]

    def transform_raw_features(self, features: tuple[float, ...]) -> tuple[float, ...]:
        if self.preprocessing.get("type") != "standardize":
            return features
        means = tuple(_float(value) for value in _object_sequence(self.preprocessing.get("means", [])))
        scales = tuple(_float(value) for value in _object_sequence(self.preprocessing.get("scales", [])))
        return tuple((value - mean) / scale for value, mean, scale in zip(features, means, scales, strict=True))


@dataclass(frozen=True)
class ModelResearchSweepResult:
    exit_code: int
    status: str
    output_dir: Path
    report_path: Path
    markdown_path: Path
    candidate_specs_path: Path
    best_candidate_spec_path: Path | None
    deployment_model_path: Path | None


def run_model_research_sweep(
    *,
    approved_dir: str | Path,
    start: str,
    end: str,
    as_of_date: str | date,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    output_dir: str | Path = "reports/tmp/model_research",
    min_accuracy_lift: float = 0.02,
    min_test_samples: int = 30,
) -> ModelResearchSweepResult:
    """Run a brokerless sweep over LogisticBaseline-compatible candidates."""

    resolved_as_of_date = _parse_date(as_of_date, "as_of_date").isoformat()
    resolved_start = _parse_date(start, "from").isoformat()
    resolved_end = _parse_date(end, "to").isoformat()
    if resolved_end < resolved_start:
        raise ModelResearchOperationalError("--to must be on or after --from")

    approved_path = Path(approved_dir)
    paths = _approved_paths(approved_path)
    manifest = _read_json(paths["manifest"])
    catalog_entry = _read_json(paths["catalog_entry"])
    metadata = _approved_metadata(manifest, catalog_entry, approved_dir=approved_path)
    approved_as_of_date = str(metadata.get("as_of_date") or "")
    if not approved_as_of_date:
        raise ModelResearchOperationalError("missing_approved_dataset_as_of_date")
    if approved_as_of_date != resolved_as_of_date:
        raise ModelResearchOperationalError(
            "approved dataset as_of_date mismatch: "
            f"requested={resolved_as_of_date} approved={approved_as_of_date} "
            f"(approved_dataset_as_of_date_mismatch:{resolved_as_of_date}:{approved_as_of_date})"
        )

    universe = load_universe_config(config)
    risk_limits = load_risk_config(risk)
    quality_policy = load_model_quality_policy(risk)
    economic_policy = _economic_policy_for_frequency(quality_policy, str(metadata["frequency"]))
    dataset_id = str(metadata["dataset_id"])
    frequency = str(metadata["frequency"])
    periods_per_year = AUTO_PERIODS_PER_YEAR.get(frequency, 252)
    cost_bps, slippage_bps = _backtest_costs(risk)
    run_dir = Path(output_dir) / dataset_id / frequency / resolved_as_of_date
    run_dir.mkdir(parents=True, exist_ok=True)

    records = read_records(paths["dataset"])
    actual_manifest = build_dataset_manifest(records, source=str(paths["dataset"]))
    if actual_manifest["dataset_hash"] != metadata["dataset_hash"]:
        raise ModelResearchOperationalError(
            "approved dataset hash mismatch: "
            f"manifest={metadata['dataset_hash']} actual={actual_manifest['dataset_hash']}"
        )
    validation = validate_ohlcv_records(records, allowed_symbols=universe.symbols)
    if not validation.valid:
        raise ModelResearchOperationalError("approved dataset validation failed: " + ", ".join(validation.errors))
    window_records = _filter_records_by_date(records, start=resolved_start, end=resolved_end)
    if not window_records:
        raise ModelResearchOperationalError("approved dataset has no records inside requested --from/--to window")
    universe_eligibility = _universe_eligibility(universe, window_records, frequency=frequency)
    analysis_metadata = dict(metadata)
    analysis_metadata.update(
        {
            "window_start": resolved_start,
            "window_end": resolved_end,
            "window_row_count": len(window_records),
            "window_dataset_hash": build_dataset_manifest(window_records, source=str(paths["dataset"]))["dataset_hash"],
        }
    )

    feature_records = build_features(
        window_records,
        FeatureConfig(periods_per_year=periods_per_year),
    )
    backtest = run_momentum_vol_target_backtest(
        window_records,
        BacktestConfig(
            max_gross_exposure=risk_limits.max_gross_exposure,
            max_single_position=risk_limits.max_single_position,
            periods_per_year=periods_per_year,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        ),
    )
    costs = _costs_payload(backtest.metrics)
    trading_gate = trading_gate_payload(
        backtest_metrics=backtest.metrics,
        costs=costs,
        policy=quality_policy,
    )
    policy = PromotionPolicy(min_accuracy_lift=min_accuracy_lift, min_test_samples=min_test_samples)
    candidates = _evaluate_candidate_specs(
        feature_records=feature_records,
        metadata=analysis_metadata,
        as_of_date=resolved_as_of_date,
        policy=policy,
        quality_policy=quality_policy,
        economic_policy=economic_policy,
        trading_gate=trading_gate,
        risk_limits=risk_limits,
        periods_per_year=periods_per_year,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )
    ranked = rank_economic_candidates(candidates, policy=economic_policy)
    for index, candidate in enumerate(ranked, start=1):
        candidate["rank"] = index

    best = next((candidate for candidate in ranked if candidate.get("ready_for_paper_demo") is True), None)
    status = STATUS_CANDIDATE_READY if best is not None else STATUS_NO_CANDIDATE_READY

    candidate_specs_path = run_dir / "candidate_specs.json"
    report_path = run_dir / "sweep_report.json"
    markdown_path = run_dir / "sweep_report.md"
    best_candidate_spec_output = run_dir / "best_candidate_spec.json"
    deployment_model_output = run_dir / "deployment_model.json"
    best_candidate_spec_path: Path | None = None
    deployment_model_path: Path | None = None

    candidate_specs_payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "approved_dataset": analysis_metadata,
        "universe_eligibility": universe_eligibility,
        "candidates": ranked,
    }
    _write_json(candidate_specs_payload, candidate_specs_path)

    best_spec_payload: dict[str, object] | None = None
    if best is not None:
        best_spec_payload = _governed_spec(
            candidate_id=str(best["candidate_id"]),
            feature_names=_string_tuple(best.get("feature_names")),
            preprocessing=_mapping(best.get("preprocessing")),
            training_config=_mapping(best.get("training_config")),
            metadata=analysis_metadata,
            as_of_date=approved_as_of_date,
        )
        _write_json(best_spec_payload, best_candidate_spec_output)
        best_candidate_spec_path = best_candidate_spec_output
        deployment_payload = dict(_mapping(best.get("model")))
        deployment_payload.update(
            {
                "model_type": "logistic-baseline",
                "candidate_id": best["candidate_id"],
                "source_candidate_spec": str(best_candidate_spec_output),
            }
        )
        _write_json(deployment_payload, deployment_model_output)
        deployment_model_path = deployment_model_output

    report = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "ready_for_paper_demo": best is not None,
        "approved_dataset": analysis_metadata,
        "inputs": {
            "approved_dir": str(approved_dir),
            "from": resolved_start,
            "to": resolved_end,
            "as_of_date": resolved_as_of_date,
            "config": str(config),
            "risk": str(risk),
        },
        "policy": {
            "min_accuracy_lift": policy.min_accuracy_lift,
            "min_test_samples": policy.min_test_samples,
        },
        "quality_policy": quality_policy_payload(quality_policy),
        "universe_eligibility": universe_eligibility,
        "ranking": {
            "selected_by": "economic_gate",
            "primary_metric": economic_policy.primary_metric,
            "frequency": frequency,
        },
        "selected_by": "economic_gate" if best else None,
        "trading_gate": trading_gate,
        "candidate_count": len(ranked),
        "best_candidate_id": best.get("candidate_id") if best else None,
        "artifacts": {
            "candidate_specs": str(candidate_specs_path),
            "best_candidate_spec": str(best_candidate_spec_path) if best_candidate_spec_path else None,
            "deployment_model": str(deployment_model_path) if deployment_model_path else None,
        },
        "authority": _authority_payload(),
        "safety": _safety_payload(),
    }
    _write_json(report, report_path)
    _write_text(render_sweep_report_markdown(report, ranked[:10]), markdown_path)
    return ModelResearchSweepResult(
        exit_code=0 if best is not None else 1,
        status=status,
        output_dir=run_dir,
        report_path=report_path,
        markdown_path=markdown_path,
        candidate_specs_path=candidate_specs_path,
        best_candidate_spec_path=best_candidate_spec_path,
        deployment_model_path=deployment_model_path,
    )


def render_sweep_report_markdown(report: Mapping[str, object], candidates: Sequence[Mapping[str, object]]) -> str:
    quality_policy = _mapping(report.get("quality_policy"))
    lines = [
        "# Model Research Sweep",
        "",
        f"- Status: {report.get('status')}",
        f"- Ready for paper demo: {str(report.get('ready_for_paper_demo')).lower()}",
        f"- Best candidate: {report.get('best_candidate_id') or ''}",
        f"- Quality policy: `{quality_policy.get('mode') or 'classification'}`",
        f"- Ranking: `{_mapping(report.get('ranking')).get('primary_metric') or 'calmar'}`",
        f"- Research-only universe: `{_mapping(report.get('universe_eligibility')).get('research_only')}`",
        "",
        "| Rank | Economic Rank | Candidate | Ready | Calmar | Net Return After Costs "
        "| Max Drawdown | Turnover | Walk-Forward Stability | Accuracy Lift |",
        "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for candidate in candidates:
        metrics = _mapping(candidate.get("metrics"))
        lines.append(
            (
                "| {rank} | {economic_rank} | `{candidate}` | `{ready}` | {calmar:.6f} | "
                "{net_return:.6f} | {drawdown:.6f} | {turnover:.6f} | {stability:.6f} | {lift:.6f} |"
            ).format(
                rank=candidate.get("rank", ""),
                economic_rank=candidate.get("economic_rank", ""),
                candidate=candidate.get("candidate_id", ""),
                ready=candidate.get("ready_for_paper_demo"),
                calmar=_float(candidate.get("calmar")),
                net_return=_float(candidate.get("net_return_after_costs")),
                drawdown=_float(candidate.get("max_drawdown")),
                turnover=_float(candidate.get("turnover")),
                stability=_float(candidate.get("walk_forward_stability")),
                lift=_float(metrics.get("accuracy_lift")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def load_candidate_spec(path: str | Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelResearchOperationalError(f"invalid candidate spec JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ModelResearchOperationalError(f"candidate spec must be a JSON object: {path}")
    return payload


def validate_candidate_spec_for_metadata(spec: Mapping[str, object], metadata: Mapping[str, object]) -> None:
    required = (
        "candidate_id",
        "model_type",
        "feature_names",
        "preprocessing",
        "training_config",
        "dataset_hash",
        "source_sha256",
        "as_of_date",
        "authority",
        "safety",
    )
    missing = [field for field in required if field not in spec]
    if missing:
        raise ModelResearchOperationalError("candidate spec missing required field(s): " + ", ".join(missing))
    if str(spec.get("model_type")) != "logistic-baseline":
        raise ModelResearchOperationalError("candidate spec model_type must be logistic-baseline")
    if str(spec.get("dataset_hash")) != str(metadata.get("dataset_hash")):
        raise ModelResearchOperationalError(
            "candidate spec dataset_hash mismatch: "
            f"spec={spec.get('dataset_hash')} approved={metadata.get('dataset_hash')}"
        )
    if str(spec.get("source_sha256")) != str(metadata.get("source_sha256")):
        raise ModelResearchOperationalError(
            "candidate spec source_sha256 mismatch: "
            f"spec={spec.get('source_sha256')} approved={metadata.get('source_sha256')}"
        )
    if str(spec.get("as_of_date")) != str(metadata.get("as_of_date")):
        raise ModelResearchOperationalError(
            f"candidate spec as_of_date mismatch: spec={spec.get('as_of_date')} approved={metadata.get('as_of_date')}"
        )
    feature_names = spec.get("feature_names")
    if not isinstance(feature_names, list) or not feature_names:
        raise ModelResearchOperationalError("candidate spec feature_names must be a non-empty list")
    if not all(isinstance(name, str) and name.strip() for name in feature_names):
        raise ModelResearchOperationalError("candidate spec feature_names must be non-empty strings")
    authority = _mapping(spec.get("authority"))
    for field in ("mutates_latest_model", "orders_submitted", "broker_client_built", "credentials_read"):
        if authority.get(field) is not False:
            raise ModelResearchOperationalError(f"candidate spec authority.{field} must be false")
    safety = _mapping(spec.get("safety"))
    if safety.get("paper_only") is not True:
        raise ModelResearchOperationalError("candidate spec safety.paper_only must be true")
    for field in ("live_trading_allowed", "futures_forex_execution"):
        if safety.get(field) is not False:
            raise ModelResearchOperationalError(f"candidate spec safety.{field} must be false")
    if safety.get("llm_order_authority") != "none":
        raise ModelResearchOperationalError("candidate spec safety.llm_order_authority must be none")


def candidate_training_spec_from_payload(payload: Mapping[str, object]) -> CandidateTrainingSpec:
    validate_candidate_training_payload(payload)
    return CandidateTrainingSpec(
        candidate_id=str(payload["candidate_id"]),
        feature_names=_string_tuple(payload.get("feature_names")),
        preprocessing=dict(_mapping(payload.get("preprocessing"))),
        training_config=dict(_mapping(payload.get("training_config"))),
    )


def validate_candidate_training_payload(payload: Mapping[str, object]) -> None:
    if payload.get("candidate_id") in {None, ""}:
        raise ModelResearchOperationalError("candidate spec candidate_id is required")
    feature_names = payload.get("feature_names")
    if not isinstance(feature_names, (list, tuple)) or not feature_names:
        raise ModelResearchOperationalError("candidate spec feature_names must be a non-empty list")
    preprocessing = _mapping(payload.get("preprocessing"))
    preprocessing_type = str(preprocessing.get("type") or "none")
    if preprocessing_type not in {"none", "standardize"}:
        raise ModelResearchOperationalError("candidate spec preprocessing.type must be none or standardize")
    training_config = _mapping(payload.get("training_config"))
    _logistic_config(tuple(str(name) for name in feature_names), training_config)


def run_candidate_model_evaluation(
    *,
    feature_records: list[dict[str, object]],
    metadata: Mapping[str, object],
    policy: PromotionPolicy,
    candidate_spec: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    spec = candidate_training_spec_from_payload(candidate_spec)
    feature_manifest = build_dataset_manifest(feature_records, source=str(metadata["dataset_path"]))
    examples = build_supervised_examples(feature_records, feature_names=spec.feature_names)
    common = {
        "schema_version": SCHEMA_VERSION,
        "approved_dataset": dict(metadata),
        "candidate_id": spec.candidate_id,
        "candidate_spec": _public_candidate_spec(candidate_spec),
    }
    if len(examples) < 2:
        return _insufficient_candidate_evaluation(
            common=common,
            feature_dataset_hash=str(feature_manifest["dataset_hash"]),
            feature_names=spec.feature_names,
            policy=policy,
        )

    split = temporal_train_test_split(examples, test_fraction=_test_fraction(spec.training_config))
    result = _train_candidate_from_examples(split.train, spec)
    challenger_train = evaluate_classifier(result.model, split.train)
    challenger_test = evaluate_classifier(result.model, split.test)
    majority_baseline = _majority_classifier_metrics(split.test)
    walk_forward = walk_forward_candidate_evaluate(
        examples,
        spec,
        min_train_size=max(2, len(split.train) // 2),
        test_size=max(1, len(split.test)),
    )
    decision = evaluate_promotion(
        challenger_metrics=challenger_test,
        baseline_metrics=majority_baseline,
        policy=policy,
    )
    model_run = {
        **common,
        "model_type": "logistic-baseline",
        "model": result.model.to_dict(),
        "preprocessing": result.preprocessing,
        "feature_dataset_hash": feature_manifest["dataset_hash"],
        "feature_names": list(spec.feature_names),
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
        "preprocessing": result.preprocessing,
        "feature_dataset_hash": feature_manifest["dataset_hash"],
        "feature_names": list(spec.feature_names),
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


def train_candidate_model(
    *,
    feature_records: list[dict[str, object]],
    spec: CandidateTrainingSpec,
) -> CandidateTrainingResult:
    examples = build_supervised_examples(feature_records, feature_names=spec.feature_names)
    if len(examples) < 2:
        raise ModelResearchOperationalError("at least two supervised examples are required")
    split = temporal_train_test_split(examples, test_fraction=_test_fraction(spec.training_config))
    return _train_candidate_from_examples(split.train, spec)


def walk_forward_candidate_evaluate(
    examples: Iterable[SupervisedExample],
    spec: CandidateTrainingSpec,
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
        result = _train_candidate_from_examples(train_rows, spec)
        windows.append(
            {
                "train_end": train_rows[-1].timestamp,
                "test_start": test_rows[0].timestamp,
                "test_end": test_rows[-1].timestamp,
                "metrics": evaluate_classifier(result.model, test_rows),
            }
        )
        cursor = test_end
    if not windows:
        return {"window_count": 0.0, "mean_accuracy": 0.0, "windows": []}
    return {
        "window_count": float(len(windows)),
        "mean_accuracy": sum(_float(_mapping(window["metrics"]).get("accuracy")) for window in windows) / len(windows),
        "windows": windows,
    }


def _evaluate_candidate_specs(
    *,
    feature_records: list[dict[str, object]],
    metadata: Mapping[str, object],
    as_of_date: str,
    policy: PromotionPolicy,
    quality_policy: ModelQualityPolicy,
    economic_policy: EconomicPromotionPolicy,
    trading_gate: Mapping[str, object],
    risk_limits: RiskLimits,
    periods_per_year: int,
    cost_bps: float,
    slippage_bps: float,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    frequency = str(metadata.get("frequency") or "")
    for spec_payload in _candidate_spec_payloads(
        feature_records,
        metadata=metadata,
        as_of_date=as_of_date,
        frequency=frequency,
    ):
        model_run, model_eval, promotion = run_candidate_model_evaluation(
            feature_records=feature_records,
            metadata=metadata,
            policy=policy,
            candidate_spec=spec_payload,
        )
        walk_forward = _mapping(_mapping(model_run.get("metrics")).get("walk_forward"))
        baseline = _mapping(_mapping(model_eval.get("metrics")).get("majority_baseline"))
        test_metrics = _mapping(_mapping(model_eval.get("metrics")).get("test"))
        baseline_accuracy = _float(baseline.get("accuracy"))
        walk_accuracy = _float(walk_forward.get("mean_accuracy"))
        walk_lift = walk_accuracy - baseline_accuracy
        classification_ready = (
            bool(promotion.get("eligible_for_paper_challenger"))
            and _int(walk_forward.get("window_count", 0.0)) > 0
            and walk_lift >= policy.min_accuracy_lift
        )
        reasons = _dedupe_strings(promotion.get("reasons", []))
        if (
            not classification_ready
            and "walk_forward_lift_not_robust" not in reasons
            and walk_lift < policy.min_accuracy_lift
        ):
            reasons.append("walk_forward_lift_not_robust")
        walk_forward_stability = _walk_forward_stability(
            walk_forward,
            baseline_accuracy=baseline_accuracy,
            min_accuracy_lift=policy.min_accuracy_lift,
        )
        signal_backtest = _candidate_signal_policy_backtest(
            model_run=model_run,
            feature_records=feature_records,
            risk_limits=risk_limits,
            periods_per_year=periods_per_year,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        )
        signal_metrics = signal_backtest.metrics if signal_backtest is not None else _empty_backtest_metrics()
        signal_costs = _costs_payload(signal_metrics)
        candidate_trading_gate = trading_gate_payload(
            backtest_metrics=signal_metrics,
            costs=signal_costs,
            policy=quality_policy,
        )
        economic_metrics = {
            "net_return_after_costs": signal_costs.get("net_cagr_after_estimated_costs", 0.0),
            "max_drawdown": signal_metrics.get("max_drawdown", 0.0),
            "turnover": signal_metrics.get("turnover", 0.0),
            "estimated_costs": signal_metrics.get("estimated_costs", 0.0),
            "trade_count": signal_metrics.get("trade_count", 0.0),
            "walk_forward_stability": walk_forward_stability,
            "walk_forward_window_count": walk_forward.get("window_count", 0.0),
        }
        economic_decision = evaluate_economic_promotion(metrics=economic_metrics, policy=economic_policy)
        if quality_policy.mode == QUALITY_MODE_TRADING_FIRST:
            ready = economic_decision.reviewable and _mapping(candidate_trading_gate).get("status") == "PASS"
            final_reasons = _dedupe_strings(
                [
                    *economic_decision.reasons,
                    *_dedupe_strings(_mapping(candidate_trading_gate).get("reasons", [])),
                ]
            )
        else:
            ready = classification_ready
            final_reasons = reasons
        candidate_metrics = {
            "accuracy": test_metrics.get("accuracy", 0.0),
            "baseline_accuracy": baseline_accuracy,
            "accuracy_lift": promotion.get("accuracy_lift", 0.0),
            "log_loss": test_metrics.get("log_loss", 0.0),
            "sample_count": test_metrics.get("sample_count", 0.0),
            "walk_forward_accuracy": walk_accuracy,
            "walk_forward_accuracy_lift": walk_lift,
            "walk_forward_window_count": walk_forward.get("window_count", 0.0),
            "walk_forward_stability": economic_decision.walk_forward_stability,
            "economic_oos_window_count": economic_decision.walk_forward_window_count,
            "calmar": economic_decision.calmar,
            "net_return_after_costs": economic_decision.net_return_after_costs,
            "max_drawdown": economic_decision.max_drawdown,
            "turnover": economic_decision.turnover,
            "estimated_costs": economic_decision.estimated_costs,
            "trade_count": economic_decision.trade_count,
        }
        candidate = {
            **_public_candidate_spec(spec_payload),
            "status": STATUS_CANDIDATE_READY if ready else STATUS_NO_CANDIDATE_READY,
            "ready_for_paper_demo": ready,
            "reasons": final_reasons,
            "selected_by": "economic_gate" if ready else None,
            "classification_gate": {
                "status": "PASS" if classification_ready else "FAIL",
                "blocking": quality_policy.mode != QUALITY_MODE_TRADING_FIRST,
                "reasons": reasons,
            },
            "trading_gate": dict(candidate_trading_gate),
            "economic_gate": economic_decision.to_dict(),
            "metrics": candidate_metrics,
            "accuracy": candidate_metrics["accuracy"],
            "calmar": candidate_metrics["calmar"],
            "net_return_after_costs": candidate_metrics["net_return_after_costs"],
            "max_drawdown": candidate_metrics["max_drawdown"],
            "turnover": candidate_metrics["turnover"],
            "walk_forward_stability": candidate_metrics["walk_forward_stability"],
            "score": _float(promotion.get("accuracy_lift")) + walk_lift,
            "model": _mapping(model_run.get("model")),
        }
        candidates.append(candidate)
    return candidates


def _costs_payload(metrics: Mapping[str, object]) -> dict[str, object]:
    cagr = _float(metrics.get("cagr"))
    estimated_costs = _float(metrics.get("estimated_costs"))
    return {
        "turnover": metrics.get("turnover", 0.0),
        "estimated_costs": estimated_costs,
        "slippage": {"source": "backtest_estimated_costs", "explicit": True},
        "net_cagr_after_estimated_costs": cagr - estimated_costs,
    }


def _candidate_signal_policy_backtest(
    *,
    model_run: Mapping[str, object],
    feature_records: list[dict[str, object]],
    risk_limits: RiskLimits,
    periods_per_year: int,
    cost_bps: float,
    slippage_bps: float,
) -> BacktestResult | None:
    if str(model_run.get("model_type")) != "logistic-baseline":
        return None
    model_payload = model_run.get("model")
    if not isinstance(model_payload, Mapping):
        return None
    try:
        model = LogisticBaselineModel.from_dict(model_payload)
    except (TypeError, ValueError, KeyError):
        return None
    return run_signal_policy_backtest(
        feature_records,
        model,
        threshold=0.5,
        min_signal_margin=risk_limits.min_signal_margin,
        max_buy_signals=risk_limits.max_buy_signals,
        config=BacktestConfig(
            periods_per_year=periods_per_year,
            cost_bps=cost_bps,
            slippage_bps=slippage_bps,
        ),
    )


def _empty_backtest_metrics() -> dict[str, float]:
    return {
        "cumulative_return": 0.0,
        "cagr": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown": 0.0,
        "turnover": 0.0,
        "trade_count": 0.0,
        "average_exposure": 0.0,
        "estimated_costs": 0.0,
    }


def _walk_forward_stability(
    walk_forward: Mapping[str, object],
    *,
    baseline_accuracy: float,
    min_accuracy_lift: float,
) -> float:
    del min_accuracy_lift
    windows = _object_sequence(walk_forward.get("windows", []))
    if not windows:
        return 0.0
    stable = 0
    for window in windows:
        metrics = _mapping(_mapping(window).get("metrics"))
        accuracy = _float(metrics.get("accuracy"))
        if accuracy >= baseline_accuracy:
            stable += 1
    return stable / len(windows)


def _economic_policy_for_frequency(
    quality_policy: ModelQualityPolicy,
    frequency: str,
) -> EconomicPromotionPolicy:
    intraday = quality_policy.intraday if frequency == "1h" else {}
    return EconomicPromotionPolicy(
        primary_metric=str(intraday.get("primary_metric", quality_policy.primary_metric)).strip().lower() or "calmar",
        min_calmar=_float(intraday.get("min_calmar", quality_policy.min_calmar)),
        min_net_return_after_costs=_float(intraday.get("min_net_return_after_costs", quality_policy.min_net_cagr)),
        max_drawdown_pct=_float(intraday.get("max_drawdown_pct", quality_policy.max_drawdown_pct)),
        max_turnover=_float(intraday.get("max_turnover", quality_policy.max_turnover)),
        max_estimated_costs=_float(intraday.get("max_estimated_costs", quality_policy.max_estimated_costs)),
        min_trade_count=_float(intraday.get("min_trade_count", quality_policy.min_trade_count)),
        min_walk_forward_stability=_float(
            intraday.get("min_walk_forward_stability", quality_policy.min_walk_forward_stability)
        ),
        min_oos_windows=_float(intraday.get("min_oos_windows", quality_policy.min_oos_windows)),
    )


def _backtest_costs(risk: str | Path) -> tuple[float, float]:
    default = BacktestConfig()
    try:
        payload = load_yaml_file(risk)
    except ConfigError:
        return default.cost_bps, default.slippage_bps
    costs = payload.get("costs")
    if not isinstance(costs, Mapping):
        return default.cost_bps, default.slippage_bps
    return (
        _non_negative_cost(costs.get("cost_bps"), default.cost_bps),
        _non_negative_cost(costs.get("slippage_bps"), default.slippage_bps),
    )


def _non_negative_cost(value: object, default: float) -> float:
    if value in {None, ""}:
        return default
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _candidate_spec_payloads(
    feature_records: list[dict[str, object]],
    *,
    metadata: Mapping[str, object],
    as_of_date: str,
    frequency: str,
) -> list[dict[str, object]]:
    available = set(default_model_feature_names(feature_records))
    candidate_sets: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("legacy_3", ("momentum_20", "realized_volatility_20", "relative_volume_20")),
        ("return_momentum_vol", ("return_1d", "momentum_20", "realized_volatility_20")),
        (
            "expanded_default",
            tuple(name for name in default_model_feature_names(feature_records) if not name.endswith("_2")),
        ),
        ("range_volume_sma", ("daily_range", "relative_volume_20", "close_to_sma_20")),
        ("vol_adjusted_momentum", ("return_1d", "vol_adjusted_momentum_20", "rolling_drawdown_20")),
        ("momentum_sma_multi", ("momentum_20", "momentum_60", "close_to_sma_20", "close_to_sma_60")),
        ("short_fixture", ("momentum_2", "realized_volatility_3", "relative_volume_2")),
    )
    if frequency == "1h":
        candidate_sets = (
            *candidate_sets,
            (
                "intraday_momentum_range_regime",
                (
                    "return_1d",
                    "momentum_20",
                    "momentum_60",
                    "realized_volatility_20",
                    "intraday_range",
                    "relative_volume_20",
                    "trend_regime_20",
                ),
            ),
            (
                "intraday_range_volume_regime",
                ("intraday_range", "relative_volume_20", "close_to_sma_20", "trend_regime_20"),
            ),
        )
    payloads: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    ml_backends = _ml_backend_availability()
    for family, names in candidate_sets:
        feature_names = tuple(name for name in names if name in available)
        if not feature_names:
            continue
        for preprocessing_type in ("none", "standardize"):
            key = (preprocessing_type, feature_names)
            if key in seen:
                continue
            seen.add(key)
            payloads.append(
                _governed_spec(
                    candidate_id=f"logreg_{preprocessing_type}_{family}",
                    feature_names=feature_names,
                    preprocessing={"type": preprocessing_type},
                    training_config={
                        "backend": _preferred_backend(ml_backends),
                        "ml_backends_available": ml_backends,
                        "frequency": frequency,
                        "learning_rate": 0.2,
                        "epochs": 200,
                        "l2": 0.001,
                        "test_fraction": 0.25,
                    },
                    metadata=metadata,
                    as_of_date=as_of_date,
                )
            )
    return payloads


def _ml_backend_availability() -> dict[str, bool]:
    return {
        "sklearn": find_spec("sklearn") is not None,
        "lightgbm": find_spec("lightgbm") is not None,
        "xgboost": find_spec("xgboost") is not None,
    }


def _preferred_backend(backends: Mapping[str, bool]) -> str:
    if backends.get("lightgbm"):
        return "lightgbm"
    if backends.get("xgboost"):
        return "xgboost"
    if backends.get("sklearn"):
        return "sklearn_logistic"
    return "pure_python_logistic"



def _governed_spec(
    *,
    candidate_id: str,
    feature_names: tuple[str, ...],
    preprocessing: Mapping[str, object],
    training_config: Mapping[str, object],
    metadata: Mapping[str, object],
    as_of_date: str,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "model_type": "logistic-baseline",
        "feature_names": list(feature_names),
        "preprocessing": dict(preprocessing),
        "training_config": dict(training_config),
        "dataset_hash": str(metadata["dataset_hash"]),
        "source_sha256": str(metadata["source_sha256"]),
        "as_of_date": as_of_date,
        "authority": _authority_payload(),
        "safety": _safety_payload(),
    }


def _train_candidate_from_examples(
    train_examples: Iterable[SupervisedExample],
    spec: CandidateTrainingSpec,
) -> CandidateTrainingResult:
    rows = tuple(train_examples)
    if not rows:
        raise ModelResearchOperationalError("at least one training example is required")
    config = _logistic_config(spec.feature_names, spec.training_config)
    preprocessing_type = str(spec.preprocessing.get("type") or "none")
    if preprocessing_type == "standardize":
        means, scales = _fit_standardization(rows)
        transformed_rows = _transform_examples(rows, means=means, scales=scales)
        standardized_model = train_logistic_baseline(transformed_rows, config)
        raw_model = _standardized_model_to_raw(standardized_model, means=means, scales=scales)
        return CandidateTrainingResult(
            model=raw_model,
            standardized_model=standardized_model,
            preprocessing={
                "type": "standardize",
                "means": list(means),
                "scales": list(scales),
                "exports_coefficients": "raw_features",
            },
        )
    model = train_logistic_baseline(rows, config)
    return CandidateTrainingResult(
        model=model,
        standardized_model=model,
        preprocessing={"type": "none", "exports_coefficients": "raw_features"},
    )


def _standardized_model_to_raw(
    model: LogisticBaselineModel,
    *,
    means: tuple[float, ...],
    scales: tuple[float, ...],
) -> LogisticBaselineModel:
    coefficients = tuple(weight / scale for weight, scale in zip(model.coefficients, scales, strict=True))
    intercept = model.intercept - sum(
        weight * mean / scale for weight, mean, scale in zip(model.coefficients, means, scales, strict=True)
    )
    return LogisticBaselineModel(
        feature_names=model.feature_names,
        intercept=intercept,
        coefficients=coefficients,
    )


def _fit_standardization(examples: tuple[SupervisedExample, ...]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    width = len(examples[0].features)
    means: list[float] = []
    scales: list[float] = []
    for index in range(width):
        values = [row.features[index] for row in examples]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        scale = math.sqrt(variance)
        means.append(mean)
        scales.append(scale if scale > 1e-12 else 1.0)
    return tuple(means), tuple(scales)


def _transform_examples(
    examples: Iterable[SupervisedExample],
    *,
    means: tuple[float, ...],
    scales: tuple[float, ...],
) -> tuple[SupervisedExample, ...]:
    return tuple(
        SupervisedExample(
            timestamp=row.timestamp,
            symbol=row.symbol,
            features=tuple(
                (value - mean) / scale for value, mean, scale in zip(row.features, means, scales, strict=True)
            ),
            target=row.target,
        )
        for row in examples
    )


def _logistic_config(feature_names: tuple[str, ...], training_config: Mapping[str, object]) -> LogisticBaselineConfig:
    return LogisticBaselineConfig(
        feature_names=feature_names,
        learning_rate=_float(training_config.get("learning_rate", 0.2)),
        epochs=_int(training_config.get("epochs", 200)),
        l2=_float(training_config.get("l2", 0.001)),
        test_fraction=_test_fraction(training_config),
    )


def _test_fraction(training_config: Mapping[str, object]) -> float:
    return _float(training_config.get("test_fraction", 0.25))


def _insufficient_candidate_evaluation(
    *,
    common: Mapping[str, object],
    feature_dataset_hash: str,
    feature_names: tuple[str, ...],
    policy: PromotionPolicy,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    empty = _empty_classifier_metrics()
    decision = evaluate_promotion(challenger_metrics=empty, baseline_metrics=empty, policy=policy)
    reasons = _dedupe_strings([*decision.reasons, "insufficient_supervised_examples"])
    model_run = {
        **dict(common),
        "model_type": "logistic-baseline",
        "status": "SKIPPED",
        "reasons": reasons,
        "model": None,
        "feature_dataset_hash": feature_dataset_hash,
        "feature_names": list(feature_names),
        "metrics": {
            "train": empty,
            "test": empty,
            "walk_forward": {"window_count": 0.0, "mean_accuracy": 0.0, "windows": []},
        },
    }
    model_eval = {
        **dict(common),
        "model_type": "logistic-baseline",
        "status": "SKIPPED",
        "reasons": reasons,
        "feature_dataset_hash": feature_dataset_hash,
        "feature_names": list(feature_names),
        "metrics": {"train": empty, "test": empty, "majority_baseline": empty},
        "baseline": {"type": "majority_class_classifier", "scope": "same_temporal_test_set", "metrics": empty},
    }
    promotion = {
        **dict(common),
        **decision.to_dict(),
        "reasons": reasons,
        "eligible_for_paper_challenger": False,
        "policy": {"min_accuracy_lift": policy.min_accuracy_lift, "min_test_samples": policy.min_test_samples},
        "baseline": {"type": "majority_class_classifier", "scope": "same_temporal_test_set"},
    }
    return model_run, model_eval, promotion


def _majority_classifier_metrics(examples: Iterable[SupervisedExample]) -> dict[str, float]:
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


def _public_candidate_spec(spec: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_id": spec.get("candidate_id"),
        "model_type": spec.get("model_type"),
        "feature_names": list(_string_tuple(spec.get("feature_names"))),
        "preprocessing": dict(_mapping(spec.get("preprocessing"))),
        "training_config": dict(_mapping(spec.get("training_config"))),
        "dataset_hash": spec.get("dataset_hash"),
        "source_sha256": spec.get("source_sha256"),
        "as_of_date": spec.get("as_of_date"),
        "authority": dict(_mapping(spec.get("authority"))),
        "safety": dict(_mapping(spec.get("safety"))),
    }


def _authority_payload() -> dict[str, bool]:
    return {
        "mutates_latest_model": False,
        "orders_submitted": False,
        "broker_client_built": False,
        "credentials_read": False,
    }


def _safety_payload() -> dict[str, object]:
    return {
        "paper_only": True,
        "live_trading_allowed": False,
        "futures_forex_execution": False,
        "llm_order_authority": "none",
    }


def _approved_paths(approved_dir: Path) -> dict[str, Path]:
    missing = [name for name in REQUIRED_APPROVED_FILES if not (approved_dir / name).exists()]
    if missing:
        raise ModelResearchOperationalError(
            "approved dataset package is missing required file(s): " + ", ".join(missing)
        )
    return {
        "dataset": approved_dir / "ohlcv.parquet",
        "manifest": approved_dir / "manifest.json",
        "catalog_entry": approved_dir / "catalog_entry.json",
    }


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelResearchOperationalError(f"invalid approved dataset JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ModelResearchOperationalError(f"approved dataset JSON must be an object: {path}")
    return payload


def _approved_metadata(
    manifest: Mapping[str, object],
    catalog_entry: Mapping[str, object],
    *,
    approved_dir: Path,
) -> dict[str, object]:
    for field in ("dataset_id", "frequency", "dataset_hash", "source_sha256"):
        if field not in manifest:
            raise ModelResearchOperationalError(f"manifest missing required field: {field}")
    for field in ("dataset_id", "frequency", "dataset_hash"):
        if str(catalog_entry.get(field)) != str(manifest[field]):
            raise ModelResearchOperationalError(f"catalog entry does not match manifest field: {field}")
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": str(manifest["dataset_id"]),
        "frequency": str(manifest["frequency"]),
        "dataset_hash": str(manifest["dataset_hash"]),
        "source_sha256": str(manifest["source_sha256"]),
        "as_of_date": str(manifest.get("as_of_date") or catalog_entry.get("as_of_date") or ""),
        "start": manifest.get("start"),
        "end": manifest.get("end"),
        "symbols": [symbol.upper() for symbol in _string_tuple(manifest.get("symbols", []))],
        "row_count": _int(manifest.get("row_count", 0)),
        "columns": list(_string_tuple(manifest.get("columns", []))),
        "approved_dir": str(approved_dir),
        "dataset_path": str(approved_dir / "ohlcv.parquet"),
    }


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ModelResearchOperationalError(f"invalid {field_name}: {value}") from exc


def _filter_records_by_date(
    records: Iterable[dict[str, object]],
    *,
    start: str,
    end: str,
) -> list[dict[str, object]]:
    start_date = _parse_date(start, "from")
    end_date = _parse_date(end, "to")
    filtered: list[dict[str, object]] = []
    for record in records:
        timestamp = str(record.get("timestamp") or "")
        record_date = _parse_date(timestamp[:10], "timestamp")
        if start_date <= record_date <= end_date:
            filtered.append(record)
    return filtered


def _universe_eligibility(
    universe: UniverseConfig,
    records: Sequence[Mapping[str, object]],
    *,
    frequency: str,
) -> dict[str, object]:
    requested = list(universe.symbols)
    row_count_by_symbol = {symbol: 0 for symbol in requested}
    dollar_volume_sum_by_symbol = {symbol: 0.0 for symbol in requested}
    timestamp_errors: list[str] = []

    for index, record in enumerate(records):
        symbol = str(record.get("symbol") or "").upper()
        if symbol not in row_count_by_symbol:
            continue
        row_count_by_symbol[symbol] += 1
        dollar_volume_sum_by_symbol[symbol] += max(0.0, _float(record.get("close"))) * max(
            0.0, _float(record.get("volume"))
        )
        if not _timestamp_matches_frequency(record.get("timestamp"), frequency=frequency):
            timestamp_errors.append(f"row {index} timestamp incompatible with {frequency}: {record.get('timestamp')}")

    average_dollar_volume_by_symbol = {
        symbol: (
            dollar_volume_sum_by_symbol[symbol] / row_count_by_symbol[symbol]
            if row_count_by_symbol[symbol] > 0
            else 0.0
        )
        for symbol in requested
    }
    symbols_covered = [symbol for symbol in requested if row_count_by_symbol[symbol] > 0]
    symbols_excluded = [
        {"symbol": symbol, "reasons": ["missing_window_rows"]}
        for symbol in requested
        if row_count_by_symbol[symbol] == 0
    ]
    return {
        "universe_name": universe.name,
        "research_only": universe.research_only,
        "paper_allowed": universe.paper_allowed,
        "live_allowed": universe.live_allowed,
        "frequency": frequency,
        "symbols_requested": requested,
        "symbols_covered": symbols_covered,
        "symbols_excluded": symbols_excluded,
        "row_count_by_symbol": row_count_by_symbol,
        "average_dollar_volume_by_symbol": average_dollar_volume_by_symbol,
        "timestamps_valid": not timestamp_errors,
        "timestamp_errors": timestamp_errors[:20],
    }


def _timestamp_matches_frequency(value: object, *, frequency: str) -> bool:
    if value in {None, ""}:
        return False
    timestamp = str(value)
    try:
        if timestamp.endswith("Z"):
            timestamp = f"{timestamp[:-1]}+00:00"
        resolved = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if frequency == "1d":
        return not any((resolved.hour, resolved.minute, resolved.second, resolved.microsecond))
    if frequency == "1h":
        return resolved.minute == 0 and resolved.second == 0 and resolved.microsecond == 0
    return True


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _object_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return ()


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in _object_sequence(value))


def _dedupe_strings(values: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in _object_sequence(values):
        if value in {None, ""}:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _write_json(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_text(payload: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
