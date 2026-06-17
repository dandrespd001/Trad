"""Offline observability for paper-session and paper-order evidence."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA_VERSION = "1.0"
LATEST_EVENT_LIMIT = 10


@dataclass(frozen=True)
class PaperObservabilityReport:
    schema_version: str
    generated_at: str
    sources: dict[str, object]
    summary: dict[str, object]
    events: tuple[dict[str, object], ...]
    diagnostics: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sources": dict(self.sources),
            "summary": dict(self.summary),
            "events": [dict(event) for event in self.events],
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
        }


def build_paper_observability_report(
    *,
    sessions_root: str | Path = "reports/tmp/paper_session",
    session_dirs: Iterable[str | Path] = (),
    ledger_inputs: Iterable[str | Path] = (),
    generated_at: str | None = None,
) -> PaperObservabilityReport:
    generated = generated_at or _utc_now()
    root = Path(sessions_root)
    discovered_dirs = _discover_session_dirs(root)
    explicit_dirs = [Path(session_dir) for session_dir in session_dirs]
    ordered_session_dirs = _dedupe_paths([*discovered_dirs, *explicit_dirs])
    ledger_paths = [Path(path) for path in ledger_inputs]

    events: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for session_dir in ordered_session_dirs:
        session_events, session_diagnostics = _events_from_session_dir(session_dir, generated_at=generated)
        events.extend(session_events)
        diagnostics.extend(session_diagnostics)
    for ledger_path in ledger_paths:
        ledger_events, ledger_diagnostics = _events_from_ledger(ledger_path, generated_at=generated)
        events.extend(ledger_events)
        diagnostics.extend(ledger_diagnostics)

    events.extend(diagnostics)
    events = _sorted_events(events)
    summary = _build_summary(events)
    return PaperObservabilityReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated,
        sources={
            "sessions_root": str(root),
            "session_dirs": [str(path) for path in ordered_session_dirs],
            "ledger_inputs": [str(path) for path in ledger_paths],
        },
        summary=summary,
        events=tuple(events),
        diagnostics=tuple(diagnostics),
    )


def write_paper_observability_report(
    report: PaperObservabilityReport,
    *,
    output: str | Path,
    markdown_output: str | Path | None = None,
) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    if markdown_output is not None:
        markdown_path = Path(markdown_output)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_paper_observability_markdown(report), encoding="utf-8")


def append_paper_ledger_event(ledger_output: str | Path | None, event: Mapping[str, object]) -> None:
    if not ledger_output:
        return
    path = Path(ledger_output)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_ledger_event(event)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(normalized, sort_keys=True) + "\n")


def paper_session_ledger_event(
    *,
    session_dir: str | Path,
    exit_code: int,
    source_path: str | Path | None = None,
    reasons: Iterable[object] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    events, diagnostics = _events_from_session_dir(Path(session_dir), generated_at=generated_at or _utc_now())
    for event in events:
        if event.get("event_type") == "paper_session":
            return {**event, "exit_code": exit_code}
    reason_list = list(reasons) or [diagnostic.get("reason") for diagnostic in diagnostics if diagnostic.get("reason")]
    return _base_event(
        "paper_session",
        generated_at=generated_at,
        status="ERROR" if exit_code == 2 else "BLOCKED",
        exit_code=exit_code,
        session_dir=session_dir,
        source_path=source_path,
        reasons=reason_list,
    )


def paper_execution_ledger_event(
    *,
    session_dir: str | Path,
    exit_code: int,
    execution_path: str | Path | None = None,
    status: str | None = None,
    reasons: Iterable[object] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    if execution_path is not None and Path(execution_path).exists():
        try:
            payload = _read_json_object(Path(execution_path))
        except (OSError, ValueError):
            payload = None
        if payload is not None:
            return _event_from_execution_payload(
                payload,
                source_path=Path(execution_path),
                session_dir=Path(session_dir),
                generated_at=generated_at or _utc_now(),
                exit_code=exit_code,
            )
    return _base_event(
        "paper_execution",
        generated_at=generated_at,
        status=status or ("ERROR" if exit_code == 2 else "BLOCKED"),
        exit_code=exit_code,
        session_dir=session_dir,
        source_path=execution_path,
        reasons=list(reasons),
    )


def paper_closeout_ledger_event(
    *,
    session_dir: str | Path,
    exit_code: int,
    closeout_path: str | Path | None = None,
    status: str | None = None,
    client_order_id: object = None,
    symbol: object = None,
    side: object = None,
    notional: object = None,
    reasons: Iterable[object] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    if closeout_path is not None and Path(closeout_path).exists():
        try:
            payload = _read_json_object(Path(closeout_path))
        except (OSError, ValueError):
            payload = None
        if payload is not None:
            return _event_from_closeout_payload(
                payload,
                source_path=Path(closeout_path),
                session_dir=Path(session_dir),
                generated_at=generated_at or _utc_now(),
                exit_code=exit_code,
            )
    return _base_event(
        "paper_closeout",
        generated_at=generated_at,
        status=status or ("ERROR" if exit_code == 2 else "PENDING"),
        exit_code=exit_code,
        session_dir=session_dir,
        source_path=closeout_path,
        output_path=closeout_path,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        notional=notional,
        reasons=list(reasons),
    )


def paper_order_ledger_event(
    *,
    event_type: str,
    payload: Mapping[str, object] | None,
    exit_code: int,
    output_path: str | Path | None,
    source_path: str | Path | None = None,
    status: str | None = None,
    reasons: Iterable[object] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    payload = payload or {}
    order = _first_mapping(
        payload.get("expected_order"),
        payload.get("order"),
        payload.get("resolved_order"),
        payload.get("order_intent"),
    )
    cancel_result = _mapping_or_empty(payload.get("cancel_result"))
    reconciliation = _mapping_or_empty(payload.get("reconciliation"))
    event_status = status
    if event_status is None:
        if event_type == "paper_reconciliation":
            event_status = "MATCHED" if reconciliation.get("matched") is True else "UNMATCHED"
        elif event_type == "paper_cancel_order":
            event_status = "CANCELLED" if cancel_result.get("accepted") is True else "BLOCKED"
        else:
            event_status = "COMPLETED" if exit_code == 0 else "BLOCKED"
    return _base_event(
        event_type,
        generated_at=generated_at,
        status=event_status,
        exit_code=exit_code,
        source_path=source_path,
        output_path=output_path,
        client_order_id=order.get("client_order_id"),
        symbol=order.get("symbol"),
        side=order.get("side"),
        notional=order.get("notional"),
        reconciliation_matched=reconciliation.get("matched") if event_type == "paper_reconciliation" else None,
        reasons=list(reasons) or _string_list(reconciliation.get("differences")) or _string_list(cancel_result.get("reasons")),
    )


def render_paper_observability_markdown(report: PaperObservabilityReport) -> str:
    summary = report.summary
    blockers = _mapping_or_empty(summary.get("blockers"))
    latest_events = summary.get("latest_events")
    lines = [
        "# Paper Observability",
        "",
        f"Generated at: `{report.generated_at}`",
        f"Events: `{summary.get('event_count', 0)}`",
        f"Sessions ready: `{summary.get('sessions_ready', 0)}`",
        f"Sessions blocked: `{summary.get('sessions_blocked', 0)}`",
        f"Executions submitted: `{summary.get('executions_submitted', 0)}`",
        f"Executions blocked: `{summary.get('executions_blocked', 0)}`",
        f"Closeouts closed: `{summary.get('closeouts_closed', 0)}`",
        f"Closeouts pending: `{summary.get('closeouts_pending', 0)}`",
        f"Closeouts unmatched: `{summary.get('closeouts_unmatched', 0)}`",
        f"Reconciliations matched: `{summary.get('reconciliations_matched', 0)}`",
        f"Reconciliations unmatched: `{summary.get('reconciliations_unmatched', 0)}`",
        f"Cancellations: `{summary.get('cancellations', 0)}`",
        "",
        "## Blockers",
        "",
        "| Reason | Count |",
        "| --- | ---: |",
    ]
    if blockers:
        for reason, count in sorted(blockers.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"| `{_escape_markdown(reason)}` | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Latest Events",
            "",
            "| Time | Type | Status | Symbol | Client order ID |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if isinstance(latest_events, list) and latest_events:
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
    lines.append("")
    return "\n".join(lines)


def _events_from_session_dir(
    session_dir: Path,
    *,
    generated_at: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    session_path = session_dir / "session.json"
    session = _read_json_object_or_diagnostic(session_path, diagnostics, generated_at=generated_at, session_dir=session_dir)
    if session is None:
        return events, diagnostics

    paths = _mapping_or_empty(session.get("paths"))
    audit_path = _artifact_path(paths, "audit_report", session_dir / "audit" / "paper_audit.json")
    signal_path = _artifact_path(paths, "signal_report", session_dir / "paper" / "paper_signal_order.json")
    freshness_path = _artifact_path(paths, "freshness_report", session_dir / "fresh_data" / "freshness.json")
    drift_path = _artifact_path(paths, "drift_report", session_dir / "monitoring" / "drift.json")
    execution_path = session_dir / "execution" / "paper_execution.json"
    closeout_path = session_dir / "closeout" / "paper_closeout.json"

    audit = _read_json_object_or_diagnostic(audit_path, diagnostics, generated_at=generated_at, session_dir=session_dir)
    signal = _read_json_object_or_diagnostic(signal_path, diagnostics, generated_at=generated_at, session_dir=session_dir)
    freshness = _read_json_object_or_diagnostic(
        freshness_path,
        diagnostics,
        generated_at=generated_at,
        session_dir=session_dir,
    )
    drift = None
    if drift_path is not None and drift_path.exists():
        drift = _read_json_object_or_diagnostic(drift_path, diagnostics, generated_at=generated_at, session_dir=session_dir)

    events.append(
        _event_from_session_payload(
            session=session,
            audit=audit,
            signal=signal,
            freshness=freshness,
            drift=drift,
            source_path=session_path,
            session_dir=session_dir,
            generated_at=generated_at,
        )
    )

    if execution_path.exists():
        execution = _read_json_object_or_diagnostic(
            execution_path,
            diagnostics,
            generated_at=generated_at,
            session_dir=session_dir,
        )
        if execution is not None:
            events.append(
                _event_from_execution_payload(
                    execution,
                    source_path=execution_path,
                    session_dir=session_dir,
                    generated_at=generated_at,
                )
            )
    if closeout_path.exists():
        closeout = _read_json_object_or_diagnostic(
            closeout_path,
            diagnostics,
            generated_at=generated_at,
            session_dir=session_dir,
        )
        if closeout is not None:
            events.append(
                _event_from_closeout_payload(
                    closeout,
                    source_path=closeout_path,
                    session_dir=session_dir,
                    generated_at=generated_at,
                )
            )
    return events, diagnostics


def _events_from_ledger(
    ledger_path: Path,
    *,
    generated_at: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    if not ledger_path.exists():
        diagnostics.append(
            _diagnostic_event(
                generated_at=generated_at,
                source_path=ledger_path,
                reason="missing_ledger",
                message=f"ledger input is missing: {ledger_path}",
            )
        )
        return events, diagnostics

    for line_number, raw_line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append(
                _diagnostic_event(
                    generated_at=generated_at,
                    source_path=ledger_path,
                    reason="invalid_ledger_json",
                    message=f"invalid JSON in {ledger_path}:{line_number}: {exc}",
                )
            )
            continue
        if not isinstance(payload, Mapping):
            diagnostics.append(
                _diagnostic_event(
                    generated_at=generated_at,
                    source_path=ledger_path,
                    reason="invalid_ledger_event",
                    message=f"ledger event in {ledger_path}:{line_number} is not a JSON object",
                )
            )
            continue
        event_payload = dict(payload)
        event_payload.setdefault("source_path", str(ledger_path))
        events.append(_normalize_ledger_event(event_payload))
    return events, diagnostics


def _event_from_session_payload(
    *,
    session: Mapping[str, object],
    audit: Mapping[str, object] | None,
    signal: Mapping[str, object] | None,
    freshness: Mapping[str, object] | None,
    drift: Mapping[str, object] | None,
    source_path: Path,
    session_dir: Path,
    generated_at: str,
) -> dict[str, object]:
    signal = signal or {}
    audit = audit or {}
    freshness = freshness or {}
    order_intent = _mapping_or_empty(signal.get("order_intent"))
    preflight = _mapping_or_empty(signal.get("preflight"))
    status = "READY" if session.get("ready_for_paper_review") is True else "BLOCKED"
    finding_codes = _finding_codes(audit)
    reasons = _dedupe_strings(
        [
            *finding_codes,
            *_string_list(freshness.get("reasons")),
            *_string_list(preflight.get("reasons")),
        ]
    )
    return _base_event(
        "paper_session",
        generated_at=str(audit.get("generated_at") or generated_at),
        status=status,
        exit_code=_int_or_none(session.get("exit_code")),
        session_dir=session_dir,
        source_path=source_path,
        output_path=session.get("output_dir"),
        client_order_id=order_intent.get("client_order_id"),
        symbol=order_intent.get("symbol"),
        side=order_intent.get("side"),
        notional=order_intent.get("notional"),
        ready_for_paper_review=session.get("ready_for_paper_review") is True,
        preflight_allowed=preflight.get("allowed"),
        reasons=reasons,
        finding_codes=finding_codes,
        extra={
            "submitted": signal.get("submitted") is True,
            "freshness_allowed": freshness.get("allowed"),
            "drift_detected": drift.get("drift_detected") if drift is not None else None,
        },
    )


def _event_from_execution_payload(
    payload: Mapping[str, object],
    *,
    source_path: Path,
    session_dir: Path,
    generated_at: str,
    exit_code: int | None = None,
) -> dict[str, object]:
    order_sent = _mapping_or_empty(payload.get("order_sent"))
    preflight = _mapping_or_empty(payload.get("preflight"))
    broker_result = _mapping_or_empty(payload.get("broker_result"))
    status = str(payload.get("status") or "BLOCKED")
    derived_exit_code = 0 if status == "SUBMITTED" else 1
    return _base_event(
        "paper_execution",
        generated_at=str(payload.get("generated_at") or generated_at),
        status=status,
        exit_code=exit_code if exit_code is not None else derived_exit_code,
        session_dir=_mapping_or_empty(payload.get("session")).get("session_dir") or session_dir,
        source_path=source_path,
        output_path=source_path,
        client_order_id=order_sent.get("client_order_id"),
        symbol=order_sent.get("symbol"),
        side=order_sent.get("side"),
        notional=order_sent.get("notional"),
        ready_for_paper_review=_mapping_or_empty(payload.get("session")).get("ready_for_paper_review"),
        preflight_allowed=preflight.get("allowed"),
        reasons=_dedupe_strings([*_string_list(preflight.get("reasons")), *_string_list(broker_result.get("reasons"))]),
    )


def _event_from_closeout_payload(
    payload: Mapping[str, object],
    *,
    source_path: Path,
    session_dir: Path,
    generated_at: str,
    exit_code: int | None = None,
) -> dict[str, object]:
    expected_order = _mapping_or_empty(payload.get("expected_order"))
    status = str(payload.get("status") or "PENDING")
    derived_exit_code = 0 if status == "CLOSED" else 1
    return _base_event(
        "paper_closeout",
        generated_at=str(payload.get("generated_at") or generated_at),
        status=status,
        exit_code=exit_code if exit_code is not None else derived_exit_code,
        session_dir=_mapping_or_empty(payload.get("session")).get("session_dir") or session_dir,
        source_path=source_path,
        output_path=source_path,
        client_order_id=expected_order.get("client_order_id"),
        symbol=expected_order.get("symbol"),
        side=expected_order.get("side"),
        notional=expected_order.get("notional"),
        reasons=_string_list(payload.get("reasons")),
    )


def _base_event(
    event_type: str,
    *,
    generated_at: str | None = None,
    status: str | None = None,
    exit_code: int | None = None,
    session_dir: str | Path | None = None,
    source_path: str | Path | None = None,
    output_path: str | Path | None = None,
    client_order_id: object = None,
    symbol: object = None,
    side: object = None,
    notional: object = None,
    ready_for_paper_review: object = None,
    preflight_allowed: object = None,
    reconciliation_matched: object = None,
    reasons: Iterable[object] = (),
    finding_codes: Iterable[object] = (),
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "generated_at": generated_at or _utc_now(),
        "status": status or "UNKNOWN",
    }
    optional = {
        "exit_code": exit_code,
        "session_dir": str(session_dir) if session_dir is not None else None,
        "source_path": str(source_path) if source_path is not None else None,
        "output_path": str(output_path) if output_path is not None else None,
        "client_order_id": str(client_order_id) if client_order_id not in {None, ""} else None,
        "symbol": str(symbol).upper() if symbol not in {None, ""} else None,
        "side": str(side).lower() if side not in {None, ""} else None,
        "notional": _float_or_original(notional),
        "ready_for_paper_review": ready_for_paper_review,
        "preflight_allowed": preflight_allowed,
        "reconciliation_matched": reconciliation_matched,
    }
    for key, value in optional.items():
        if value is not None:
            event[key] = value
    reason_list = _dedupe_strings(reasons)
    if reason_list:
        event["reasons"] = reason_list
    code_list = _dedupe_strings(finding_codes)
    if code_list:
        event["finding_codes"] = code_list
    for key, value in (extra or {}).items():
        if value is not None:
            event[str(key)] = value
    return event


def _diagnostic_event(
    *,
    generated_at: str,
    source_path: Path,
    reason: str,
    message: str,
    session_dir: Path | None = None,
) -> dict[str, object]:
    return _base_event(
        "paper_observability_diagnostic",
        generated_at=generated_at,
        status="BLOCKED",
        exit_code=1,
        session_dir=session_dir,
        source_path=source_path,
        reasons=[reason],
        finding_codes=[reason],
        extra={"message": message, "reason": reason},
    )


def _build_summary(events: list[dict[str, object]]) -> dict[str, object]:
    blockers: Counter[str] = Counter()
    for event in events:
        status = str(event.get("status", "")).upper()
        if status not in {"READY", "SUBMITTED", "MATCHED", "COMPLETED", "CANCELLED", "FOUND", "CLOSED"}:
            blockers.update(
                set(_string_list(event.get("finding_codes"))) | set(_string_list(event.get("reasons")))
            )

    return {
        "event_count": len(events),
        "sessions_ready": _count_events(events, "paper_session", statuses={"READY"}),
        "sessions_blocked": _count_events(events, "paper_session", statuses={"BLOCKED", "ERROR"}),
        "executions_submitted": _count_events(events, "paper_execution", statuses={"SUBMITTED"}),
        "executions_blocked": _count_events(events, "paper_execution", statuses={"BLOCKED", "ERROR"}),
        "closeouts_closed": _count_events(events, "paper_closeout", statuses={"CLOSED"}),
        "closeouts_pending": _count_events(events, "paper_closeout", statuses={"PENDING"}),
        "closeouts_unmatched": _count_events(events, "paper_closeout", statuses={"UNMATCHED"}),
        "reconciliations_matched": _count_reconciliations(events, matched=True),
        "reconciliations_unmatched": _count_reconciliations(events, matched=False),
        "cancellations": _count_events(events, "paper_cancel_order"),
        "order_queries": _count_events(events, "paper_order_query"),
        "order_lists": _count_events(events, "paper_order_list"),
        "diagnostics": _count_events(events, "paper_observability_diagnostic"),
        "blockers": dict(sorted(blockers.items())),
        "latest_events": [_event_summary(event) for event in _latest_events(events)],
    }


def _count_events(events: Iterable[Mapping[str, object]], event_type: str, statuses: set[str] | None = None) -> int:
    count = 0
    for event in events:
        if event.get("event_type") != event_type:
            continue
        if statuses is not None and str(event.get("status", "")).upper() not in statuses:
            continue
        count += 1
    return count


def _count_reconciliations(events: Iterable[Mapping[str, object]], *, matched: bool) -> int:
    return sum(
        1
        for event in events
        if event.get("event_type") == "paper_reconciliation" and event.get("reconciliation_matched") is matched
    )


def _event_summary(event: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "generated_at",
        "event_type",
        "status",
        "session_dir",
        "output_path",
        "client_order_id",
        "symbol",
        "side",
        "notional",
        "reasons",
        "finding_codes",
    )
    return {key: event[key] for key in keys if key in event}


def _latest_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return _sorted_events(events)[-LATEST_EVENT_LIMIT:][::-1]


def _sorted_events(events: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(events, key=lambda event: str(event.get("generated_at", "")))


def _discover_session_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if (root / "session.json").exists():
        return [root]
    return sorted(path.parent for path in root.glob("*/session.json"))


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _read_json_object_or_diagnostic(
    path: Path | None,
    diagnostics: list[dict[str, object]],
    *,
    generated_at: str,
    session_dir: Path,
) -> Mapping[str, object] | None:
    if path is None:
        return None
    if not path.exists():
        diagnostics.append(
            _diagnostic_event(
                generated_at=generated_at,
                source_path=path,
                reason="missing_artifact",
                message=f"required session artifact is missing: {path}",
                session_dir=session_dir,
            )
        )
        return None
    try:
        return _read_json_object(path)
    except ValueError as exc:
        diagnostics.append(
            _diagnostic_event(
                generated_at=generated_at,
                source_path=path,
                reason="invalid_json",
                message=str(exc),
                session_dir=session_dir,
            )
        )
    return None


def _read_json_object(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _artifact_path(paths: Mapping[str, object], key: str, default: Path) -> Path | None:
    raw_value = paths.get(key)
    if raw_value in {None, ""}:
        return default
    return Path(str(raw_value))


def _normalize_ledger_event(event: Mapping[str, object]) -> dict[str, object]:
    event_type = str(event.get("event_type") or "unknown")
    normalized = _base_event(
        event_type,
        generated_at=str(event.get("generated_at") or _utc_now()),
        status=str(event.get("status") or "UNKNOWN"),
        exit_code=_int_or_none(event.get("exit_code")),
        session_dir=event.get("session_dir"),
        source_path=event.get("source_path"),
        output_path=event.get("output_path"),
        client_order_id=event.get("client_order_id"),
        symbol=event.get("symbol"),
        side=event.get("side"),
        notional=event.get("notional"),
        ready_for_paper_review=event.get("ready_for_paper_review"),
        preflight_allowed=event.get("preflight_allowed"),
        reconciliation_matched=event.get("reconciliation_matched"),
        reasons=_string_list(event.get("reasons")),
        finding_codes=_string_list(event.get("finding_codes")),
    )
    for key in ("submitted", "freshness_allowed"):
        if key in event:
            normalized[key] = event[key]
    return normalized


def _finding_codes(audit: Mapping[str, object]) -> list[str]:
    findings = audit.get("findings")
    if not isinstance(findings, list):
        return []
    return _dedupe_strings(
        finding.get("code")
        for finding in findings
        if isinstance(finding, Mapping) and finding.get("severity") in {"fail", "warn"}
    )


def _first_mapping(*values: object) -> Mapping[str, object]:
    for value in values:
        if isinstance(value, Mapping):
            return value
    return {}


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in {None, ""}]
    if value in {None, ""}:
        return []
    return [str(value)]


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
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_original(value: object) -> object:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
