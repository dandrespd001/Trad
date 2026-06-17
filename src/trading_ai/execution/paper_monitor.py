"""Offline monitor dashboard and alerts for paper trading evidence."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from trading_ai.execution.paper_observability import (
    PaperObservabilityReport,
    append_paper_ledger_event,
    build_paper_observability_report,
)


SCHEMA_VERSION = "1.0"
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
RECENT_SESSION_MAX_AGE_DAYS = 1


class PaperMonitorOperationalError(RuntimeError):
    """Raised when monitor artifacts or notification outputs cannot be produced."""


@dataclass(frozen=True)
class TelegramSendResult:
    sent: bool
    status: str
    reason: str | None = None
    http_status: int | None = None

    def to_artifact(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": self.status, "sent": self.sent}
        if self.reason:
            payload["reason"] = self.reason
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload


@dataclass(frozen=True)
class PaperMonitorResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    dashboard: dict[str, object]


def run_paper_monitor(
    *,
    sessions_root: str | Path = "reports/tmp/paper_session",
    session_dirs: Iterable[str | Path] = (),
    ledger_inputs: Iterable[str | Path] = (),
    output: str | Path = "reports/tmp/paper_monitor/latest.json",
    markdown_output: str | Path = "reports/tmp/paper_monitor/latest.md",
    as_of_date: str | date = "today",
    ledger_output: str | Path | None = None,
    send_telegram: bool = False,
    telegram_dry_run: bool = False,
    telegram_send_warnings: bool = False,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> PaperMonitorResult:
    dashboard = build_paper_monitor_dashboard(
        sessions_root=sessions_root,
        session_dirs=session_dirs,
        ledger_inputs=ledger_inputs,
        as_of_date=as_of_date,
        generated_at=generated_at,
    )

    notification = _telegram_notification_artifact(
        dashboard,
        enabled=send_telegram,
        dry_run=telegram_dry_run,
        send_warnings=telegram_send_warnings,
    )
    if notification is not None:
        dashboard["telegram"] = notification

    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_paper_monitor_dashboard(dashboard, output=output_path, markdown_output=markdown_path)

    operational_exit_code = None
    if send_telegram and not telegram_dry_run and notification is not None and notification.get("would_send") is True:
        text = str(notification.get("message_preview") or "")
        result = send_paper_monitor_telegram(text, env=env)
        telegram_artifact = {
            **notification,
            **result.to_artifact(),
            "credential_source": "process_environment",
        }
        dashboard["telegram"] = telegram_artifact
        write_paper_monitor_dashboard(dashboard, output=output_path, markdown_output=markdown_path)
        if not result.sent:
            operational_exit_code = 2

    exit_code = operational_exit_code if operational_exit_code is not None else _exit_code_for_status(str(dashboard["status"]))
    if ledger_output:
        append_paper_ledger_event(
            ledger_output,
            _paper_monitor_ledger_event(dashboard, exit_code=exit_code, output_path=output_path),
        )
    return PaperMonitorResult(
        exit_code=exit_code,
        status=str(dashboard["status"]),
        output_path=output_path,
        markdown_path=markdown_path,
        dashboard=dashboard,
    )


def build_paper_monitor_dashboard(
    *,
    sessions_root: str | Path = "reports/tmp/paper_session",
    session_dirs: Iterable[str | Path] = (),
    ledger_inputs: Iterable[str | Path] = (),
    as_of_date: str | date = "today",
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    resolved_as_of_date = _resolve_as_of_date(as_of_date)
    observability = build_paper_observability_report(
        sessions_root=sessions_root,
        session_dirs=session_dirs,
        ledger_inputs=ledger_inputs,
        generated_at=generated,
    )
    alerts = _build_alerts(observability, as_of_date=resolved_as_of_date)
    status = _status_from_alerts(alerts)
    summary = _build_monitor_summary(observability, alerts, as_of_date=resolved_as_of_date)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "status": status,
        "sources": {
            **dict(observability.sources),
            "as_of_date": resolved_as_of_date.isoformat(),
        },
        "observability_summary": dict(observability.summary),
        "monitor_summary": summary,
        "alerts": alerts,
        "latest_events": list(observability.summary.get("latest_events") or []),
    }


def write_paper_monitor_dashboard(
    dashboard: Mapping[str, object],
    *,
    output: str | Path,
    markdown_output: str | Path,
) -> None:
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(dict(dashboard), indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_paper_monitor_markdown(dashboard), encoding="utf-8")


def render_paper_monitor_markdown(dashboard: Mapping[str, object]) -> str:
    summary = _mapping_or_empty(dashboard.get("monitor_summary"))
    alerts = _object_list(dashboard.get("alerts"))
    latest_events = _object_list(dashboard.get("latest_events"))
    lines = [
        "# Paper Monitor",
        "",
        f"Status: **{dashboard.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{dashboard.get('generated_at') or ''}`",
        f"As of date: `{summary.get('as_of_date') or ''}`",
        (
            "Alerts: "
            f"`{summary.get('critical_count', 0)}` critical, "
            f"`{summary.get('warning_count', 0)}` warning"
        ),
        f"Action: `{summary.get('action_required') or ''}`",
        "",
        "## Alerts",
        "",
        "| Severity | Code | Message | Source |",
        "| --- | --- | --- | --- |",
    ]
    if alerts:
        for alert in alerts:
            if not isinstance(alert, Mapping):
                continue
            source = alert.get("source_path") or alert.get("session_dir") or alert.get("event_type") or ""
            lines.append(
                "| "
                f"`{_escape_markdown(alert.get('severity') or '')}` "
                f"| `{_escape_markdown(alert.get('code') or '')}` "
                f"| {_escape_markdown(alert.get('message') or '')} "
                f"| `{_escape_markdown(source)}` |"
            )
    else:
        lines.append("| OK | none | No monitor alerts. |  |")
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
            "- `WARN`: review monitor warnings before the next paper action.",
            "- `CRITICAL`: stop paper operations until the evidence gap or blocker is resolved.",
            "",
        ]
    )
    telegram = _mapping_or_empty(dashboard.get("telegram"))
    if telegram:
        lines.extend(
            [
                "## Telegram",
                "",
                f"Status: `{telegram.get('status') or ''}`",
                f"Would send: `{telegram.get('would_send')}`",
                f"Sent: `{telegram.get('sent')}`",
                f"Reason: `{telegram.get('reason') or ''}`",
                "",
            ]
        )
    return "\n".join(lines)


def send_paper_monitor_telegram(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> TelegramSendResult:
    source = os.environ if env is None else env
    token = source.get("TELEGRAM_BOT_TOKEN")
    chat_id = source.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return TelegramSendResult(sent=False, status="FAILED", reason="missing_telegram_credentials")

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": _truncate_message(text)}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            http_status = _response_status(response)
            body = response.read(2048)
    except urllib.error.HTTPError as exc:
        return TelegramSendResult(
            sent=False,
            status="FAILED",
            reason=f"telegram_http_error_{exc.code}",
            http_status=exc.code,
        )
    except urllib.error.URLError:
        return TelegramSendResult(sent=False, status="FAILED", reason="telegram_network_error")
    except Exception as exc:  # pragma: no cover - defensive notification boundary
        return TelegramSendResult(
            sent=False,
            status="FAILED",
            reason=_redact_text(f"telegram_send_error_{type(exc).__name__}", token=token),
        )

    ok, error_code, description = _telegram_response_status(body, token=token)
    if not ok:
        reason = f"telegram_api_error_{error_code}" if error_code is not None else "telegram_api_error"
        if description:
            reason = f"{reason}: {_redact_text(description, token=token)[:160]}"
        return TelegramSendResult(sent=False, status="FAILED", reason=reason, http_status=http_status)
    return TelegramSendResult(sent=True, status="SENT", http_status=http_status)


def _build_alerts(
    observability: PaperObservabilityReport,
    *,
    as_of_date: date,
) -> list[dict[str, object]]:
    events = list(observability.events)
    alerts: list[dict[str, object]] = []

    for diagnostic in observability.diagnostics:
        reason = _first_string(diagnostic.get("reason"), diagnostic.get("reasons"))
        severity = "WARNING" if reason == "missing_ledger" else "CRITICAL"
        code = "ledger_missing" if reason == "missing_ledger" else "observability_diagnostic"
        alerts.append(
            _alert(
                severity=severity,
                code=code,
                message=str(diagnostic.get("message") or reason or "observability diagnostic"),
                event=diagnostic,
                reason=reason,
            )
        )

    for event in events:
        event_type = str(event.get("event_type") or "")
        status = str(event.get("status") or "").upper()
        if event_type == "paper_session" and status in {"BLOCKED", "ERROR"}:
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    code="paper_session_blocked",
                    message="paper session is blocked",
                    event=event,
                )
            )
        elif event_type == "paper_execution" and status in {"BLOCKED", "ERROR"}:
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    code="paper_execution_blocked",
                    message="paper execution is blocked",
                    event=event,
                )
            )
        elif event_type == "paper_closeout" and status in {"PENDING", "UNMATCHED"}:
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    code=f"paper_closeout_{status.lower()}",
                    message=f"paper closeout is {status.lower()}",
                    event=event,
                )
            )

    closeouts = [event for event in events if event.get("event_type") == "paper_closeout"]
    for execution in events:
        if execution.get("event_type") != "paper_execution":
            continue
        if str(execution.get("status") or "").upper() != "SUBMITTED":
            continue
        if not _has_matching_event(execution, closeouts):
            alerts.append(
                _alert(
                    severity="CRITICAL",
                    code="paper_execution_without_closeout",
                    message="submitted paper execution has no closeout evidence",
                    event=execution,
                )
            )

    blockers = _mapping_or_empty(observability.summary.get("blockers"))
    for reason, count in sorted(blockers.items(), key=lambda item: str(item[0])):
        if str(reason) == "missing_ledger":
            continue
        alerts.append(
            _alert(
                severity="CRITICAL",
                code="observability_blocker",
                message=f"observability blocker `{reason}` appeared {count} time(s)",
                reason=str(reason),
                count=count,
            )
        )

    session_events = [event for event in events if event.get("event_type") == "paper_session"]
    latest_session_date = _latest_event_date(session_events)
    if not session_events:
        alerts.append(
            _alert(
                severity="WARNING",
                code="no_sessions",
                message="no paper sessions were found",
            )
        )
    elif latest_session_date is None or (as_of_date - latest_session_date).days > RECENT_SESSION_MAX_AGE_DAYS:
        alerts.append(
            _alert(
                severity="WARNING",
                code="no_recent_sessions",
                message="no recent paper session was found for the monitor date",
                extra={"latest_session_date": latest_session_date.isoformat() if latest_session_date else None},
            )
        )

    executions = [event for event in events if event.get("event_type") == "paper_execution"]
    for session in session_events:
        if str(session.get("status") or "").upper() != "READY":
            continue
        if not _has_matching_event(session, executions):
            alerts.append(
                _alert(
                    severity="WARNING",
                    code="missing_execution_evidence",
                    message="ready paper session has no execution evidence",
                    event=session,
                )
            )

    return _dedupe_alerts(alerts)


def _build_monitor_summary(
    observability: PaperObservabilityReport,
    alerts: list[dict[str, object]],
    *,
    as_of_date: date,
) -> dict[str, object]:
    events = list(observability.events)
    critical_count = sum(1 for alert in alerts if alert.get("severity") == "CRITICAL")
    warning_count = sum(1 for alert in alerts if alert.get("severity") == "WARNING")
    session_events = [event for event in events if event.get("event_type") == "paper_session"]
    latest_session_date = _latest_event_date(session_events)
    status = _status_from_alerts(alerts)
    return {
        "as_of_date": as_of_date.isoformat(),
        "status": status,
        "alert_count": len(alerts),
        "critical_count": critical_count,
        "warning_count": warning_count,
        "session_count": len(session_events),
        "latest_session_date": latest_session_date.isoformat() if latest_session_date is not None else None,
        "submitted_execution_count": _count_events(events, "paper_execution", {"SUBMITTED"}),
        "blocked_execution_count": _count_events(events, "paper_execution", {"BLOCKED", "ERROR"}),
        "closed_closeout_count": _count_events(events, "paper_closeout", {"CLOSED"}),
        "pending_closeout_count": _count_events(events, "paper_closeout", {"PENDING"}),
        "unmatched_closeout_count": _count_events(events, "paper_closeout", {"UNMATCHED"}),
        "action_required": _action_for_status(status),
    }


def _telegram_notification_artifact(
    dashboard: Mapping[str, object],
    *,
    enabled: bool,
    dry_run: bool,
    send_warnings: bool,
) -> dict[str, object] | None:
    if not enabled and not dry_run:
        return None
    alerts = [
        alert
        for alert in _object_list(dashboard.get("alerts"))
        if isinstance(alert, Mapping)
        and (alert.get("severity") == "CRITICAL" or (send_warnings and alert.get("severity") == "WARNING"))
    ]
    would_send = bool(alerts)
    message = _telegram_message(dashboard, alerts) if would_send else ""
    artifact: dict[str, object] = {
        "enabled": enabled,
        "dry_run": dry_run,
        "send_warnings": send_warnings,
        "would_send": would_send,
        "sent": False,
        "status": "DRY_RUN" if dry_run else ("PENDING" if would_send else "SKIPPED"),
        "credential_source": "not_read_in_dry_run" if dry_run else "process_environment",
        "message_preview": message,
    }
    if not would_send:
        artifact["reason"] = "no_eligible_alerts"
    return artifact


def _telegram_message(dashboard: Mapping[str, object], alerts: list[Mapping[str, object]]) -> str:
    summary = _mapping_or_empty(dashboard.get("monitor_summary"))
    lines = [
        f"Paper monitor {dashboard.get('status') or 'UNKNOWN'}",
        f"Generated: {dashboard.get('generated_at') or ''}",
        f"As of: {summary.get('as_of_date') or ''}",
        f"Critical: {summary.get('critical_count', 0)} Warning: {summary.get('warning_count', 0)}",
        f"Action: {summary.get('action_required') or ''}",
    ]
    if alerts:
        lines.append("Alerts:")
        for alert in alerts[:8]:
            lines.append(f"- {alert.get('severity')}: {alert.get('code')} - {alert.get('message')}")
        if len(alerts) > 8:
            lines.append(f"- plus {len(alerts) - 8} more alert(s)")
    return _truncate_message("\n".join(lines))


def _paper_monitor_ledger_event(
    dashboard: Mapping[str, object],
    *,
    exit_code: int,
    output_path: Path,
) -> dict[str, object]:
    alerts = _object_list(dashboard.get("alerts"))
    critical_reasons = [
        str(alert.get("code"))
        for alert in alerts
        if isinstance(alert, Mapping) and alert.get("severity") == "CRITICAL" and alert.get("code")
    ]
    warning_reasons = [
        str(alert.get("code"))
        for alert in alerts
        if isinstance(alert, Mapping) and alert.get("severity") == "WARNING" and alert.get("code")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": "paper_monitor",
        "generated_at": str(dashboard.get("generated_at") or _utc_now()),
        "status": str(dashboard.get("status") or "UNKNOWN"),
        "exit_code": exit_code,
        "output_path": str(output_path),
        "reasons": _dedupe_strings([*critical_reasons, *warning_reasons]),
    }


def _status_from_alerts(alerts: list[Mapping[str, object]]) -> str:
    if any(alert.get("severity") == "CRITICAL" for alert in alerts):
        return "CRITICAL"
    if any(alert.get("severity") == "WARNING" for alert in alerts):
        return "WARN"
    return "OK"


def _exit_code_for_status(status: str) -> int:
    return 1 if status.upper() == "CRITICAL" else 0


def _action_for_status(status: str) -> str:
    if status == "CRITICAL":
        return "stop_paper_operations"
    if status == "WARN":
        return "review_warnings"
    return "continue_daily_flow"


def _alert(
    *,
    severity: str,
    code: str,
    message: str,
    event: Mapping[str, object] | None = None,
    reason: str | None = None,
    count: object = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    alert: dict[str, object] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if reason:
        alert["reason"] = reason
    if count is not None:
        alert["count"] = count
    if event is not None:
        for key in (
            "event_type",
            "status",
            "session_dir",
            "source_path",
            "output_path",
            "client_order_id",
            "symbol",
            "side",
            "notional",
            "reasons",
            "finding_codes",
        ):
            if key in event:
                alert[key] = event[key]
    for key, value in (extra or {}).items():
        if value is not None:
            alert[str(key)] = value
    return alert


def _dedupe_alerts(alerts: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for alert in alerts:
        key = (
            str(alert.get("severity") or ""),
            str(alert.get("code") or ""),
            str(alert.get("reason") or ""),
            str(alert.get("source_path") or ""),
            str(alert.get("session_dir") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(alert)
    return result


def _has_matching_event(event: Mapping[str, object], candidates: Iterable[Mapping[str, object]]) -> bool:
    session_dir = event.get("session_dir")
    client_order_id = event.get("client_order_id")
    for candidate in candidates:
        if session_dir and candidate.get("session_dir") == session_dir:
            return True
        if client_order_id and candidate.get("client_order_id") == client_order_id:
            return True
    return False


def _latest_event_date(events: Iterable[Mapping[str, object]]) -> date | None:
    parsed = [_parse_event_date(event.get("generated_at")) for event in events]
    dates = [value for value in parsed if value is not None]
    return max(dates) if dates else None


def _parse_event_date(value: object) -> date | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _count_events(events: Iterable[Mapping[str, object]], event_type: str, statuses: set[str]) -> int:
    return sum(
        1
        for event in events
        if event.get("event_type") == event_type and str(event.get("status") or "").upper() in statuses
    )


def _telegram_response_status(body: bytes, *, token: str) -> tuple[bool, int | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None, "invalid Telegram JSON response"
    if not isinstance(payload, Mapping):
        return False, None, "invalid Telegram response object"
    ok = payload.get("ok") is True
    error_code = _int_or_none(payload.get("error_code"))
    description = payload.get("description")
    return ok, error_code, _redact_text(str(description), token=token) if description else None


def _response_status(response: object) -> int | None:
    status = getattr(response, "status", None)
    if status is not None:
        return _int_or_none(status)
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        return _int_or_none(getcode())
    return None


def _truncate_message(text: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    return text[: TELEGRAM_MESSAGE_LIMIT - 20] + "\n[truncated]"


def _redact_text(text: str, *, token: str | None) -> str:
    redacted = text
    if token:
        redacted = redacted.replace(token, "[redacted-token]")
    return re.sub(r"bot[^/\s]+/sendMessage", "bot[redacted]/sendMessage", redacted)


def _resolve_as_of_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    if value == "today":
        return date.today()
    return date.fromisoformat(value)


def _first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                if item not in {None, ""}:
                    return str(item)
    return None


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


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


def _int_or_none(value: object) -> int | None:
    if value in {None, ""} or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
