"""Evidence-gated activation of extended technical indicators (governance metadata; never executes orders).

``features/engineering.py`` ships RSI-14, MACD and Bollinger %B but they are
disabled by default (``rsi_window=0`` etc.) so the baseline model keeps its
current behaviour. This module answers, with evidence rather than opinion,
whether turning them on actually improves the risk-adjusted, cost-net score
of the trading model benchmark -- and only then recommends activation.

The comparison's sensitivity depends on the extended side fielding a
candidate that actually *uses* the extended indicators together with the
default model features: ``DEFAULT_MODEL_FEATURE_CANDIDATES`` deliberately
excludes ``rsi_14``/``macd_hist``/``bb_pct_b``, so ``_side_candidates``
injects a ``logreg_default_plus_extended`` union candidate on the extended
side only. Without it, the champion model could win both sides with an
identical score and "baseline" would win by construction, never by
evidence.

The report never trains the production model, never mutates
``models/latest_model.json`` and never submits an order; it produces a
report-only artifact plus a re-verifiable ``artifact_hash`` (the same idiom
as ``paper_signal_approval.compute_plan_hash``: ``generated_at`` and
``artifact_hash`` itself are excluded from the hashed body so re-running the
report over identical inputs yields an identical hash).

Consumption is strictly opt-in: ``load_indicator_activation`` and
``feature_config_from_activation`` fail closed to the baseline
``FeatureConfig()`` on any missing, corrupt, tampered or stale artifact, so a
caller that never asks for activation metadata sees no behaviour change at
all.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.backtest.engine import BacktestConfig
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.evaluation.trading_model_benchmark import (
    _available_features,
    _evaluate_candidate,
    build_benchmark_candidates,
)
from trading_ai.execution.paper_common import (
    as_of_date_to_date,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.features.engineering import (
    DEFAULT_MODEL_FEATURE_CANDIDATES,
    EXTENDED_FEATURE_CANDIDATES,
    FeatureConfig,
    build_features,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/indicator_activation"
MIN_RELATIVE_MARGIN = 0.05
MAX_ARTIFACT_AGE_DAYS = 7
EXTENDED_FEATURE_CONFIG_FIELDS: dict[str, float | int] = {
    "rsi_window": 14,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bb_window": 20,
    "bb_n_std": 2.0,
}

RECOMMENDATION_EXTENDED = "extended"
RECOMMENDATION_BASELINE = "baseline"

STATUS_OK = "OK"
STATUS_BLOCKED = "BLOCKED"

# Fixed evaluation knobs for the two-sided comparison benchmark run. These are
# deliberately not exposed as CLI flags (the report is a lightweight,
# self-contained comparison, not a full trading-model-benchmark rerun): they
# mirror trading_ai.risk.policy.RiskLimits() and backtest.engine.BacktestConfig()
# defaults so results are directly comparable across runs without requiring a
# universe/risk config file.
_DEFAULT_SIGNAL_MODEL = Path("models/latest_model.json")
_DEFAULT_MIN_SIGNAL_MARGIN = 0.05
_DEFAULT_MAX_BUY_SIGNALS = 3
_DEFAULT_EMBARGO = 1
_DEFAULT_THRESHOLD = 0.5

_ARTIFACT_FILENAME = "activation.json"
_MARKDOWN_FILENAME = "activation.md"


@dataclass(frozen=True)
class ActivationResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def compute_activation_hash(payload: Mapping[str, object]) -> str:
    """Return a stable sha256 hex digest of ``payload``.

    ``generated_at`` and ``artifact_hash`` are excluded so that two
    regenerations of the same logical report (same inputs, same
    recommendation) hash identically, while any other field change changes
    the hash -- the same idiom as
    ``paper_signal_approval.compute_plan_hash``.
    """

    body = {key: value for key, value in payload.items() if key not in {"generated_at", "artifact_hash"}}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_indicator_activation_report(
    *,
    as_of_date: str,
    dataset: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    min_relative_margin: float = MIN_RELATIVE_MARGIN,
    generated_at: str | None = None,
) -> ActivationResult:
    """Compare the trading-model benchmark's best score under the baseline
    ``FeatureConfig()`` vs. the extended technical-indicator config, and
    recommend activation only when the extended side clears
    ``min_relative_margin``. Any data or benchmark error fails closed to
    ``recommendation="baseline"`` with ``status=BLOCKED`` (never raises).
    """

    generated = generated_at or _utc_now()
    run_dir = Path(output_dir) / as_of_date
    output_path = run_dir / _ARTIFACT_FILENAME
    markdown_path = run_dir / _MARKDOWN_FILENAME

    blockers: list[str] = []
    baseline_score: float | None = None
    extended_score: float | None = None
    sources: dict[str, object] = {"dataset": str(Path(dataset))}
    status = STATUS_OK

    try:
        records = read_records(dataset)
        validation = validate_ohlcv_records(records)
        if not validation.valid:
            blockers.extend(f"dataset_invalid:{error}" for error in validation.errors)
            status = STATUS_BLOCKED
        else:
            backtest_config = BacktestConfig()
            baseline_features = build_features(records, FeatureConfig())
            extended_features = build_features(records, FeatureConfig(**EXTENDED_FEATURE_CONFIG_FIELDS))
            baseline_score, baseline_candidate_id = _best_candidate_score(
                baseline_features, backtest_config=backtest_config, extended=False
            )
            extended_score, extended_candidate_id = _best_candidate_score(
                extended_features, backtest_config=backtest_config, extended=True
            )
            sources.update(
                {
                    "baseline_row_count": len(baseline_features),
                    "extended_row_count": len(extended_features),
                    "baseline_candidate_id": baseline_candidate_id,
                    "extended_candidate_id": extended_candidate_id,
                }
            )
            if baseline_score is None:
                blockers.append("baseline_candidate_missing")
            if extended_score is None:
                blockers.append("extended_candidate_missing")
    except Exception as exc:  # noqa: BLE001 - fail closed on any data/benchmark error
        blockers.append(f"benchmark_error:{type(exc).__name__}")
        status = STATUS_BLOCKED

    both_valid = baseline_score is not None and extended_score is not None
    relative_margin_observed: float | None = None
    recommendation = RECOMMENDATION_BASELINE
    if both_valid:
        assert baseline_score is not None
        assert extended_score is not None
        if baseline_score != 0:
            relative_margin_observed = (extended_score - baseline_score) / baseline_score
        required_extended_score = baseline_score * (1.0 + min_relative_margin)
        if extended_score >= required_extended_score:
            recommendation = RECOMMENDATION_EXTENDED

    feature_config_out = dict(EXTENDED_FEATURE_CONFIG_FIELDS) if recommendation == RECOMMENDATION_EXTENDED else {}

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "recommendation": recommendation,
        "baseline_score": baseline_score,
        "extended_score": extended_score,
        "relative_margin_observed": relative_margin_observed,
        "min_relative_margin": min_relative_margin,
        "feature_config": feature_config_out,
        "blockers": _dedupe(blockers),
        "sources": sources,
        "safety": _safety(),
    }
    payload["artifact_hash"] = compute_activation_hash(payload)

    write_json_artifact(payload, output_path)
    write_text_artifact(_render_markdown(payload), markdown_path)

    exit_code = 0 if status == STATUS_OK else 1
    return ActivationResult(exit_code, status, output_path, payload)


def load_indicator_activation(
    *,
    as_of_date: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    max_age_days: int = MAX_ARTIFACT_AGE_DAYS,
) -> dict[str, object]:
    """Load the freshest activation report within ``max_age_days`` of
    ``as_of_date``, verifying its ``artifact_hash``. Fails closed to
    ``{"recommendation": "baseline", "feature_config": {}, "fail_closed":
    True, "reason": <code>}`` on any missing, corrupt, tampered or stale
    artifact -- never raises.
    """

    target_date = as_of_date_to_date(as_of_date)
    base = Path(output_dir)
    if not base.exists() or not base.is_dir():
        return _fail_closed("artifact_missing")

    all_candidates: list[tuple[Any, Path]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            candidate_date = as_of_date_to_date(child.name)
        except ValueError:
            continue
        artifact_path = child / _ARTIFACT_FILENAME
        if artifact_path.exists():
            all_candidates.append((candidate_date, artifact_path))

    if not all_candidates:
        return _fail_closed("artifact_missing")

    within_window = [
        (candidate_date, path)
        for candidate_date, path in all_candidates
        if abs((target_date - candidate_date).days) <= max_age_days
    ]
    if not within_window:
        return _fail_closed("artifact_stale")

    within_window.sort(key=lambda item: item[0], reverse=True)
    _, artifact_path = within_window[0]

    try:
        payload = read_json_artifact(artifact_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return _fail_closed("artifact_corrupt")

    stored_hash = payload.get("artifact_hash")
    if not isinstance(stored_hash, str) or not stored_hash:
        return _fail_closed("artifact_hash_missing")
    if compute_activation_hash(payload) != stored_hash:
        return _fail_closed("artifact_hash_mismatch")

    recommendation = str(payload.get("recommendation") or "")
    if recommendation not in {RECOMMENDATION_EXTENDED, RECOMMENDATION_BASELINE}:
        return _fail_closed("artifact_invalid_recommendation")

    feature_config = payload.get("feature_config")
    if not isinstance(feature_config, Mapping):
        feature_config = {}

    return {
        "recommendation": recommendation,
        "feature_config": dict(feature_config),
        "fail_closed": False,
        "as_of_date": str(payload.get("as_of_date") or ""),
        "artifact_path": str(artifact_path),
    }


def feature_config_from_activation(activation_payload: Mapping[str, object]) -> FeatureConfig:
    """Return the ``FeatureConfig`` implied by ``activation_payload``.

    Only ``recommendation == "extended"`` with a ``feature_config`` whose
    fields match ``EXTENDED_FEATURE_CONFIG_FIELDS`` exactly yields the
    extended config; anything else (including a tampered/partial
    ``feature_config``) fails closed to the baseline ``FeatureConfig()``.
    This is a defense-in-depth check independent of
    ``load_indicator_activation``'s hash verification, so a payload
    constructed by hand (bypassing the loader) cannot smuggle in
    unauthorized indicator windows.
    """

    if str(activation_payload.get("recommendation") or "") != RECOMMENDATION_EXTENDED:
        return FeatureConfig()
    feature_config = activation_payload.get("feature_config")
    if not isinstance(feature_config, Mapping) or _extended_config_mismatch_reason(feature_config) is not None:
        return FeatureConfig()
    return FeatureConfig(**EXTENDED_FEATURE_CONFIG_FIELDS)


def _extended_config_mismatch_reason(feature_config: Mapping[str, object]) -> str | None:
    """Return ``"tampered_feature_config"`` if ``feature_config`` does not
    match ``EXTENDED_FEATURE_CONFIG_FIELDS`` exactly, else ``None``."""

    for key, canonical_value in EXTENDED_FEATURE_CONFIG_FIELDS.items():
        if key not in feature_config:
            return "tampered_feature_config"
        value = feature_config[key]
        if isinstance(value, bool):
            return "tampered_feature_config"
        try:
            if isinstance(canonical_value, float):
                if not math.isclose(float(value), canonical_value):
                    return "tampered_feature_config"
            elif int(value) != int(canonical_value):
                return "tampered_feature_config"
        except (TypeError, ValueError):
            return "tampered_feature_config"
    return None


def _side_candidates(available_features: tuple[str, ...], *, extended: bool) -> list[dict[str, Any]]:
    """Return the benchmark candidates evaluated on one side of the comparison.

    Both sides start from ``build_benchmark_candidates``. The extended side
    additionally gets ``logreg_default_plus_extended``, a logistic candidate
    whose feature set is the union of the default model features present in
    the data plus the extended technical indicators present. This candidate
    is what makes the comparison *sensitive*: ``DEFAULT_MODEL_FEATURE_CANDIDATES``
    deliberately excludes ``rsi_14``/``macd_hist``/``bb_pct_b`` (they live in
    the separate ``EXTENDED_FEATURE_CANDIDATES`` constant), and without a
    candidate that actually combines both sets the champion model can win
    both sides with an identical score and the recommendation would be
    "baseline" by construction, never by evidence.
    """

    candidates = build_benchmark_candidates(available_features)
    if not extended:
        return candidates
    available = set(available_features)
    default_present = [name for name in DEFAULT_MODEL_FEATURE_CANDIDATES if name in available]
    extended_present = [name for name in EXTENDED_FEATURE_CANDIDATES if name in available]
    if extended_present:
        candidates.append(
            {
                "candidate_id": "logreg_default_plus_extended",
                "family": "logistic",
                "model_type": "logistic-baseline",
                "baseline_role": "challenger",
                "features": [*default_present, *extended_present],
            }
        )
    return candidates


def _best_candidate_score(
    features: list[dict[str, Any]],
    *,
    backtest_config: BacktestConfig,
    extended: bool = False,
) -> tuple[float | None, str | None]:
    """Evaluate every benchmark candidate available for ``features`` (reusing
    ``trading_model_benchmark``'s per-candidate evaluation/scoring so the
    cost-net score logic is never duplicated) and return the best ``OK``
    candidate's score, or ``(None, None)`` if none is valid.

    The sensitivity of the baseline-vs-extended comparison depends on
    ``extended=True`` injecting the ``logreg_default_plus_extended`` union
    candidate (see ``_side_candidates``); without it the extended side would
    never field a candidate that combines the default model features with
    ``rsi_14``/``macd_hist``/``bb_pct_b``.
    """

    available = _available_features(features)
    candidates = _side_candidates(available, extended=extended)
    rows = [
        _evaluate_candidate(
            candidate,
            feature_records=features,
            signal_model=_DEFAULT_SIGNAL_MODEL,
            threshold=_DEFAULT_THRESHOLD,
            min_signal_margin=_DEFAULT_MIN_SIGNAL_MARGIN,
            max_buy_signals=_DEFAULT_MAX_BUY_SIGNALS,
            backtest_config=backtest_config,
            embargo=_DEFAULT_EMBARGO,
        )
        for candidate in candidates
    ]
    valid = [
        row
        for row in rows
        if row.get("status") == "OK" and row.get("feature_names") and math.isfinite(_finite_float(row.get("score")))
    ]
    if not valid:
        return None, None
    best = max(valid, key=lambda row: _finite_float(row.get("score")))
    return _finite_float(best.get("score")), str(best.get("candidate_id") or "") or None


def _finite_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("-inf")
    return number if math.isfinite(number) else float("-inf")


def _fail_closed(reason: str) -> dict[str, object]:
    return {
        "recommendation": RECOMMENDATION_BASELINE,
        "feature_config": {},
        "fail_closed": True,
        "reason": reason,
    }


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
        "live_execution_enabled": False,
        "mutates_latest_model": False,
        "llm_authority": "none",
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _render_markdown(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_rows = blockers if isinstance(blockers, list) else []
    lines = [
        "# Indicator Activation Report",
        "",
        f"- As of date: `{payload.get('as_of_date')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Recommendation: `{payload.get('recommendation')}`",
        f"- Baseline score: `{payload.get('baseline_score')}`",
        f"- Extended score: `{payload.get('extended_score')}`",
        f"- Relative margin observed: `{payload.get('relative_margin_observed')}`",
        f"- Min relative margin: `{payload.get('min_relative_margin')}`",
        "",
        "## Blockers",
    ]
    lines.extend(f"- `{blocker}`" for blocker in blocker_rows)
    if not blocker_rows:
        lines.append("- `none`")
    lines.extend(["", "Orders submitted: `False`", "LLM authority: `none`", ""])
    return "\n".join(lines)
