"""Read-only daily paper operations completeness check."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_payload,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_ops_check"


class PaperOpsCheckOperationalError(RuntimeError):
    """Raised when the paper ops check cannot be written."""


@dataclass(frozen=True)
class PaperOpsCheckResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_ops_check(
    *,
    as_of_date: str,
    readiness_root: str | Path = "reports/tmp/paper_daily_prepare",
    sessions_root: str | Path = "reports/tmp/paper_session",
    monitor_root: str | Path = "reports/tmp/paper_monitor",
    campaign_root: str | Path = "reports/tmp/paper_campaign",
    decisions_root: str | Path = "reports/tmp/paper_decisions",
    performance_root: str | Path = "reports/tmp/paper_performance",
    position_watch: str | Path | None = None,
    eod_position_plan: str | Path | None = None,
    telegram_status: str | Path | None = None,
    telegram_history: str | Path | None = None,
    telegram_dispatch: str | Path | None = None,
    ai_value_report: str | Path | None = None,
    cross_asset_session_plan: str | Path | None = None,
    require_ai_value_ready: bool = False,
    ledger_inputs: Iterable[str | Path] = (),
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperOpsCheckResult:
    report = build_paper_ops_check(
        as_of_date=as_of_date,
        readiness_root=readiness_root,
        sessions_root=sessions_root,
        monitor_root=monitor_root,
        campaign_root=campaign_root,
        decisions_root=decisions_root,
        performance_root=performance_root,
        position_watch=position_watch,
        eod_position_plan=eod_position_plan,
        telegram_status=telegram_status,
        telegram_history=telegram_history,
        telegram_dispatch=telegram_dispatch,
        ai_value_report=ai_value_report,
        cross_asset_session_plan=cross_asset_session_plan,
        require_ai_value_ready=require_ai_value_ready,
        ledger_inputs=ledger_inputs,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "ops_check.json"
    markdown_path = output_root / "ops_check.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_ops_check_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperOpsCheckResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_ops_check(
    *,
    as_of_date: str,
    readiness_root: str | Path = "reports/tmp/paper_daily_prepare",
    sessions_root: str | Path = "reports/tmp/paper_session",
    monitor_root: str | Path = "reports/tmp/paper_monitor",
    campaign_root: str | Path = "reports/tmp/paper_campaign",
    decisions_root: str | Path = "reports/tmp/paper_decisions",
    performance_root: str | Path = "reports/tmp/paper_performance",
    position_watch: str | Path | None = None,
    eod_position_plan: str | Path | None = None,
    telegram_status: str | Path | None = None,
    telegram_history: str | Path | None = None,
    telegram_dispatch: str | Path | None = None,
    ai_value_report: str | Path | None = None,
    cross_asset_session_plan: str | Path | None = None,
    require_ai_value_ready: bool = False,
    ledger_inputs: Iterable[str | Path] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    issue_list: list[dict[str, object]] = []
    readiness = _load_artifact(
        "readiness",
        Path(readiness_root),
        as_of_date=as_of_date,
        filenames=("readiness.json", "latest.json"),
        required=True,
        issues=issue_list,
    )
    monitor = _load_artifact(
        "monitor",
        Path(monitor_root),
        as_of_date=as_of_date,
        filenames=("monitor.json", "latest.json"),
        required=True,
        issues=issue_list,
    )
    campaign = _load_artifact(
        "campaign",
        Path(campaign_root),
        as_of_date=as_of_date,
        filenames=("campaign.json", "latest.json"),
        required=True,
        issues=issue_list,
    )
    decision = _load_artifact(
        "decision",
        Path(decisions_root),
        as_of_date=as_of_date,
        filenames=("decision.json", "latest.json"),
        required=True,
        issues=issue_list,
    )
    performance = _load_artifact(
        "performance",
        Path(performance_root),
        as_of_date=as_of_date,
        filenames=("performance.json", "latest.json"),
        required=False,
        issues=issue_list,
    )
    position_watch_artifact = _load_direct_artifact(
        "position_watch",
        position_watch,
        required=False,
        issues=issue_list,
    )
    eod_position_plan_artifact = _load_direct_artifact(
        "eod_position_plan",
        eod_position_plan,
        required=False,
        issues=issue_list,
    )
    telegram_status_artifact = _load_direct_artifact(
        "telegram_status",
        telegram_status,
        required=False,
        issues=issue_list,
    )
    telegram_history_artifact = _load_direct_artifact(
        "telegram_history",
        telegram_history,
        required=False,
        issues=issue_list,
    )
    telegram_dispatch_artifact = _load_direct_artifact(
        "telegram_dispatch",
        telegram_dispatch,
        required=False,
        issues=issue_list,
    )
    ai_value_artifact = _load_direct_artifact(
        "ai_value_report",
        ai_value_report,
        required=require_ai_value_ready,
        issues=issue_list,
    )
    cross_asset_session_artifact = _load_direct_artifact(
        "cross_asset_session_plan",
        cross_asset_session_plan,
        required=False,
        issues=issue_list,
    )

    issues = [*issue_list]
    for artifact_name, artifact_payload in (
        ("readiness", readiness.payload),
        ("monitor", monitor.payload),
        ("campaign", campaign.payload),
        ("decision", decision.payload),
        ("performance", performance.payload),
        ("position_watch", position_watch_artifact.payload),
        ("eod_position_plan", eod_position_plan_artifact.payload),
        ("telegram_status", telegram_status_artifact.payload),
        ("telegram_history", telegram_history_artifact.payload),
        ("telegram_dispatch", telegram_dispatch_artifact.payload),
        ("cross_asset_session_plan", cross_asset_session_artifact.payload),
    ):
        issues.extend(_artifact_date_issues(artifact_name, artifact_payload, as_of_date=as_of_date))
    issues.extend(_readiness_issues(readiness.payload))
    issues.extend(_monitor_issues(monitor.payload))
    issues.extend(_campaign_issues(campaign.payload))
    issues.extend(_decision_issues(decision.payload))
    issues.extend(_performance_issues(performance.payload, present=performance.present))
    issues.extend(_position_watch_issues(position_watch_artifact.payload))
    issues.extend(_eod_position_plan_issues(eod_position_plan_artifact.payload))
    issues.extend(_telegram_status_issues(telegram_status_artifact.payload))
    issues.extend(_telegram_history_issues(telegram_history_artifact.payload))
    issues.extend(_telegram_dispatch_issues(telegram_dispatch_artifact.payload))
    issues.extend(
        _ai_value_issues(
            ai_value_artifact.payload,
            present=ai_value_artifact.present,
            required=require_ai_value_ready,
            as_of_date=as_of_date,
        )
    )
    issues.extend(_cross_asset_session_plan_issues(cross_asset_session_artifact.payload))
    ledger_summary, ledger_issues = _ledger_summary([Path(path) for path in ledger_inputs])
    issues.extend(ledger_issues)
    status = _status_from_issues(issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "sources": {
            "readiness_root": str(Path(readiness_root)),
            "sessions_root": str(Path(sessions_root)),
            "monitor_root": str(Path(monitor_root)),
            "campaign_root": str(Path(campaign_root)),
            "decisions_root": str(Path(decisions_root)),
            "performance_root": str(Path(performance_root)),
            "position_watch": str(Path(position_watch)) if position_watch is not None else None,
            "eod_position_plan": str(Path(eod_position_plan)) if eod_position_plan is not None else None,
            "telegram_status": str(Path(telegram_status)) if telegram_status is not None else None,
            "telegram_history": str(Path(telegram_history)) if telegram_history is not None else None,
            "telegram_dispatch": str(Path(telegram_dispatch)) if telegram_dispatch is not None else None,
            "ai_value_report": str(Path(ai_value_report)) if ai_value_report is not None else None,
            "cross_asset_session_plan": str(Path(cross_asset_session_plan))
            if cross_asset_session_plan is not None
            else None,
            "require_ai_value_ready": require_ai_value_ready,
            "ledger_inputs": [str(path) for path in ledger_inputs],
        },
        "artifacts": {
            "readiness": readiness.summary,
            "monitor": monitor.summary,
            "campaign": campaign.summary,
            "decision": decision.summary,
            "performance": performance.summary,
            "position_watch": position_watch_artifact.summary,
            "eod_position_plan": eod_position_plan_artifact.summary,
            "telegram_status": telegram_status_artifact.summary,
            "telegram_history": telegram_history_artifact.summary,
            "telegram_dispatch": telegram_dispatch_artifact.summary,
            "ai_value_report": ai_value_artifact.summary,
            "cross_asset_session_plan": cross_asset_session_artifact.summary,
        },
        "ledger": ledger_summary,
        "issues": _dedupe_issues(issues),
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_ops_check_markdown(report: Mapping[str, object]) -> str:
    artifacts = _mapping(report.get("artifacts"))
    issues = _object_list(report.get("issues"))
    lines = [
        "# Paper Ops Check",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        f"Generated at: `{report.get('generated_at') or ''}`",
        "",
        "## Artifacts",
        "",
        "| Artifact | Status | Path |",
        "| --- | --- | --- |",
    ]
    for name, artifact in artifacts.items():
        if not isinstance(artifact, Mapping):
            continue
        artifact_status = artifact.get("status") or artifact.get("decision") or "UNKNOWN"
        lines.append(f"| `{_escape(name)}` | `{_escape(artifact_status)}` | `{_escape(artifact.get('path') or '')}` |")
    lines.extend(["", "## Issues", "", "| Severity | Code | Message |", "| --- | --- | --- |"])
    if issues:
        for issue in issues:
            if isinstance(issue, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(issue.get('severity') or '')}` "
                    f"| `{_escape(issue.get('code') or '')}` "
                    f"| {_escape(issue.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | Paper day is complete. |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "Live trading authorized: `False`",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class _LoadedArtifact:
    present: bool
    payload: Mapping[str, object] | None
    summary: dict[str, object]


def _load_artifact(
    name: str,
    root: Path,
    *,
    as_of_date: str,
    filenames: tuple[str, ...],
    required: bool,
    issues: list[dict[str, object]],
) -> _LoadedArtifact:
    path = _find_artifact_path(root, as_of_date=as_of_date, filenames=filenames)
    if path is None:
        severity = "ERROR" if required else "WARNING"
        issues.append(_issue(severity, f"missing_{name}", f"{name} artifact is missing"))
        return _LoadedArtifact(False, None, {"present": False, "status": "MISSING", "path": None})
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        issues.append(_issue("ERROR", f"invalid_{name}_json", f"invalid {name} JSON: {exc}", source_path=path))
        return _LoadedArtifact(True, None, {"present": True, "status": "ERROR", "path": str(path)})
    return _LoadedArtifact(True, payload, _artifact_summary(path, payload))


def _load_direct_artifact(
    name: str,
    path: str | Path | None,
    *,
    required: bool,
    issues: list[dict[str, object]],
) -> _LoadedArtifact:
    if path is None:
        if required:
            issues.append(_issue("CRITICAL", f"missing_{name}", f"{name} artifact is required"))
        return _LoadedArtifact(
            False,
            None,
            {"present": False, "status": "MISSING" if required else "NOT_CONFIGURED", "path": None},
        )
    artifact_path = Path(path)
    try:
        payload = read_json_artifact(artifact_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        issues.append(_issue("ERROR", f"invalid_{name}_json", f"invalid {name} JSON: {exc}", source_path=path))
        return _LoadedArtifact(True, None, {"present": True, "status": "ERROR", "path": str(artifact_path)})
    return _LoadedArtifact(True, payload, _artifact_summary(artifact_path, payload))


def _find_artifact_path(root: Path, *, as_of_date: str, filenames: tuple[str, ...]) -> Path | None:
    if root.is_file():
        return root
    candidates: list[Path] = []
    for filename in filenames:
        candidates.extend(
            [
                root / as_of_date / filename,
                root / filename,
                root / "latest.json" if filename != "latest.json" else root / filename,
            ]
        )
    candidates.extend(path for path in sorted((root / as_of_date).glob("*.json")) if (root / as_of_date).exists())
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    if root.exists():
        dated = [path for path in sorted(root.rglob("*.json")) if as_of_date in path.parts or as_of_date in path.name]
        if dated:
            return dated[0]
    return None


def _artifact_summary(path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    summary = {
        "present": True,
        "path": str(path),
        "status": str(payload.get("status") or payload.get("state") or payload.get("decision") or "UNKNOWN"),
        "as_of_date": payload.get("as_of_date"),
    }
    if payload.get("decision") is not None:
        summary["decision"] = str(payload.get("decision"))
    if payload.get("reason") not in {None, ""}:
        summary["reason"] = str(payload.get("reason"))
    return summary


def _artifact_date_issues(
    name: str,
    payload: Mapping[str, object] | None,
    *,
    as_of_date: str,
) -> list[dict[str, object]]:
    if not payload:
        return []
    artifact_date = str(payload.get("as_of_date") or "")
    if artifact_date and artifact_date != as_of_date:
        return [_issue("CRITICAL", f"{name}_stale", f"{name} as_of_date does not match paper ops check date")]
    return []


def _readiness_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    if payload.get("status") == "READY" and payload.get("ready_for_paper_daily") is True:
        return []
    return [_issue("CRITICAL", "readiness_not_ready", "readiness is not READY")]


def _monitor_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    summary = _mapping(payload.get("monitor_summary"))
    if status == "ERROR":
        issues.append(_issue("ERROR", "monitor_error", "monitor status is ERROR"))
    elif status == "CRITICAL" or _int_value(summary.get("critical_count"), default=0) > 0:
        issues.append(_issue("CRITICAL", "monitor_critical", "monitor has critical alerts"))
    elif status == "WARN" or _int_value(summary.get("warning_count"), default=0) > 0:
        issues.append(_issue("WARNING", "monitor_warn", "monitor has warnings"))
    if _int_value(summary.get("pending_closeout_count"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_pending", "monitor reports pending closeouts"))
    if _int_value(summary.get("unmatched_closeout_count"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_unmatched", "monitor reports unmatched closeouts"))
    for alert in _object_list(payload.get("alerts")):
        if not isinstance(alert, Mapping):
            continue
        severity = str(alert.get("severity") or "WARNING").upper()
        if severity in {"ERROR", "CRITICAL"}:
            issues.append(_issue("CRITICAL", str(alert.get("code") or "monitor_alert"), "monitor critical alert"))
    return issues


def _campaign_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    status = str(payload.get("status") or "").upper()
    if status == "ERROR":
        return [_issue("ERROR", "campaign_error", "campaign report status is ERROR")]
    if status == "CRITICAL":
        return [_issue("CRITICAL", "campaign_critical", "campaign report is CRITICAL")]
    if status == "WARN":
        return [_issue("WARNING", "campaign_warn", "campaign report is WARN")]
    return []


def _decision_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    decision = str(payload.get("decision") or payload.get("state") or "").upper()
    if decision == "STOP":
        return [_issue("CRITICAL", "decision_stop", "day-close decision is STOP")]
    if decision == "ERROR":
        return [_issue("ERROR", "decision_error", "day-close decision is ERROR")]
    if decision == "REVIEW":
        return [_issue("WARNING", "decision_review", "day-close decision requires review")]
    if decision != "CONTINUE":
        return [_issue("WARNING", "decision_unknown", "day-close decision is not CONTINUE")]
    return []


def _performance_issues(payload: Mapping[str, object] | None, *, present: bool) -> list[dict[str, object]]:
    if not present:
        return []
    if not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    metrics = _mapping(payload.get("paper_metrics"))
    statement = _mapping(payload.get("statement_reconciliation"))
    if status == "ERROR":
        issues.append(_issue("ERROR", "performance_error", "performance report status is ERROR"))
    elif status == "CRITICAL":
        issues.append(_issue("CRITICAL", "performance_critical", "performance report is CRITICAL"))
    elif status == "WARN":
        issues.append(_issue("WARNING", "performance_warn", "performance report is WARN"))
    if _int_value(metrics.get("pending_closeouts"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_pending", "performance reports pending closeouts"))
    if _int_value(metrics.get("unmatched_closeouts"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_unmatched", "performance reports unmatched closeouts"))
    if statement and str(statement.get("status") or "").upper() in {"NOT_REQUESTED", "MISSING"}:
        issues.append(_issue("WARNING", "statement_absent", "broker statement was not matched"))
    return issues


def _position_watch_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues = _status_issues(
        "position_watch",
        payload,
        error_message="position watch status is ERROR",
        critical_message="position watch requires intervention",
        warn_message="position watch has warnings",
    )
    issues.extend(_artifact_safety_issues("position_watch", payload))
    return issues


def _eod_position_plan_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    summary = _mapping(payload.get("summary"))
    close_required = _int_value(summary.get("close_required_count"), default=0)
    longer_term = _int_value(summary.get("longer_term_hold_count"), default=0)
    wait_count = _int_value(summary.get("wait_count"), default=0)
    if status == "ERROR":
        issues.append(_issue("ERROR", "eod_position_plan_error", "EOD position plan status is ERROR"))
    if status == "CRITICAL" or close_required > 0:
        issues.append(_issue("CRITICAL", "eod_close_required", "EOD plan requires closing intraday positions"))
    elif status == "WARN" or longer_term > 0 or wait_count > 0:
        issues.append(_issue("WARNING", "eod_review_required", "EOD plan requires operator review"))
    issues.extend(_artifact_safety_issues("eod_position_plan", payload))
    return issues


def _telegram_status_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues = _status_issues(
        "telegram_status",
        payload,
        error_message="Telegram status artifact is ERROR",
        critical_message="Telegram status artifact is BLOCKED",
        warn_message="Telegram status artifact is WARN",
    )
    if _object_list(payload.get("blockers")):
        issues.append(_issue("CRITICAL", "telegram_status_blocked", "Telegram status has blockers"))
    issues.extend(_artifact_safety_issues("telegram_status", payload))
    return issues


def _telegram_history_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues = _status_issues(
        "telegram_history",
        payload,
        error_message="Telegram history artifact is ERROR",
        critical_message="Telegram history artifact is BLOCKED",
        warn_message="Telegram history artifact is WARN",
    )
    if _object_list(payload.get("blockers")):
        issues.append(_issue("CRITICAL", "telegram_history_blocked", "Telegram history has blockers"))
    issues.extend(_artifact_safety_issues("telegram_history", payload))
    return issues


def _telegram_dispatch_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    summary = _mapping(payload.get("summary"))
    blocked_count = _int_value(summary.get("blocked_count"), default=0)
    ready_count = _int_value(summary.get("ready_count"), default=0)
    if status == "ERROR":
        issues.append(_issue("ERROR", "telegram_dispatch_error", "Telegram dispatch artifact is ERROR"))
    if status == "BLOCKED" or blocked_count > 0 or _object_list(payload.get("blockers")):
        issues.append(_issue("CRITICAL", "telegram_dispatch_blocked", "Telegram dispatch has blocked control steps"))
    elif ready_count > 0:
        issues.append(
            _issue("WARNING", "telegram_dispatch_ready_for_operator", "Telegram dispatch has operator-ready steps")
        )
    safety = _mapping(payload.get("safety"))
    if safety.get("subprocess_started") is True:
        issues.append(_issue("ERROR", "telegram_dispatch_subprocess_started", "Telegram dispatch started subprocesses"))
    issues.extend(_artifact_safety_issues("telegram_dispatch", payload))
    return issues


def _ai_value_issues(
    payload: Mapping[str, object] | None,
    *,
    present: bool,
    required: bool,
    as_of_date: str,
) -> list[dict[str, object]]:
    if not present or not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if str(payload.get("as_of_date") or "") not in {"", as_of_date}:
        severity = "CRITICAL" if required else "WARNING"
        issues.append(_issue(severity, "ai_value_as_of_date_mismatch", "AI value report date does not match ops date"))
    if status != "AI_VALUE_READY":
        severity = "CRITICAL" if required else "WARNING"
        issues.append(_issue(severity, "ai_value_not_ready", "AI feature value evidence is not ready"))
    issues.extend(_artifact_safety_issues("ai_value_report", payload))
    return issues


def _cross_asset_session_plan_issues(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    summary = _mapping(payload.get("summary"))
    close_required = _int_value(summary.get("close_required_count"), default=0)
    review_count = _int_value(summary.get("review_count"), default=0)
    longer_term = _int_value(summary.get("longer_term_hold_count"), default=0)
    if status == "ERROR":
        issues.append(_issue("ERROR", "cross_asset_session_plan_error", "cross-asset session plan status is ERROR"))
    if status == "CRITICAL" or close_required > 0:
        issues.append(
            _issue(
                "CRITICAL",
                "cross_asset_session_close_required",
                "cross-asset session plan requires closing positions",
            )
        )
    elif status == "WARN" or review_count > 0 or longer_term > 0:
        issues.append(
            _issue("WARNING", "cross_asset_session_review_required", "cross-asset session plan requires review")
        )
    issues.extend(_artifact_safety_issues("cross_asset_session_plan", payload))
    return issues


def _status_issues(
    name: str,
    payload: Mapping[str, object],
    *,
    error_message: str,
    critical_message: str,
    warn_message: str,
) -> list[dict[str, object]]:
    status = str(payload.get("status") or "").upper()
    if status == "ERROR":
        return [_issue("ERROR", f"{name}_error", error_message)]
    if status in {"CRITICAL", "BLOCKED"}:
        return [_issue("CRITICAL", f"{name}_blocked" if status == "BLOCKED" else f"{name}_critical", critical_message)]
    if status == "WARN":
        return [_issue("WARNING", f"{name}_warn", warn_message)]
    return []


def _artifact_safety_issues(name: str, payload: Mapping[str, object]) -> list[dict[str, object]]:
    safety = _mapping(payload.get("safety"))
    if not safety:
        return []
    issues: list[dict[str, object]] = []
    if safety.get("paper_only") is False:
        issues.append(_issue("ERROR", f"{name}_not_paper_only", f"{name} artifact is not paper-only"))
    if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
        issues.append(_issue("ERROR", f"{name}_live_trading_flag", f"{name} artifact has a live trading flag"))
    if safety.get("credentials_read") is True:
        issues.append(_issue("ERROR", f"{name}_credentials_read", f"{name} artifact read credentials"))
    if safety.get("orders_submitted") is True:
        issues.append(_issue("ERROR", f"{name}_orders_submitted", f"{name} artifact submitted orders"))
    return issues


def _ledger_summary(paths: list[Path]) -> tuple[dict[str, object], list[dict[str, object]]]:
    pending = 0
    unmatched = 0
    issues: list[dict[str, object]] = []
    for path in paths:
        if not path.exists():
            issues.append(_issue("WARNING", "missing_ledger", f"ledger input is missing: {path}", source_path=path))
            continue
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                issues.append(
                    _issue(
                        "ERROR",
                        "ledger_invalid_json",
                        f"invalid ledger JSON at line {line_number}: {exc}",
                        source_path=path,
                    )
                )
                continue
            if not isinstance(payload, Mapping) or payload.get("event_type") != "paper_closeout":
                continue
            status = str(payload.get("status") or "").upper()
            if status == "PENDING":
                pending += 1
            elif status == "UNMATCHED":
                unmatched += 1
    if pending:
        issues.append(_issue("CRITICAL", "closeout_pending", "ledger contains pending closeouts"))
    if unmatched:
        issues.append(_issue("CRITICAL", "closeout_unmatched", "ledger contains unmatched closeouts"))
    return {"pending_closeouts": pending, "unmatched_closeouts": unmatched}, issues


def _status_from_issues(issues: Sequence[Mapping[str, object]]) -> str:
    if any(str(issue.get("severity") or "").upper() == "ERROR" for issue in issues):
        return "ERROR"
    if any(str(issue.get("severity") or "").upper() == "CRITICAL" for issue in issues):
        return "CRITICAL"
    if any(str(issue.get("severity") or "").upper() in {"WARNING", "WARN"} for issue in issues):
        return "WARN"
    return "OK"


def _issue(severity: str, code: str, message: str, *, source_path: object = None) -> dict[str, object]:
    payload: dict[str, object] = {"severity": severity, "code": code, "message": message}
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    return payload


def _dedupe_issues(issues: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        normalized = dict(issue)
        key = (
            str(normalized.get("severity") or ""),
            str(normalized.get("code") or ""),
            str(normalized.get("source_path") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _redact_payload(value: object) -> dict[str, object]:
    redacted = redact_payload(value, env={})
    if not isinstance(redacted, dict):
        raise PaperOpsCheckOperationalError("paper ops check must be a JSON object")
    return redacted


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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
