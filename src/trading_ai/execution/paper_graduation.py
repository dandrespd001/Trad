"""Governed paper notional graduation gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from trading_ai.risk.policy import RiskLimits

CANARY_STAGE = "CANARY"
SCALE_UP_STAGE = "SCALE_UP"
READINESS_STAGE = "READINESS"
PAPER_STAGES = frozenset({CANARY_STAGE, SCALE_UP_STAGE, READINESS_STAGE})
CANARY_CAP_USD = 1.0
NON_CANARY_CAP_USD = 5.0


def evaluate_paper_graduation(
    *,
    risk_limits: RiskLimits,
    campaign_report: Mapping[str, object] | None = None,
    phase_review: Mapping[str, object] | None = None,
    campaign_report_path: str | Path | None = None,
    phase_review_path: str | Path | None = None,
) -> dict[str, object]:
    stage = str(risk_limits.paper_stage or CANARY_STAGE).upper()
    stage_cap = CANARY_CAP_USD if stage == CANARY_STAGE else NON_CANARY_CAP_USD
    blockers: list[dict[str, object]] = []
    campaign_evidence = _campaign_evidence(campaign_report, campaign_report_path)
    phase_evidence = _phase_evidence(phase_review, phase_review_path)
    evidence: dict[str, object] = {"campaign_report": campaign_evidence, "phase_review": phase_evidence}

    if stage not in PAPER_STAGES:
        blockers.append(_blocker("invalid_paper_stage", f"paper stage is not supported: {stage}", "risk"))
    if stage == CANARY_STAGE:
        if abs(float(risk_limits.paper_notional_usd) - CANARY_CAP_USD) > 1e-9:
            blockers.append(_blocker("canary_notional_must_be_one", "CANARY requires USD 1.0 notional", "risk"))
    else:
        if not _non_empty(risk_limits.paper_stage_reviewer):
            blockers.append(_blocker("paper_stage_reviewer_missing", f"{stage} requires reviewer", "risk"))
        if not _non_empty(risk_limits.paper_stage_reason):
            blockers.append(_blocker("paper_stage_reason_missing", f"{stage} requires reason", "risk"))
        notional = float(risk_limits.paper_notional_usd)
        if not CANARY_CAP_USD <= notional <= NON_CANARY_CAP_USD:
            blockers.append(_blocker("paper_notional_outside_stage_cap", f"{stage} requires USD 1.0 to 5.0", "risk"))
        blockers.extend(_campaign_blockers(campaign_evidence))
        if stage == READINESS_STAGE:
            blockers.extend(_phase_blockers(phase_evidence))

    return {
        "stage": stage,
        "paper_notional_usd": float(risk_limits.paper_notional_usd),
        "stage_cap_usd": stage_cap,
        "reviewer": risk_limits.paper_stage_reviewer,
        "reason": risk_limits.paper_stage_reason,
        "allowed": not blockers,
        "blockers": blockers,
        "evidence": evidence,
    }


def load_optional_json_report(path: str | Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON report must contain an object: {path}")
    return payload


def legacy_canary_graduation(*, paper_notional_usd: float = 1.0) -> dict[str, object]:
    allowed = abs(float(paper_notional_usd) - CANARY_CAP_USD) <= 1e-9
    blockers = (
        []
        if allowed
        else [
            _blocker(
                "legacy_notional_not_canary",
                "legacy sessions are limited to CANARY USD 1.0",
                "signal_report",
            )
        ]
    )
    return {
        "stage": CANARY_STAGE,
        "paper_notional_usd": float(paper_notional_usd),
        "stage_cap_usd": CANARY_CAP_USD,
        "reviewer": None,
        "reason": None,
        "allowed": allowed,
        "blockers": blockers,
        "evidence": {"campaign_report": {"provided": False}, "phase_review": {"provided": False}},
        "legacy": True,
    }


def graduation_reasons(
    *,
    current: Mapping[str, object] | None,
    expected: Mapping[str, object],
    signal_report: Mapping[str, object] | None = None,
) -> list[str]:
    current_report = current
    if current_report is None and signal_report is not None:
        signal_graduation = signal_report.get("paper_graduation")
        current_report = signal_graduation if isinstance(signal_graduation, Mapping) else None
    if current_report is None:
        order_intent = signal_report.get("order_intent") if signal_report is not None else None
        notional = _float_or_none(order_intent.get("notional")) if isinstance(order_intent, Mapping) else None
        current_report = legacy_canary_graduation(paper_notional_usd=notional or CANARY_CAP_USD)

    reasons: list[str] = []
    stage = str(current_report.get("stage") or "").upper()
    expected_stage = str(expected.get("stage") or "").upper()
    current_notional = _float_or_none(current_report.get("paper_notional_usd"))
    expected_notional = _float_or_none(expected.get("paper_notional_usd"))
    if stage != expected_stage:
        reasons.append("paper_stage_mismatch")
    if current_notional is None or expected_notional is None or abs(current_notional - expected_notional) > 1e-9:
        reasons.append("paper_notional_mismatch")
    if current_report.get("allowed") is not True:
        reasons.append("paper_graduation_not_allowed")
    if expected.get("allowed") is not True:
        reasons.append("paper_graduation_evidence_not_allowed")
    if stage != CANARY_STAGE and current_report.get("legacy") is True:
        reasons.append("paper_graduation_missing_for_non_canary")
    if expected_stage != CANARY_STAGE:
        if _evidence_sha256(current_report, "campaign_report") != _evidence_sha256(expected, "campaign_report"):
            reasons.append("paper_campaign_evidence_hash_mismatch")
        if expected_stage == READINESS_STAGE and _evidence_sha256(current_report, "phase_review") != _evidence_sha256(
            expected, "phase_review"
        ):
            reasons.append("paper_phase_evidence_hash_mismatch")
    return reasons


def _campaign_evidence(
    campaign_report: Mapping[str, object] | None,
    campaign_report_path: str | Path | None,
) -> dict[str, object]:
    if campaign_report is None:
        return {"provided": False, "path": str(campaign_report_path) if campaign_report_path is not None else None}
    real_money = _mapping(campaign_report.get("real_money_consideration"))
    path_text = str(campaign_report_path) if campaign_report_path is not None else None
    return {
        "provided": True,
        "path": path_text,
        "sha256": _file_sha256(campaign_report_path),
        "status": campaign_report.get("status"),
        "real_money_state": real_money.get("state"),
        "clean_trial_days": _int_value(real_money.get("clean_trial_days")),
        "target_trial_days": _int_value(real_money.get("target_trial_days"), default=30),
        "recovery_days": _int_value(real_money.get("recovery_days")),
        "error_days": _int_value(real_money.get("error_days")),
        "live_true_paths": _live_true_paths(campaign_report),
    }


def _phase_evidence(
    phase_review: Mapping[str, object] | None,
    phase_review_path: str | Path | None,
) -> dict[str, object]:
    if phase_review is None:
        return {"provided": False, "path": str(phase_review_path) if phase_review_path is not None else None}
    return {
        "provided": True,
        "path": str(phase_review_path) if phase_review_path is not None else None,
        "sha256": _file_sha256(phase_review_path),
        "status": phase_review.get("status"),
        "phase_status": phase_review.get("phase_status"),
        "review_only": phase_review.get("review_only"),
        "live_true_paths": _live_true_paths(phase_review),
    }


def _campaign_blockers(evidence: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    if evidence.get("provided") is not True:
        return [_blocker("campaign_report_missing", "SCALE_UP requires a paper campaign report", "campaign_report")]
    if str(evidence.get("real_money_state") or "").upper() != "PAPER_EVIDENCE_READY":
        blockers.append(
            _blocker("campaign_evidence_not_ready", "campaign real money evidence is not ready", "campaign_report")
        )
    if _int_value(evidence.get("clean_trial_days")) < 30:
        blockers.append(
            _blocker("campaign_trial_days_below_30", "campaign requires 30 clean trial days", "campaign_report")
        )
    if _int_value(evidence.get("recovery_days")) > 0:
        blockers.append(
            _blocker("campaign_recovery_days_nonzero", "campaign has recovery trial days", "campaign_report")
        )
    if _int_value(evidence.get("error_days")) > 0:
        blockers.append(_blocker("campaign_error_days_nonzero", "campaign has error trial days", "campaign_report"))
    live_true_paths = _string_list(evidence.get("live_true_paths"))
    if live_true_paths:
        blockers.append(
            _blocker(
                "campaign_live_trading_not_allowed",
                "campaign report contains live trading authority: " + ", ".join(live_true_paths),
                "campaign_report",
            )
        )
    return blockers


def _phase_blockers(evidence: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    if evidence.get("provided") is not True:
        return [_blocker("phase_review_missing", "READINESS requires a phase review report", "phase_review")]
    if str(evidence.get("phase_status") or "").upper() != "READY_FOR_REVIEW":
        blockers.append(_blocker("phase_review_not_ready", "phase review is not READY_FOR_REVIEW", "phase_review"))
    if evidence.get("review_only") is not True:
        blockers.append(_blocker("phase_review_not_review_only", "phase review must be review-only", "phase_review"))
    live_true_paths = _string_list(evidence.get("live_true_paths"))
    if live_true_paths:
        blockers.append(
            _blocker(
                "phase_review_live_trading_not_allowed",
                "phase review contains live trading authority: " + ", ".join(live_true_paths),
                "phase_review",
            )
        )
    return blockers


def _blocker(code: str, message: str, source: str) -> dict[str, object]:
    return {"severity": "fail", "code": code, "message": message, "source": source}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _non_empty(value: object) -> bool:
    return bool(str(value or "").strip())


def _int_value(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    if isinstance(value, (Mapping, list)):
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


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


def _file_sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_sha256(report: Mapping[str, object], evidence_key: str) -> str | None:
    evidence = _mapping(report.get("evidence"))
    item = _mapping(evidence.get(evidence_key))
    value = item.get("sha256")
    return str(value) if value not in {None, ""} else None


def _live_true_paths(payload: object, *, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if key_text in {"live_trading_authorized", "live_trading_allowed"} and _is_true(value):
                paths.append(path)
            paths.extend(_live_true_paths(value, prefix=path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            paths.extend(_live_true_paths(value, prefix=path))
    return paths


def _is_true(value: object) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]
