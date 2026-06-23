"""Read-only index of paper operating evidence artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_evidence_index"


class PaperEvidenceIndexOperationalError(RuntimeError):
    """Raised when the paper evidence index cannot be written."""


@dataclass(frozen=True)
class PaperEvidenceIndexResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_evidence_index(
    *,
    as_of_date: str,
    readiness_root: str | Path = "reports/tmp/paper_daily_prepare",
    monitor_root: str | Path = "reports/tmp/paper_monitor",
    campaign_root: str | Path = "reports/tmp/paper_campaign",
    decisions_root: str | Path = "reports/tmp/paper_decisions",
    performance_root: str | Path = "reports/tmp/paper_performance",
    ops_root: str | Path = "reports/tmp/paper_ops_check",
    weekly_root: str | Path = "reports/tmp/paper_weekly_summary",
    statement_root: str | Path = "reports/tmp/paper_statements",
    challenger_decisions_root: str | Path = "reports/tmp/model_challenger_decisions",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperEvidenceIndexResult:
    report = build_paper_evidence_index(
        as_of_date=as_of_date,
        readiness_root=readiness_root,
        monitor_root=monitor_root,
        campaign_root=campaign_root,
        decisions_root=decisions_root,
        performance_root=performance_root,
        ops_root=ops_root,
        weekly_root=weekly_root,
        statement_root=statement_root,
        challenger_decisions_root=challenger_decisions_root,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "evidence_index.json"
    markdown_path = output_root / "evidence_index.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_evidence_index_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperEvidenceIndexResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_evidence_index(
    *,
    as_of_date: str,
    readiness_root: str | Path = "reports/tmp/paper_daily_prepare",
    monitor_root: str | Path = "reports/tmp/paper_monitor",
    campaign_root: str | Path = "reports/tmp/paper_campaign",
    decisions_root: str | Path = "reports/tmp/paper_decisions",
    performance_root: str | Path = "reports/tmp/paper_performance",
    ops_root: str | Path = "reports/tmp/paper_ops_check",
    weekly_root: str | Path = "reports/tmp/paper_weekly_summary",
    statement_root: str | Path = "reports/tmp/paper_statements",
    challenger_decisions_root: str | Path = "reports/tmp/model_challenger_decisions",
    generated_at: str | None = None,
) -> dict[str, object]:
    week = _week_token(_parse_date(as_of_date))
    issues: list[dict[str, object]] = []
    specs = (
        ("readiness", Path(readiness_root), ("readiness.json", "latest.json"), True, as_of_date),
        ("monitor", Path(monitor_root), ("monitor.json", "latest.json"), True, as_of_date),
        ("campaign", Path(campaign_root), ("campaign.json", "latest.json"), True, as_of_date),
        ("decision", Path(decisions_root), ("decision.json", "latest.json"), True, as_of_date),
        ("performance", Path(performance_root), ("performance.json", "latest.json"), False, as_of_date),
        ("ops_check", Path(ops_root), ("ops_check.json", "latest.json"), True, as_of_date),
        ("weekly_summary", Path(weekly_root), ("weekly_summary.json", "latest.json"), False, week),
        ("statement", Path(statement_root), ("statement.normalized.json", "latest.json"), False, as_of_date),
        ("model_review_decision", Path(challenger_decisions_root), ("decision.json", "latest.json"), False, as_of_date),
    )
    artifacts = {
        name: _load_artifact(name, root, filenames=filenames, required=required, token=token, issues=issues)
        for name, root, filenames, required, token in specs
    }
    status = _status_from_issues(issues)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "as_of_date": as_of_date,
        "week": week,
        "status": status,
        "sources": {
            "readiness_root": str(Path(readiness_root)),
            "monitor_root": str(Path(monitor_root)),
            "campaign_root": str(Path(campaign_root)),
            "decisions_root": str(Path(decisions_root)),
            "performance_root": str(Path(performance_root)),
            "ops_root": str(Path(ops_root)),
            "weekly_root": str(Path(weekly_root)),
            "statement_root": str(Path(statement_root)),
            "challenger_decisions_root": str(Path(challenger_decisions_root)),
        },
        "artifacts": artifacts,
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


def render_paper_evidence_index_markdown(report: Mapping[str, object]) -> str:
    artifacts = _mapping(report.get("artifacts"))
    issues = _object_list(report.get("issues"))
    lines = [
        "# Paper Evidence Index",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        f"Week: `{report.get('week') or ''}`",
        "",
        "## Artifacts",
        "",
        "| Artifact | Status | Path |",
        "| --- | --- | --- |",
    ]
    for name, artifact in artifacts.items():
        if not isinstance(artifact, Mapping):
            continue
        lines.append(
            "| "
            f"`{_escape(name)}` "
            f"| `{_escape(artifact.get('status') or '')}` "
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
        lines.append("| OK | none | Evidence index is complete. |")
    lines.extend(["", "Live trading allowed: `False`", "Credentials read: `False`", ""])
    return "\n".join(lines)


def _load_artifact(
    name: str,
    root: Path,
    *,
    filenames: tuple[str, ...],
    required: bool,
    token: str,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    path = _find_artifact_path(root, token=token, filenames=filenames)
    if path is None:
        severity = "ERROR" if required else "WARNING"
        issues.append(_issue(severity, f"missing_{name}", f"{name} artifact is missing"))
        return {"present": False, "status": "MISSING", "path": None, "required": required}
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        severity = "ERROR" if required else "WARNING"
        issues.append(_issue(severity, f"invalid_{name}_json", f"invalid {name} JSON: {exc}", source_path=path))
        return {"present": True, "status": "ERROR", "path": str(path), "required": required}
    return _artifact_summary(path, payload, required=required)


def _find_artifact_path(root: Path, *, token: str, filenames: tuple[str, ...]) -> Path | None:
    if root.is_file():
        return root
    candidates: list[Path] = []
    for filename in filenames:
        candidates.extend([root / token / filename, root / filename])
    latest = root / "latest.json"
    if latest.exists():
        candidates.append(latest)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    if root.exists():
        matched = [path for path in sorted(root.rglob("*.json")) if token in path.parts or token in path.name]
        if matched:
            return matched[0]
    return None


def _artifact_summary(path: Path, payload: Mapping[str, object], *, required: bool) -> dict[str, object]:
    status = str(payload.get("status") or payload.get("state") or payload.get("decision") or "UNKNOWN")
    summary: dict[str, object] = {
        "present": True,
        "required": required,
        "path": str(path),
        "status": status,
        "as_of_date": payload.get("as_of_date"),
        "week": payload.get("week"),
    }
    if payload.get("decision") is not None:
        summary["decision"] = payload.get("decision")
    return summary


def _status_from_issues(issues: list[Mapping[str, object]]) -> str:
    if any(str(issue.get("severity") or "").upper() == "ERROR" for issue in issues):
        return "ERROR"
    if issues:
        return "WARN"
    return "OK"


def _issue(severity: str, code: str, message: str, *, source_path: object = None) -> dict[str, object]:
    payload: dict[str, object] = {"severity": severity, "code": code, "message": message}
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    return payload


def _dedupe_issues(issues: list[Mapping[str, object]]) -> list[dict[str, object]]:
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
        raise PaperEvidenceIndexOperationalError("paper evidence index must be a JSON object")
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


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _week_token(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
