"""Read-only daily paper operations completeness check."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from trading_ai.execution.paper_common import (
    paper_exit_code,
    redact_secrets,
    read_json_artifact,
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

    issues = [*issue_list]
    issues.extend(_readiness_issues(readiness.payload))
    issues.extend(_monitor_issues(monitor.payload))
    issues.extend(_campaign_issues(campaign.payload))
    issues.extend(_decision_issues(decision.payload))
    issues.extend(_performance_issues(performance.payload, present=performance.present))
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
            "ledger_inputs": [str(path) for path in ledger_inputs],
        },
        "artifacts": {
            "readiness": readiness.summary,
            "monitor": monitor.summary,
            "campaign": campaign.summary,
            "decision": decision.summary,
            "performance": performance.summary,
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
        lines.append(
            "| "
            f"`{_escape(name)}` "
            f"| `{_escape(artifact_status)}` "
            f"| `{_escape(artifact.get('path') or '')}` |"
        )
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
    elif status == "CRITICAL" or int(summary.get("critical_count") or 0) > 0:
        issues.append(_issue("CRITICAL", "monitor_critical", "monitor has critical alerts"))
    elif status == "WARN" or int(summary.get("warning_count") or 0) > 0:
        issues.append(_issue("WARNING", "monitor_warn", "monitor has warnings"))
    if int(summary.get("pending_closeout_count") or 0) > 0:
        issues.append(_issue("CRITICAL", "closeout_pending", "monitor reports pending closeouts"))
    if int(summary.get("unmatched_closeout_count") or 0) > 0:
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
    if int(metrics.get("pending_closeouts") or 0) > 0:
        issues.append(_issue("CRITICAL", "closeout_pending", "performance reports pending closeouts"))
    if int(metrics.get("unmatched_closeouts") or 0) > 0:
        issues.append(_issue("CRITICAL", "closeout_unmatched", "performance reports unmatched closeouts"))
    if statement and str(statement.get("status") or "").upper() in {"NOT_REQUESTED", "MISSING"}:
        issues.append(_issue("WARNING", "statement_absent", "broker statement was not matched"))
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
                issues.append(_issue("ERROR", "ledger_invalid_json", f"invalid ledger JSON at line {line_number}: {exc}", source_path=path))
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


def _status_from_issues(issues: list[Mapping[str, object]]) -> str:
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
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperOpsCheckOperationalError("paper ops check must be a JSON object")
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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
