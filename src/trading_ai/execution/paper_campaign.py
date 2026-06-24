"""Read-only paper campaign reporting."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import load_risk_config
from trading_ai.execution.paper_auto_sessions import paper_auto_blockers, summarize_paper_auto_sessions
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_graduation import evaluate_paper_graduation
from trading_ai.execution.paper_monitor import DEFAULT_MIN_STABLE_SESSIONS, build_paper_monitor_dashboard

SCHEMA_VERSION = "1.0"
DEFAULT_SESSIONS_ROOT = "reports/tmp/paper_session"
DEFAULT_READINESS_ROOT = "reports/tmp/paper_daily_prepare"
DEFAULT_DECISIONS_ROOT = "reports/tmp/paper_decisions"
DEFAULT_PERFORMANCE_ROOT = "reports/tmp/paper_performance"
DEFAULT_TRIAL_DAY_ROOT = "reports/tmp/paper_trial_day"
DEFAULT_OUTPUT = "reports/tmp/paper_campaign/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_campaign/latest.md"
LATEST_READINESS_LIMIT = 10
DEFAULT_MIN_TRIAL_DAYS = 30


class PaperCampaignOperationalError(RuntimeError):
    """Raised when the campaign report cannot be produced."""


@dataclass(frozen=True)
class PaperCampaignReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def build_paper_campaign_report(
    *,
    sessions_root: str | Path = DEFAULT_SESSIONS_ROOT,
    readiness_root: str | Path = DEFAULT_READINESS_ROOT,
    decisions_root: str | Path = DEFAULT_DECISIONS_ROOT,
    performance_root: str | Path = DEFAULT_PERFORMANCE_ROOT,
    trial_day_root: str | Path = DEFAULT_TRIAL_DAY_ROOT,
    ledger_inputs: Iterable[str | Path] = (),
    min_paper_auto_clean_sessions: int = 20,
    min_stable_sessions: int = DEFAULT_MIN_STABLE_SESSIONS,
    min_trial_days: int = DEFAULT_MIN_TRIAL_DAYS,
    risk: str | Path = "configs/risk.yml",
    as_of_date: str = "today",
    generated_at: str | None = None,
) -> dict[str, object]:
    """Build a brokerless, notification-free paper campaign report."""

    generated = generated_at or _utc_now()
    ledger_paths = [Path(path) for path in ledger_inputs]
    monitor = build_paper_monitor_dashboard(
        sessions_root=sessions_root,
        session_dirs=(),
        ledger_inputs=ledger_paths,
        as_of_date=as_of_date,
        min_stable_sessions=DEFAULT_MIN_STABLE_SESSIONS,
        broker_read_only=False,
        confirm_paper=False,
        generated_at=generated,
    )
    readiness = _readiness_summary(Path(readiness_root), generated_at=generated)
    decisions = _decisions_summary(Path(decisions_root))
    performance = _performance_summary(Path(performance_root))
    paper_auto = summarize_paper_auto_sessions(
        ledger_paths,
        min_clean_sessions=min_paper_auto_clean_sessions,
    )
    stability_campaign = _stability_campaign_summary(
        ledger_paths,
        min_stable_sessions=min_stable_sessions,
    )
    real_money = _real_money_consideration_summary(Path(trial_day_root), min_trial_days=min_trial_days)
    paper_graduation = evaluate_paper_graduation(
        risk_limits=load_risk_config(risk),
        campaign_report={"real_money_consideration": real_money},
        campaign_report_path=None,
    )
    monitor_blockers = _monitor_blockers(monitor)
    blockers = _dedupe_blockers(
        [
            *monitor_blockers,
            *paper_auto_blockers(paper_auto),
            *_real_money_blockers(real_money, source_path=trial_day_root),
            *_mapping_list(readiness.get("blockers")),
            *_mapping_list(decisions.get("blockers")),
            *_mapping_list(performance.get("blockers")),
        ]
    )
    status = _campaign_status(monitor, blockers)
    progress = _campaign_progress(monitor, blockers, paper_auto=paper_auto)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "status": status,
        "as_of_date": _monitor_as_of_date(monitor),
        "sources": {
            "sessions_root": str(Path(sessions_root)),
            "readiness_root": str(Path(readiness_root)),
            "decisions_root": str(Path(decisions_root)),
            "performance_root": str(Path(performance_root)),
            "trial_day_root": str(Path(trial_day_root)),
            "ledger_inputs": [str(path) for path in ledger_paths],
            "risk": str(Path(risk)),
        },
        "progress": progress,
        "paper_auto_campaign": paper_auto,
        "stability_campaign": stability_campaign,
        "real_money_consideration": real_money,
        "paper_graduation": paper_graduation,
        "readiness": readiness["summary"],
        "decisions": decisions["summary"],
        "performance": performance["summary"],
        "paper_vs_backtest": performance["paper_vs_backtest"],
        "sessions": _session_summary(monitor, progress, paper_auto=paper_auto),
        "blockers": blockers,
        "observability_summary": monitor.get("observability_summary") or {},
        "monitor": {
            "status": monitor.get("status"),
            "monitor_summary": monitor.get("monitor_summary") or {},
            "stability": monitor.get("stability") or {},
            "alerts": monitor.get("alerts") or [],
            "latest_events": monitor.get("latest_events") or [],
        },
        "safety": {
            "broker_client_built": False,
            "broker_snapshot_enabled": False,
            "telegram_enabled": False,
            "credentials_read": False,
            "live_trading_authorized": False,
        },
    }
    return _redact_payload(payload)


def write_paper_campaign_report(
    report: Mapping[str, object],
    *,
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
) -> PaperCampaignReportResult:
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_campaign_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperCampaignReportResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def render_paper_campaign_markdown(report: Mapping[str, object]) -> str:
    progress = _mapping_or_empty(report.get("progress"))
    paper_auto = _mapping_or_empty(report.get("paper_auto_campaign"))
    stability_campaign = _mapping_or_empty(report.get("stability_campaign"))
    real_money = _mapping_or_empty(report.get("real_money_consideration"))
    readiness = _mapping_or_empty(report.get("readiness"))
    decisions = _mapping_or_empty(report.get("decisions"))
    performance = _mapping_or_empty(report.get("performance"))
    gap = _mapping_or_empty(report.get("paper_vs_backtest"))
    sessions = _mapping_or_empty(report.get("sessions"))
    blockers = _object_list(report.get("blockers"))
    graduation = _mapping_or_empty(report.get("paper_graduation"))
    latest_readiness = _object_list(readiness.get("latest"))
    latest_events = _object_list(_mapping_or_empty(report.get("monitor")).get("latest_events"))
    lines = [
        "# Paper Campaign Report",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{report.get('generated_at') or ''}`",
        f"As of date: `{report.get('as_of_date') or ''}`",
        "",
        "## Progress",
        "",
        f"Complete sessions: `{progress.get('complete_sessions', 0)}` / `{progress.get('target_sessions', 0)}`",
        f"Pending sessions: `{progress.get('pending_sessions', 0)}`",
        f"Remaining sessions: `{progress.get('remaining_sessions', 0)}`",
        f"Ready for live review: `{progress.get('ready_for_live_review')}`",
        f"Live trading authorized: `{progress.get('live_trading_authorized')}`",
        "",
        "## Paper Auto Campaign",
        "",
        f"State: `{paper_auto.get('state') or ''}`",
        f"Clean sessions: `{paper_auto.get('clean_sessions', 0)}` / `{paper_auto.get('target_clean_sessions', 0)}`",
        f"Remaining clean sessions: `{paper_auto.get('remaining_clean_sessions', 0)}`",
        f"Next action: `{paper_auto.get('next_action') or ''}`",
        "",
        "## Stability Campaign",
        "",
        f"State: `{stability_campaign.get('state') or ''}`",
        "Clean sessions: "
        f"`{stability_campaign.get('clean_sessions', 0)}` / "
        f"`{stability_campaign.get('target_clean_sessions', 0)}`",
        f"Remaining clean sessions: `{stability_campaign.get('remaining_clean_sessions', 0)}`",
        f"Next action: `{stability_campaign.get('next_action') or ''}`",
        "",
        "## Real Money Consideration",
        "",
        f"State: `{real_money.get('state') or ''}`",
        "Clean trial days: "
        f"`{real_money.get('clean_trial_days', 0)}` / "
        f"`{real_money.get('target_trial_days', 0)}`",
        f"Recovery days: `{real_money.get('recovery_days', 0)}`",
        f"Live trading authorized: `{real_money.get('live_trading_authorized')}`",
        "",
        "## Paper Graduation",
        "",
        f"Stage: `{graduation.get('stage') or ''}`",
        f"Notional: `{graduation.get('paper_notional_usd') or ''}`",
        f"Allowed: `{graduation.get('allowed')}`",
        "",
        "## Readiness",
        "",
        f"Total readiness reports: `{readiness.get('total', 0)}`",
        f"Ready: `{readiness.get('ready', 0)}`",
        f"Blocked: `{readiness.get('blocked', 0)}`",
        f"Errors: `{readiness.get('error', 0)}`",
        f"Latest readiness date: `{readiness.get('latest_as_of_date') or ''}`",
        "",
        "## Latest Decisions",
        "",
        f"Total decisions: `{decisions.get('total', 0)}`",
        f"Latest decision: `{_mapping_or_empty(decisions.get('latest')).get('decision') or ''}`",
        f"Latest decision date: `{_mapping_or_empty(decisions.get('latest')).get('as_of_date') or ''}`",
        "",
        "## Performance",
        "",
        f"Status: `{_mapping_or_empty(performance.get('latest')).get('status') or ''}`",
        "Complete sessions: "
        f"`{_mapping_or_empty(_mapping_or_empty(performance.get('latest')).get('paper_metrics')).get('complete_sessions', 0)}`",  # noqa: E501
        f"Fills: "
        f"`{_mapping_or_empty(_mapping_or_empty(performance.get('latest')).get('paper_metrics')).get('fills', 0)}`",
        f"Backtest available: `{gap.get('backtest_available')}`",
        "",
        "## Sessions",
        "",
        f"Total sessions: `{sessions.get('total', 0)}`",
        f"Complete: `{sessions.get('complete', 0)}`",
        f"Pending: `{sessions.get('pending', 0)}`",
        f"Blocked: `{sessions.get('blocked', 0)}`",
        f"Latest session date: `{sessions.get('latest_session_date') or ''}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Message | Source |",
        "| --- | --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if not isinstance(blocker, Mapping):
                continue
            source = blocker.get("source_path") or blocker.get("session_dir") or blocker.get("event_type") or ""
            lines.append(
                "| "
                f"`{_escape_markdown(blocker.get('severity') or '')}` "
                f"| `{_escape_markdown(blocker.get('code') or '')}` "
                f"| {_escape_markdown(blocker.get('message') or '')} "
                f"| `{_escape_markdown(source)}` |"
            )
    else:
        lines.append("| OK | none | No campaign blockers. |  |")
    lines.extend(
        [
            "",
            "## Latest Readiness",
            "",
            "| Date | Status | Ready | Dataset | Reasons |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if latest_readiness:
        for item in latest_readiness:
            if not isinstance(item, Mapping):
                continue
            dataset = " ".join(
                value
                for value in (
                    str(item.get("dataset_id") or ""),
                    str(item.get("frequency") or ""),
                )
                if value
            )
            lines.append(
                "| "
                f"`{_escape_markdown(item.get('as_of_date') or '')}` "
                f"| `{_escape_markdown(item.get('status') or '')}` "
                f"| `{item.get('ready_for_paper_daily')}` "
                f"| `{_escape_markdown(dataset)}` "
                f"| `{_escape_markdown(', '.join(_string_list(item.get('reasons'))))}` |"
            )
    else:
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Latest Events",
            "",
            "| Time | Type | Status | Symbol | Client order ID |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if latest_events:
        for event in latest_events:
            if not isinstance(event, Mapping):
                continue
            lines.append(
                "| "
                f"`{_escape_markdown(event.get('generated_at') or '')}` "
                f"| `{_escape_markdown(event.get('event_type') or '')}` "
                f"| `{_escape_markdown(event.get('status') or '')}` "
                f"| `{_escape_markdown(event.get('symbol') or '')}` "
                f"| `{_escape_markdown(event.get('client_order_id') or '')}` |"
            )
    else:
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Action Criteria",
            "",
            "- `OK`: continue the daily paper-only flow.",
            "- `WARN`: review campaign warnings before the next paper action.",
            "- `CRITICAL`: stop paper operations until readiness or evidence blockers are resolved.",
            "- `ERROR`: resolve the campaign report operational error before relying on the report.",
            "- Live trading remains outside this campaign report's authority.",
            "",
        ]
    )
    return "\n".join(lines)


def _readiness_summary(root: Path, *, generated_at: str) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    for path in _discover_readiness_paths(root):
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(
                _blocker(
                    severity="CRITICAL",
                    code="readiness_invalid_json",
                    message=str(exc),
                    source_path=path,
                )
            )
            reports.append(
                {
                    "path": str(path),
                    "status": "ERROR",
                    "ready_for_paper_daily": False,
                    "exit_code": 2,
                    "as_of_date": None,
                    "generated_at": generated_at,
                    "reasons": ["readiness_invalid_json"],
                }
            )
            continue
        item = _readiness_item(path, payload)
        reports.append(item)
        if item["ready_for_paper_daily"] is not True or item["status"] != "READY":
            reasons = _string_list(item.get("reasons")) or [f"readiness_{str(item.get('status') or 'unknown').lower()}"]
            for reason in reasons:
                blockers.append(
                    _blocker(
                        severity="CRITICAL",
                        code=reason,
                        message=f"readiness report is not ready: {reason}",
                        source_path=path,
                    )
                )
    latest_reports = sorted(reports, key=_readiness_sort_key, reverse=True)[:LATEST_READINESS_LIMIT]
    summary = {
        "total": len(reports),
        "ready": sum(
            1 for item in reports if item.get("ready_for_paper_daily") is True and item.get("status") == "READY"
        ),
        "blocked": sum(1 for item in reports if str(item.get("status") or "").upper() in {"BLOCKED", "REJECTED"}),
        "error": sum(1 for item in reports if str(item.get("status") or "").upper() == "ERROR"),
        "latest_as_of_date": _latest_readiness_date(reports),
        "latest": latest_reports,
    }
    return {"summary": summary, "blockers": blockers}


def _readiness_item(path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    dataset = _mapping_or_empty(payload.get("approved_dataset"))
    offline_smoke = _mapping_or_empty(payload.get("offline_smoke"))
    return {
        "path": str(path),
        "status": str(payload.get("status") or "UNKNOWN"),
        "ready_for_paper_daily": payload.get("ready_for_paper_daily") is True,
        "exit_code": _int_or_none(payload.get("exit_code")),
        "as_of_date": str(payload.get("as_of_date") or "") or None,
        "generated_at": str(payload.get("generated_at") or ""),
        "dataset_id": dataset.get("dataset_id"),
        "frequency": dataset.get("frequency"),
        "offline_smoke_status": offline_smoke.get("status"),
        "offline_smoke_ran": offline_smoke.get("ran"),
        "reasons": _string_list(payload.get("reasons")),
    }


def _discover_readiness_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name == "readiness.json" else []
    if (root / "readiness.json").exists():
        return [root / "readiness.json"]
    return sorted(root.rglob("readiness.json"))


def _decisions_summary(root: Path) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    for path in _discover_named_json(root, "decision.json"):
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(
                _blocker(
                    severity="WARNING",
                    code="decision_invalid_json",
                    message=str(exc),
                    source_path=path,
                )
            )
            continue
        reports.append(_decision_item(path, payload))
    latest = sorted(reports, key=_generated_sort_key, reverse=True)[:5]
    return {
        "summary": {
            "total": len(reports),
            "latest": latest[0] if latest else None,
            "recent": latest,
        },
        "blockers": blockers,
    }


def _decision_item(path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": str(path),
        "generated_at": str(payload.get("generated_at") or ""),
        "as_of_date": str(payload.get("as_of_date") or ""),
        "decision": str(payload.get("decision") or payload.get("state") or "UNKNOWN"),
        "state": str(payload.get("state") or payload.get("decision") or "UNKNOWN"),
        "operator": payload.get("operator"),
        "reason": payload.get("reason"),
        "live_trading_authorized": _mapping_or_empty(payload.get("safety")).get("live_trading_authorized") is True,
    }


def _performance_summary(root: Path) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    for path in _discover_performance_paths(root):
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(
                _blocker(
                    severity="WARNING",
                    code="performance_invalid_json",
                    message=str(exc),
                    source_path=path,
                )
            )
            continue
        item = dict(payload)
        item["path"] = str(path)
        reports.append(item)
    latest = sorted(reports, key=_generated_sort_key, reverse=True)[:1]
    latest_item = latest[0] if latest else None
    paper_vs_backtest = _mapping_or_empty(_mapping_or_empty(latest_item).get("paper_vs_backtest"))
    return {
        "summary": {
            "total": len(reports),
            "latest": latest_item,
        },
        "paper_vs_backtest": dict(paper_vs_backtest) if paper_vs_backtest else {"backtest_available": False},
        "blockers": blockers,
    }


def _discover_named_json(root: Path, filename: str) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name == filename else []
    if (root / filename).exists():
        return [root / filename]
    return sorted(root.rglob(filename))


def _discover_performance_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    paths = []
    latest = root / "latest.json"
    if latest.exists():
        paths.append(latest)
    paths.extend(path for path in sorted(root.rglob("*.json")) if path not in paths)
    return paths


def _monitor_blockers(monitor: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for alert in _object_list(monitor.get("alerts")):
        if not isinstance(alert, Mapping):
            continue
        code = str(alert.get("code") or "monitor_alert")
        if code == "observability_blocker" and alert.get("reason") not in {None, ""}:
            code = str(alert.get("reason"))
        blockers.append(
            _blocker(
                severity=str(alert.get("severity") or "WARNING"),
                code=code,
                message=str(alert.get("message") or alert.get("code") or "monitor alert"),
                source_path=alert.get("source_path"),
                session_dir=alert.get("session_dir"),
                event_type=alert.get("event_type"),
                extra={
                    key: alert.get(key)
                    for key in ("client_order_id", "symbol", "side", "notional", "reason", "count")
                    if alert.get(key) is not None
                },
            )
        )
    return blockers


def _real_money_consideration_summary(root: Path, *, min_trial_days: int) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for path in _trial_day_paths(root):
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError):
            records.append(
                {
                    "as_of_date": path.parent.name,
                    "trial_state": "ERROR",
                    "path": str(path),
                    "blockers": ["artifact_read_error"],
                }
            )
            continue
        records.append(
            {
                "as_of_date": str(payload.get("as_of_date") or path.parent.name),
                "trial_state": str(payload.get("trial_state") or payload.get("status") or "UNKNOWN"),
                "path": str(path),
                "blockers": _string_list(payload.get("blockers")),
            }
        )
    records.sort(key=lambda item: str(item.get("as_of_date") or ""))
    clean_states = {"TRIAL_DAY_OK", "TRIAL_DAY_WARN"}
    clean = [record for record in records if str(record.get("trial_state") or "").upper() in clean_states]
    recovery = [
        record for record in records if str(record.get("trial_state") or "").upper() in {"RECOVERY_REQUIRED", "ERROR"}
    ]
    if recovery:
        state = "BLOCKED"
        next_action = "resolve_trial_day_recovery"
    elif len(clean) >= min_trial_days:
        state = "PAPER_EVIDENCE_READY"
        next_action = "human_live_readiness_review"
    else:
        state = "ACCUMULATING"
        next_action = "continue_paper_trial"
    return {
        "state": state,
        "target_trial_days": min_trial_days,
        "total_trial_days": len(records),
        "clean_trial_days": len(clean),
        "recovery_days": len(recovery),
        "remaining_trial_days": max(min_trial_days - len(clean), 0),
        "next_action": next_action,
        "latest_trial_day": records[-1] if records else None,
        "recent_trial_days": records[-10:],
        "live_trading_authorized": False,
    }


def _real_money_blockers(summary: Mapping[str, object], *, source_path: object) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for record in _object_list(summary.get("recent_trial_days")):
        if not isinstance(record, Mapping):
            continue
        state = str(record.get("trial_state") or "").upper()
        if state not in {"RECOVERY_REQUIRED", "ERROR"}:
            continue
        codes = _string_list(record.get("blockers")) or [state.lower()]
        for code in codes:
            blockers.append(
                _blocker(
                    severity="CRITICAL",
                    code=f"trial_day_{code}",
                    message=f"trial day recovery required: {code}",
                    source_path=record.get("path") or source_path,
                )
            )
    return blockers


def _trial_day_paths(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    paths = []
    latest = root / "latest.json"
    if latest.exists():
        paths.append(latest)
    paths.extend(path for path in sorted(root.rglob("trial_day.json")) if path not in paths)
    return paths


def _campaign_progress(
    monitor: Mapping[str, object],
    blockers: Sequence[Mapping[str, object]],
    *,
    paper_auto: Mapping[str, object],
) -> dict[str, object]:
    monitor_summary = _mapping_or_empty(monitor.get("monitor_summary"))
    stability = _mapping_or_empty(monitor.get("stability"))
    paper_auto_total = _int_value(paper_auto.get("total_sessions"), default=0)
    if paper_auto_total:
        target_sessions = _int_value(paper_auto.get("target_clean_sessions"), default=20)
        complete_sessions = _int_value(paper_auto.get("clean_sessions"), default=0)
        total_sessions = paper_auto_total
        pending_sessions = _int_value(paper_auto.get("blocked_sessions"), default=0)
    else:
        target_sessions = _int_value(stability.get("min_stable_sessions"), default=DEFAULT_MIN_STABLE_SESSIONS)
        complete_sessions = _int_value(stability.get("stable_session_count"), default=0)
        total_sessions = _int_value(monitor_summary.get("session_count"), default=0)
        pending_sessions = max(total_sessions - complete_sessions, 0)
    critical_blockers = any(str(blocker.get("severity") or "").upper() == "CRITICAL" for blocker in blockers)
    return {
        "target_sessions": target_sessions,
        "complete_sessions": complete_sessions,
        "pending_sessions": pending_sessions,
        "remaining_sessions": max(target_sessions - complete_sessions, 0),
        "ready_for_live_review": bool(stability.get("ready_for_live_review")) and not critical_blockers,
        "live_trading_authorized": False,
    }


def _session_summary(
    monitor: Mapping[str, object],
    progress: Mapping[str, object],
    *,
    paper_auto: Mapping[str, object],
) -> dict[str, object]:
    monitor_summary = _mapping_or_empty(monitor.get("monitor_summary"))
    stability = _mapping_or_empty(monitor.get("stability"))
    paper_auto_total = _int_value(paper_auto.get("total_sessions"), default=0)
    if paper_auto_total:
        return {
            "total": paper_auto_total,
            "complete": progress.get("complete_sessions", 0),
            "pending": progress.get("pending_sessions", 0),
            "blocked": paper_auto.get("blocked_sessions", 0),
            "latest_session_date": paper_auto.get("latest_session_date"),
            "complete_sessions": [
                record
                for record in _object_list(paper_auto.get("records"))
                if isinstance(record, Mapping) and record.get("classification") == "CLEAN"
            ],
            "pending_sessions": [
                record
                for record in _object_list(paper_auto.get("records"))
                if isinstance(record, Mapping) and record.get("classification") != "CLEAN"
            ],
        }
    return {
        "total": monitor_summary.get("session_count", 0),
        "complete": progress.get("complete_sessions", 0),
        "pending": progress.get("pending_sessions", 0),
        "blocked": _mapping_or_empty(monitor.get("observability_summary")).get("sessions_blocked", 0),
        "latest_session_date": monitor_summary.get("latest_session_date"),
        "complete_sessions": _object_list(stability.get("stable_sessions")),
        "pending_sessions": _object_list(stability.get("incomplete_sessions")),
    }


def _stability_campaign_summary(
    ledger_paths: Iterable[str | Path],
    *,
    min_stable_sessions: int,
) -> dict[str, object]:
    summary = summarize_paper_auto_sessions(
        ledger_paths,
        min_clean_sessions=min_stable_sessions,
    )
    histogram = _mapping_or_empty(summary.get("blocker_histogram"))
    payload = dict(summary)
    payload["critical_blockers"] = sorted(str(code) for code in histogram)
    return payload


def _campaign_status(monitor: Mapping[str, object], blockers: Sequence[Mapping[str, object]]) -> str:
    monitor_status = str(monitor.get("status") or "ERROR").upper()
    if monitor_status == "ERROR":
        return "ERROR"
    if any(str(blocker.get("severity") or "").upper() == "CRITICAL" for blocker in blockers):
        return "CRITICAL"
    if monitor_status in {"CRITICAL", "WARN"}:
        return monitor_status
    if any(str(blocker.get("severity") or "").upper() == "WARNING" for blocker in blockers):
        return "WARN"
    return "OK"


def _dedupe_blockers(blockers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for blocker in blockers:
        normalized = dict(blocker)
        key = (
            str(normalized.get("severity") or ""),
            str(normalized.get("code") or ""),
            str(normalized.get("source_path") or ""),
            str(normalized.get("session_dir") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _blocker(
    *,
    severity: str,
    code: str,
    message: str,
    source_path: object = None,
    session_dir: object = None,
    event_type: object = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    blocker: dict[str, object] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if source_path not in {None, ""}:
        blocker["source_path"] = str(source_path)
    if session_dir not in {None, ""}:
        blocker["session_dir"] = str(session_dir)
    if event_type not in {None, ""}:
        blocker["event_type"] = str(event_type)
    for key, value in (extra or {}).items():
        if value is not None:
            blocker[str(key)] = value
    return blocker


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperCampaignOperationalError("campaign report must be a JSON object")
    return redacted


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value


def _latest_readiness_date(reports: Iterable[Mapping[str, object]]) -> str | None:
    dates = [str(item.get("as_of_date")) for item in reports if item.get("as_of_date")]
    return max(dates) if dates else None


def _readiness_sort_key(item: Mapping[str, object]) -> tuple[str, str]:
    return (str(item.get("as_of_date") or ""), str(item.get("generated_at") or ""))


def _generated_sort_key(item: Mapping[str, object]) -> tuple[str, str]:
    return (str(item.get("generated_at") or ""), str(item.get("path") or ""))


def _monitor_as_of_date(monitor: Mapping[str, object]) -> str | None:
    return str(_mapping_or_empty(monitor.get("monitor_summary")).get("as_of_date") or "") or None


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    return [item for item in _object_list(value) if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _int_or_none(value: object) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _int_value(value: object, *, default: int) -> int:
    if value in {None, ""}:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
