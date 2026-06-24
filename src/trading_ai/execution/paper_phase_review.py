"""Read-only phase review for the 60-session paper campaign."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import load_risk_config
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    reason_codes,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_graduation import evaluate_paper_graduation

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_phase_review"
DEFAULT_MIN_STABLE_SESSIONS = 60
DEFAULT_MIN_PAPER_AUTO_CLEAN_SESSIONS = 20


class PaperPhaseReviewOperationalError(RuntimeError):
    """Raised when the paper phase review cannot be produced."""


@dataclass(frozen=True)
class PaperPhaseReviewResult:
    exit_code: int
    status: str
    phase_status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_phase_review_report(
    *,
    as_of_date: str,
    campaign_report: str | Path,
    performance_report: str | Path,
    operator_status: str | Path,
    strategy_quality: str | Path,
    evidence_index: str | Path,
    risk: str | Path = "configs/risk.yml",
    weekly_summary: str | Path | None = None,
    trial_day_root: str | Path | None = None,
    min_stable_sessions: int = DEFAULT_MIN_STABLE_SESSIONS,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperPhaseReviewResult:
    generated = generated_at or _utc_now()
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "phase_review.json"
    markdown_path = output_root / "phase_review.md"
    try:
        payload = build_paper_phase_review_report(
            as_of_date=as_of_date,
            campaign_report=campaign_report,
            performance_report=performance_report,
            operator_status=operator_status,
            strategy_quality=strategy_quality,
            evidence_index=evidence_index,
            risk=risk,
            weekly_summary=weekly_summary,
            trial_day_root=trial_day_root,
            min_stable_sessions=min_stable_sessions,
            generated_at=generated,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _error_payload(as_of_date=as_of_date, generated_at=generated, message=str(exc))
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_phase_review_markdown(payload), markdown_path)
    status = str(payload.get("status") or "ERROR")
    return PaperPhaseReviewResult(
        exit_code=paper_exit_code(status),
        status=status,
        phase_status=str(payload.get("phase_status") or "BLOCKED"),
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def build_paper_phase_review_report(
    *,
    as_of_date: str,
    campaign_report: str | Path,
    performance_report: str | Path,
    operator_status: str | Path,
    strategy_quality: str | Path,
    evidence_index: str | Path,
    risk: str | Path,
    weekly_summary: str | Path | None,
    trial_day_root: str | Path | None,
    min_stable_sessions: int,
    generated_at: str,
) -> dict[str, object]:
    campaign = read_json_artifact(campaign_report)
    performance = read_json_artifact(performance_report)
    operator = read_json_artifact(operator_status)
    quality = read_json_artifact(strategy_quality)
    evidence = read_json_artifact(evidence_index)
    weekly = read_json_artifact(weekly_summary) if weekly_summary is not None else None

    blockers: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    artifact_payloads: tuple[tuple[str, str | Path, Mapping[str, object]], ...] = (
        ("campaign_report", campaign_report, campaign),
        ("performance_report", performance_report, performance),
        ("operator_status", operator_status, operator),
        ("strategy_quality", strategy_quality, quality),
        ("evidence_index", evidence_index, evidence),
    )
    if weekly is not None and weekly_summary is not None:
        artifact_payloads = (*artifact_payloads, ("weekly_summary", weekly_summary, weekly))
    for artifact_id, path, payload in artifact_payloads:
        blockers.extend(_safety_blockers(artifact_id, path, payload))

    stable_sessions = _stable_session_summary(campaign, min_stable_sessions=min_stable_sessions)
    paper_auto = _paper_auto_summary(campaign)
    real_money = _mapping(campaign.get("real_money_consideration"))
    blockers.extend(_campaign_blockers(campaign_report, campaign))
    blockers.extend(_operator_blockers(operator_status, operator, as_of_date=as_of_date))
    blockers.extend(_performance_blockers(performance_report, performance))
    blockers.extend(_quality_blockers(strategy_quality, quality))
    blockers.extend(_evidence_blockers(evidence_index, evidence))
    if weekly is not None and weekly_summary is not None:
        blockers.extend(
            _status_blockers(
                "weekly_summary", weekly_summary, weekly, critical_statuses={"CRITICAL", "ERROR", "BLOCKED"}
            )
        )

    if _int_value(stable_sessions.get("clean_sessions"), default=0) < _int_value(
        stable_sessions.get("target_clean_sessions"), default=DEFAULT_MIN_STABLE_SESSIONS
    ):
        warnings.append(
            _finding(
                "WARNING",
                "stable_sessions_accumulating",
                "60-session stability campaign is still accumulating",
                source_path=campaign_report,
            )
        )
    if _int_value(paper_auto.get("clean_sessions"), default=0) < _int_value(
        paper_auto.get("target_clean_sessions"), default=DEFAULT_MIN_PAPER_AUTO_CLEAN_SESSIONS
    ):
        warnings.append(
            _finding(
                "WARNING",
                "paper_auto_sessions_accumulating",
                "20-session paper-auto campaign is still accumulating",
                source_path=campaign_report,
            )
        )
    quality_status = str(quality.get("quality_status") or "").upper()
    if quality_status == "DEFER":
        warnings.append(
            _finding("WARNING", "strategy_quality_defer", "strategy quality is deferred", source_path=strategy_quality)
        )
    elif quality_status not in {"PASS", "WARN"}:
        warnings.append(
            _finding(
                "WARNING",
                "strategy_quality_not_reviewable",
                "strategy quality is not PASS or WARN",
                source_path=strategy_quality,
            )
        )

    blockers = _dedupe_findings(blockers)
    warnings = _dedupe_findings(warnings)
    phase_status = _phase_status(
        blockers=blockers, warnings=warnings, stable_sessions=stable_sessions, paper_auto=paper_auto, quality=quality
    )
    status = _status_for_phase(phase_status, blockers=blockers, warnings=warnings)
    phase_evidence = {"phase_status": phase_status, "status": status, "review_only": True}
    paper_graduation = evaluate_paper_graduation(
        risk_limits=load_risk_config(risk),
        campaign_report=campaign,
        phase_review=phase_evidence,
        campaign_report_path=campaign_report,
        phase_review_path=None,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "phase_status": phase_status,
        "next_action": _next_action(
            phase_status, stable_sessions=stable_sessions, paper_auto=paper_auto, quality=quality
        ),
        "review_only": True,
        "live_trading_authorized": False,
        "stable_sessions": stable_sessions,
        "paper_auto_campaign": paper_auto,
        "real_money_consideration": dict(real_money),
        "quality_status": quality_status or "UNKNOWN",
        "sources": {
            "campaign_report": str(Path(campaign_report)),
            "risk": str(Path(risk)),
            "performance_report": str(Path(performance_report)),
            "operator_status": str(Path(operator_status)),
            "strategy_quality": str(Path(strategy_quality)),
            "evidence_index": str(Path(evidence_index)),
            "weekly_summary": str(Path(weekly_summary)) if weekly_summary is not None else None,
            "trial_day_root": str(Path(trial_day_root)) if trial_day_root is not None else None,
        },
        "artifact_statuses": {
            artifact_id: str(payload.get("status") or payload.get("phase_status") or payload.get("state") or "UNKNOWN")
            for artifact_id, _path, payload in artifact_payloads
        },
        "paper_graduation": paper_graduation,
        "blockers": blockers,
        "warnings": warnings,
        "authority": {
            "review_only": True,
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    return _redact_payload(payload)


def render_paper_phase_review_markdown(payload: Mapping[str, object]) -> str:
    stable = _mapping(payload.get("stable_sessions"))
    paper_auto = _mapping(payload.get("paper_auto_campaign"))
    graduation = _mapping(payload.get("paper_graduation"))
    blockers = _object_list(payload.get("blockers"))
    warnings = _object_list(payload.get("warnings"))
    lines = [
        "# Paper Phase Review",
        "",
        f"Status: **{payload.get('status') or 'ERROR'}**",
        f"Phase status: **{payload.get('phase_status') or 'BLOCKED'}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Next action: `{payload.get('next_action') or ''}`",
        "",
        "## Stable Sessions",
        "",
        f"Clean sessions: `{stable.get('clean_sessions', 0)}` / `{stable.get('target_clean_sessions', 0)}`",
        f"Remaining sessions: `{stable.get('remaining_sessions', 0)}`",
        "",
        "## Paper Auto Campaign",
        "",
        f"Clean sessions: `{paper_auto.get('clean_sessions', 0)}` / `{paper_auto.get('target_clean_sessions', 0)}`",
        f"Remaining clean sessions: `{paper_auto.get('remaining_clean_sessions', 0)}`",
        "",
        "## Paper Graduation",
        "",
        f"Stage: `{graduation.get('stage') or ''}`",
        f"Notional: `{graduation.get('paper_notional_usd') or ''}`",
        f"Allowed: `{graduation.get('allowed')}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Message |",
        "| --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    f"| `{_escape(blocker.get('severity') or '')}` | "
                    f"`{_escape(blocker.get('code') or '')}` | "
                    f"{_escape(blocker.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | No phase blockers. |")
    lines.extend(["", "## Warnings", "", "| Code | Message |", "| --- | --- |"])
    if warnings:
        for warning in warnings:
            if isinstance(warning, Mapping):
                lines.append(f"| `{_escape(warning.get('code') or '')}` | {_escape(warning.get('message') or '')} |")
    else:
        lines.append("| none | No phase warnings. |")
    lines.extend(["", "Review only: `True`", "Live trading authorized: `False`", ""])
    return "\n".join(lines)


def _stable_session_summary(campaign: Mapping[str, object], *, min_stable_sessions: int) -> dict[str, object]:
    stability = _mapping(campaign.get("stability_campaign"))
    if stability:
        clean = _int_value(stability.get("clean_sessions"), default=0)
        target = _int_value(stability.get("target_clean_sessions"), default=min_stable_sessions)
        return {
            "state": str(stability.get("state") or ("READY_FOR_REVIEW" if clean >= target else "ACCUMULATING")),
            "target_clean_sessions": target,
            "clean_sessions": clean,
            "broker_confirmed_sessions": _int_value(stability.get("broker_confirmed_sessions"), default=clean),
            "remaining_sessions": max(target - clean, 0),
            "remaining_clean_sessions": max(target - clean, 0),
            "critical_blockers": reason_codes(stability.get("critical_blockers")),
            "blocker_histogram": dict(_mapping(stability.get("blocker_histogram"))),
        }
    progress = _mapping(campaign.get("progress"))
    clean = _int_value(progress.get("complete_sessions"), default=0)
    target = _int_value(progress.get("target_sessions"), default=min_stable_sessions)
    return {
        "state": "READY_FOR_REVIEW" if clean >= target else "ACCUMULATING",
        "target_clean_sessions": target,
        "clean_sessions": clean,
        "broker_confirmed_sessions": clean,
        "remaining_sessions": max(target - clean, 0),
        "remaining_clean_sessions": max(target - clean, 0),
        "critical_blockers": [],
        "blocker_histogram": {},
    }


def _paper_auto_summary(campaign: Mapping[str, object]) -> dict[str, object]:
    paper_auto = _mapping(campaign.get("paper_auto_campaign"))
    clean = _int_value(paper_auto.get("clean_sessions"), default=0)
    target = _int_value(paper_auto.get("target_clean_sessions"), default=DEFAULT_MIN_PAPER_AUTO_CLEAN_SESSIONS)
    return {
        "state": str(paper_auto.get("state") or ("READY_FOR_REVIEW" if clean >= target else "ACCUMULATING")),
        "target_clean_sessions": target,
        "clean_sessions": clean,
        "broker_confirmed_sessions": _int_value(paper_auto.get("broker_confirmed_sessions"), default=clean),
        "remaining_clean_sessions": max(target - clean, 0),
        "blocker_histogram": dict(_mapping(paper_auto.get("blocker_histogram"))),
    }


def _campaign_blockers(path: str | Path, campaign: Mapping[str, object]) -> list[dict[str, object]]:
    blockers = _status_blockers("campaign_report", path, campaign, critical_statuses={"CRITICAL", "ERROR", "BLOCKED"})
    for blocker in _object_list(campaign.get("blockers")):
        if isinstance(blocker, Mapping) and str(blocker.get("severity") or "").upper() in {"CRITICAL", "ERROR"}:
            blockers.append(
                _finding(
                    str(blocker.get("severity") or "CRITICAL").upper(),
                    str(blocker.get("code") or "campaign_blocker"),
                    str(blocker.get("message") or blocker.get("code") or "campaign blocker"),
                    source_path=blocker.get("source_path") or path,
                )
            )
    for section_name in ("paper_auto_campaign", "stability_campaign"):
        section = _mapping(campaign.get(section_name))
        for code, count in _mapping(section.get("blocker_histogram")).items():
            blockers.append(
                _finding("CRITICAL", str(code), f"{section_name} blocker observed {count} time(s)", source_path=path)
            )
        for code in reason_codes(section.get("critical_blockers")):
            blockers.append(_finding("CRITICAL", code, f"{section_name} critical blocker: {code}", source_path=path))
    return blockers


def _operator_blockers(path: str | Path, operator: Mapping[str, object], *, as_of_date: str) -> list[dict[str, object]]:
    blockers = _status_blockers("operator_status", path, operator, critical_statuses={"CRITICAL", "ERROR", "BLOCKED"})
    if str(operator.get("as_of_date") or "") not in {"", as_of_date}:
        blockers.append(
            _finding("CRITICAL", "operator_status_stale", "operator status is for a different date", source_path=path)
        )
    if operator.get("clean_for_paper_auto") is not True:
        codes = [
            str(item.get("code"))
            for item in _object_list(operator.get("blockers"))
            if isinstance(item, Mapping) and item.get("code") not in {None, ""}
        ]
        for code in codes or ["operator_status_not_clean"]:
            blockers.append(_finding("CRITICAL", code, f"operator status is not clean: {code}", source_path=path))
    return blockers


def _performance_blockers(path: str | Path, performance: Mapping[str, object]) -> list[dict[str, object]]:
    blockers = _status_blockers(
        "performance_report", path, performance, critical_statuses={"CRITICAL", "ERROR", "BLOCKED"}
    )
    for code in reason_codes(performance.get("blockers")):
        blockers.append(_finding("CRITICAL", code, f"performance blocker: {code}", source_path=path))
    metrics = _mapping(performance.get("paper_metrics"))
    if _int_value(metrics.get("pending_closeouts"), default=0) > 0:
        blockers.append(_finding("CRITICAL", "closeout_pending", "performance has pending closeouts", source_path=path))
    if _int_value(metrics.get("unmatched_closeouts"), default=0) > 0:
        blockers.append(
            _finding("CRITICAL", "closeout_unmatched", "performance has unmatched closeouts", source_path=path)
        )
    statement = _mapping(performance.get("statement_status"))
    reconciliation = _mapping(performance.get("statement_reconciliation"))
    statement_status = str(statement.get("status") or reconciliation.get("status") or "").upper()
    if statement_status in {"ERROR", "MISMATCH", "DIFFERENCES", "UNMATCHED", "MISSING"}:
        blockers.append(
            _finding(
                "CRITICAL", "statement_mismatch", "performance statement reconciliation is not clean", source_path=path
            )
        )
    if (
        _int_value(
            statement.get("unreconciled_fills"), default=_int_value(reconciliation.get("missing_fills"), default=0)
        )
        > 0
    ):
        blockers.append(
            _finding("CRITICAL", "fills_unreconciled", "performance has unreconciled fills", source_path=path)
        )
    return blockers


def _quality_blockers(path: str | Path, quality: Mapping[str, object]) -> list[dict[str, object]]:
    blockers = _status_blockers("strategy_quality", path, quality, critical_statuses={"CRITICAL", "ERROR", "BLOCKED"})
    quality_status = str(quality.get("quality_status") or "").upper()
    if quality_status == "BLOCKED":
        for code in reason_codes(quality.get("blockers")) or ["strategy_quality_blocked"]:
            blockers.append(_finding("CRITICAL", code, f"strategy quality is blocked: {code}", source_path=path))
    return blockers


def _evidence_blockers(path: str | Path, evidence: Mapping[str, object]) -> list[dict[str, object]]:
    blockers = _status_blockers("evidence_index", path, evidence, critical_statuses={"ERROR", "CRITICAL", "BLOCKED"})
    for issue in _object_list(evidence.get("issues")):
        if not isinstance(issue, Mapping):
            continue
        severity = str(issue.get("severity") or "").upper()
        if severity in {"ERROR", "CRITICAL"}:
            blockers.append(
                _finding(
                    severity,
                    str(issue.get("code") or "evidence_issue"),
                    str(issue.get("message") or issue.get("code") or "evidence issue"),
                    source_path=issue.get("source_path") or path,
                )
            )
    return blockers


def _status_blockers(
    artifact_id: str,
    path: str | Path,
    payload: Mapping[str, object],
    *,
    critical_statuses: set[str],
) -> list[dict[str, object]]:
    status = str(payload.get("status") or payload.get("phase_status") or payload.get("state") or "").upper()
    if status in critical_statuses:
        severity = "ERROR" if status == "ERROR" else "CRITICAL"
        return [
            _finding(severity, f"{artifact_id}_{status.lower()}", f"{artifact_id} status is {status}", source_path=path)
        ]
    return []


def _safety_blockers(artifact_id: str, path: str | Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    safety = _mapping(payload.get("safety"))
    authority = _mapping(payload.get("authority"))
    blockers: list[dict[str, object]] = []
    for field in ("broker_client_built", "credentials_read", "orders_submitted"):
        if safety.get(field) is True or authority.get(field) is True:
            blockers.append(_finding("CRITICAL", field, f"{artifact_id} reports {field}", source_path=path))
    if (
        safety.get("live_trading_authorized") is True
        or safety.get("live_trading_allowed") is True
        or authority.get("live_trading_authorized") is True
    ):
        blockers.append(
            _finding(
                "CRITICAL",
                "live_trading_not_allowed",
                f"{artifact_id} attempts live trading authority",
                source_path=path,
            )
        )
    return blockers


def _phase_status(
    *,
    blockers: Sequence[Mapping[str, object]],
    warnings: Sequence[Mapping[str, object]],
    stable_sessions: Mapping[str, object],
    paper_auto: Mapping[str, object],
    quality: Mapping[str, object],
) -> str:
    if any(str(blocker.get("severity") or "").upper() in {"CRITICAL", "ERROR"} for blocker in blockers):
        return "BLOCKED"
    if _int_value(stable_sessions.get("clean_sessions"), default=0) < _int_value(
        stable_sessions.get("target_clean_sessions"), default=DEFAULT_MIN_STABLE_SESSIONS
    ):
        return "ACCUMULATING"
    if _int_value(paper_auto.get("clean_sessions"), default=0) < _int_value(
        paper_auto.get("target_clean_sessions"), default=DEFAULT_MIN_PAPER_AUTO_CLEAN_SESSIONS
    ):
        return "ACCUMULATING"
    if str(quality.get("quality_status") or "").upper() == "DEFER":
        return "ACCUMULATING"
    if warnings and str(quality.get("quality_status") or "").upper() not in {"PASS", "WARN"}:
        return "ACCUMULATING"
    return "READY_FOR_REVIEW"


def _status_for_phase(
    phase_status: str, *, blockers: Sequence[Mapping[str, object]], warnings: Sequence[Mapping[str, object]]
) -> str:
    if phase_status == "BLOCKED":
        if any(str(blocker.get("severity") or "").upper() == "ERROR" for blocker in blockers):
            return "ERROR"
        return "CRITICAL"
    if phase_status == "ACCUMULATING" or warnings:
        return "WARN"
    return "OK"


def _next_action(
    phase_status: str,
    *,
    stable_sessions: Mapping[str, object],
    paper_auto: Mapping[str, object],
    quality: Mapping[str, object],
) -> str:
    if phase_status == "BLOCKED":
        return "resolve_phase_blockers"
    if _int_value(stable_sessions.get("clean_sessions"), default=0) < _int_value(
        stable_sessions.get("target_clean_sessions"), default=DEFAULT_MIN_STABLE_SESSIONS
    ):
        return "continue_accumulating_stable_sessions"
    if _int_value(paper_auto.get("clean_sessions"), default=0) < _int_value(
        paper_auto.get("target_clean_sessions"), default=DEFAULT_MIN_PAPER_AUTO_CLEAN_SESSIONS
    ):
        return "continue_paper_auto_campaign"
    if str(quality.get("quality_status") or "").upper() == "DEFER":
        return "continue_quality_evidence"
    return "manual_phase_review"


def _error_payload(*, as_of_date: str, generated_at: str, message: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": "ERROR",
        "phase_status": "BLOCKED",
        "next_action": "fix_phase_review_error",
        "review_only": True,
        "live_trading_authorized": False,
        "errors": [{"code": "invalid_phase_review_input", "message": redact_secrets(message, env={})}],
        "blockers": [
            {"severity": "ERROR", "code": "invalid_phase_review_input", "message": redact_secrets(message, env={})}
        ],
        "authority": {
            "review_only": True,
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def _finding(severity: str, code: str, message: str, *, source_path: object = None) -> dict[str, object]:
    item: dict[str, object] = {"severity": severity, "code": code, "message": redact_secrets(message, env={})}
    if source_path not in {None, ""}:
        item["source_path"] = str(source_path)
    return item


def _dedupe_findings(findings: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        item = dict(finding)
        key = (str(item.get("severity") or ""), str(item.get("code") or ""), str(item.get("source_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_value(value: object, *, default: int) -> int:
    if value in {None, ""}:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(_redact_value(payload)))


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
