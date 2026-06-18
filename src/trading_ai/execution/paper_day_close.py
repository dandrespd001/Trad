"""Auditable end-of-day paper decision report."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from trading_ai.execution.paper_common import read_json_artifact, redact_secrets, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_decisions"
DECISION_CONTINUE = "CONTINUE"
DECISION_STOP = "STOP"
DECISION_REVIEW = "REVIEW"
DECISION_ERROR = "ERROR"


class PaperDayCloseOperationalError(RuntimeError):
    """Raised when the paper day close report cannot be written."""


@dataclass(frozen=True)
class PaperDayCloseResult:
    exit_code: int
    decision: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_day_close(
    *,
    readiness: str | Path,
    broker_run: str | Path,
    monitor: str | Path,
    campaign_report: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    as_of_date: str = "auto",
    operator: str | None = None,
    reason: str | None = None,
    ledger_output: str | Path | None = None,
    generated_at: str | None = None,
) -> PaperDayCloseResult:
    """Close one paper day with an append-only, redacted decision artifact."""

    generated = generated_at or _utc_now()
    artifact_specs = {
        "readiness": Path(readiness),
        "broker_run": Path(broker_run),
        "monitor": Path(monitor),
        "campaign_report": Path(campaign_report),
    }
    payloads: dict[str, dict[str, object]] = {}
    blockers: list[dict[str, object]] = []
    artifacts: dict[str, dict[str, object]] = {}

    for name, path in artifact_specs.items():
        artifact = _artifact_summary(path)
        try:
            loaded = read_json_artifact(path)
        except FileNotFoundError:
            blockers.append(_blocker("ERROR", "missing_artifact", f"required artifact is missing: {path}", path))
            artifact["status"] = "ERROR"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(_blocker("ERROR", "invalid_json", f"invalid JSON in {path}: {exc}", path))
            artifact["status"] = "ERROR"
        else:
            payloads[name] = loaded
            artifact["status"] = str(loaded.get("status") or "UNKNOWN")
            if loaded.get("exit_code") is not None:
                artifact["exit_code"] = loaded.get("exit_code")
        artifacts[name] = artifact

    resolved_as_of_date = _resolve_as_of_date(
        as_of_date,
        readiness=payloads.get("readiness"),
        broker_run=payloads.get("broker_run"),
        monitor=payloads.get("monitor"),
        campaign_report=payloads.get("campaign_report"),
    )
    if blockers:
        decision = DECISION_ERROR
        exit_code = 2
    else:
        decision, blockers = _evaluate_decision(
            readiness=payloads["readiness"],
            broker_run=payloads["broker_run"],
            monitor=payloads["monitor"],
            campaign_report=payloads["campaign_report"],
        )
        exit_code = _exit_code_for_decision(decision)

    output_root = Path(output_dir) / resolved_as_of_date
    output_path = output_root / "decision.json"
    markdown_path = output_root / "decision.md"
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": resolved_as_of_date,
        "decision": decision,
        "state": decision,
        "exit_code": exit_code,
        "operator": operator,
        "reason": reason or _default_reason(decision),
        "artifacts": artifacts,
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
            "broker_client_built": False,
            "orders_submitted": False,
        },
    }
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_day_close_markdown(redacted), markdown_path)
    if ledger_output:
        _append_decision_ledger(ledger_output, redacted, output_path=output_path)
    return PaperDayCloseResult(
        exit_code=exit_code,
        decision=decision,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def render_paper_day_close_markdown(report: Mapping[str, object]) -> str:
    artifacts = _mapping(report.get("artifacts"))
    blockers = _list(report.get("blockers"))
    lines = [
        "# Paper Day Close",
        "",
        f"Decision: **{report.get('decision') or 'UNKNOWN'}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        f"Operator: `{report.get('operator') or ''}`",
        f"Reason: {report.get('reason') or ''}",
        "",
        "## Artifacts",
        "",
        "| Name | Status | SHA-256 | Path |",
        "| --- | --- | --- | --- |",
    ]
    for name, artifact in artifacts.items():
        if not isinstance(artifact, Mapping):
            continue
        lines.append(
            "| "
            f"`{_escape(name)}` "
            f"| `{_escape(artifact.get('status') or '')}` "
            f"| `{_escape(artifact.get('sha256') or '')}` "
            f"| `{_escape(artifact.get('path') or '')}` |"
        )
    lines.extend(["", "## Blockers", "", "| Severity | Code | Message |", "| --- | --- | --- |"])
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(blocker.get('severity') or '')}` "
                    f"| `{_escape(blocker.get('code') or '')}` "
                    f"| {_escape(blocker.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | No blockers. |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            f"Live trading authorized: `{_mapping(report.get('safety')).get('live_trading_authorized')}`",
            f"Live trading allowed: `{_mapping(report.get('safety')).get('live_trading_allowed')}`",
            "",
        ]
    )
    return "\n".join(lines)


def _evaluate_decision(
    *,
    readiness: Mapping[str, object],
    broker_run: Mapping[str, object],
    monitor: Mapping[str, object],
    campaign_report: Mapping[str, object],
) -> tuple[str, list[dict[str, object]]]:
    blockers: list[dict[str, object]] = []
    readiness_ready = readiness.get("status") == "READY" and readiness.get("ready_for_paper_daily") is True
    if not readiness_ready:
        blockers.append(_blocker("CRITICAL", "readiness_not_ready", "readiness is not ready for paper daily"))
    broker_status = _status(broker_run)
    monitor_status = _status(monitor)
    campaign_status = _status(campaign_report)
    if _int_or_none(broker_run.get("exit_code")) == 2 or broker_status == "ERROR":
        blockers.append(_blocker("ERROR", "broker_run_error", "broker run ended with an operational error"))
    if monitor_status == "ERROR":
        blockers.append(_blocker("ERROR", "monitor_error", "monitor ended with an operational error"))
    if campaign_status == "ERROR":
        blockers.append(_blocker("ERROR", "campaign_error", "campaign report ended with an operational error"))
    blockers.extend(_monitor_alert_blockers(monitor))
    blockers.extend(_campaign_blockers(campaign_report))

    if any(blocker["severity"] == "ERROR" for blocker in blockers):
        return DECISION_ERROR, blockers
    if broker_status in {"BLOCKED", "CRITICAL"} or monitor_status == "CRITICAL" or campaign_status == "CRITICAL":
        return DECISION_STOP, blockers
    if any(blocker["severity"] == "CRITICAL" for blocker in blockers):
        return DECISION_STOP, blockers
    if broker_status == "WARN" or monitor_status == "WARN" or campaign_status == "WARN":
        return DECISION_REVIEW, blockers
    if any(blocker["severity"] == "WARNING" for blocker in blockers):
        return DECISION_REVIEW, blockers
    return DECISION_CONTINUE, blockers


def _monitor_alert_blockers(monitor: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for alert in _list(monitor.get("alerts")):
        if not isinstance(alert, Mapping):
            continue
        severity = str(alert.get("severity") or "WARNING").upper()
        if severity not in {"ERROR", "CRITICAL", "WARNING"}:
            severity = "WARNING"
        blockers.append(
            _blocker(
                severity,
                str(alert.get("code") or "monitor_alert"),
                str(alert.get("message") or alert.get("code") or "monitor alert"),
                alert.get("source_path"),
            )
        )
    return blockers


def _campaign_blockers(campaign: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for blocker in _list(campaign.get("blockers")):
        if not isinstance(blocker, Mapping):
            continue
        severity = str(blocker.get("severity") or "WARNING").upper()
        if severity not in {"ERROR", "CRITICAL", "WARNING"}:
            severity = "WARNING"
        blockers.append(
            _blocker(
                severity,
                str(blocker.get("code") or "campaign_blocker"),
                str(blocker.get("message") or blocker.get("code") or "campaign blocker"),
                blocker.get("source_path"),
            )
        )
    return blockers


def _resolve_as_of_date(
    value: str,
    *,
    readiness: Mapping[str, object] | None,
    broker_run: Mapping[str, object] | None,
    monitor: Mapping[str, object] | None,
    campaign_report: Mapping[str, object] | None,
) -> str:
    if value != "auto":
        return _date_token(value)
    candidates = [
        readiness.get("as_of_date") if readiness else None,
        broker_run.get("as_of_date") if broker_run else None,
        _mapping(monitor.get("monitor_summary")).get("as_of_date") if monitor else None,
        campaign_report.get("as_of_date") if campaign_report else None,
    ]
    for candidate in candidates:
        if candidate not in {None, ""}:
            return _date_token(str(candidate))
    return date.today().isoformat()


def _artifact_summary(path: Path) -> dict[str, object]:
    payload: dict[str, object] = {"path": str(path), "sha256": None}
    if path.exists() and path.is_file():
        payload["sha256"] = _file_sha256(path)
    return payload


def _append_decision_ledger(ledger_output: str | Path, report: Mapping[str, object], *, output_path: Path) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "paper_day_decision",
        "generated_at": report.get("generated_at"),
        "status": report.get("decision"),
        "exit_code": report.get("exit_code"),
        "as_of_date": report.get("as_of_date"),
        "output_path": str(output_path),
        "reasons": [report.get("reason")] if report.get("reason") else [],
    }
    path = Path(ledger_output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_redact_payload(event), sort_keys=True) + "\n")


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperDayCloseOperationalError("paper day close report must be a JSON object")
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


def _blocker(severity: str, code: str, message: str, source_path: object = None) -> dict[str, object]:
    payload: dict[str, object] = {"severity": severity, "code": code, "message": message}
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    return payload


def _default_reason(decision: str) -> str:
    if decision == DECISION_CONTINUE:
        return "paper evidence supports continuing the paper-only campaign"
    if decision == DECISION_REVIEW:
        return "paper evidence has warnings that require review"
    if decision == DECISION_STOP:
        return "paper evidence has blockers that require stopping paper operations"
    return "paper day close encountered an operational error"


def _exit_code_for_decision(decision: str) -> int:
    if decision == DECISION_ERROR:
        return 2
    if decision == DECISION_STOP:
        return 1
    return 0


def _status(payload: Mapping[str, object]) -> str:
    return str(payload.get("status") or "UNKNOWN").upper()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _date_token(value: str) -> str:
    return value.replace("/", "-").replace(" ", "_")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
