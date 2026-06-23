"""Shared paper-auto session ledger classification."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

CLASSIFICATIONS = (
    "CLEAN",
    "BLOCKED",
    "SUBMITTED_NO_FILL",
    "FILL_UNRECONCILED",
    "CLOSEOUT_PENDING",
    "STATEMENT_PENDING",
)


def summarize_paper_auto_sessions(
    ledger_inputs: Iterable[str | Path],
    *,
    min_clean_sessions: int = 20,
) -> dict[str, object]:
    records, diagnostics = read_paper_auto_session_records(ledger_inputs)
    classifications = {name: 0 for name in CLASSIFICATIONS}
    blocker_histogram: dict[str, int] = {}
    classified_records: list[dict[str, object]] = []
    latest_session_date = None
    latest_session_id = None
    broker_confirmed = 0

    for record in records:
        classification, reasons = classify_paper_auto_session(record)
        classifications[classification] += 1
        session_id = str(record.get("session_id") or "")
        as_of_date = str(record.get("as_of_date") or "")
        if as_of_date:
            latest_session_date = max(latest_session_date or as_of_date, as_of_date)
        if session_id:
            latest_session_id = session_id
        if record.get("confirm_paper_auto") is True:
            broker_confirmed += 1
        for reason in reasons:
            blocker_histogram[reason] = blocker_histogram.get(reason, 0) + 1
        classified_records.append(
            {
                "session_id": session_id or None,
                "as_of_date": as_of_date or None,
                "state": str(record.get("state") or "UNKNOWN"),
                "classification": classification,
                "blockers": reasons,
            }
        )

    for diagnostic in diagnostics:
        reason = str(diagnostic.get("code") or "ledger_diagnostic")
        blocker_histogram[reason] = blocker_histogram.get(reason, 0) + 1

    clean_sessions = classifications["CLEAN"]
    blocking_classifications = sum(count for name, count in classifications.items() if name != "CLEAN")
    if diagnostics or blocking_classifications:
        state = "BLOCKED"
        next_action = "resolve_blockers"
    elif clean_sessions >= min_clean_sessions:
        state = "READY_FOR_REVIEW"
        next_action = "review_next_phase"
    else:
        state = "ACCUMULATING"
        next_action = "continue_paper_auto_campaign"

    return {
        "target_clean_sessions": int(min_clean_sessions),
        "total_sessions": len(records),
        "clean_sessions": clean_sessions,
        "broker_confirmed_sessions": broker_confirmed,
        "blocked_sessions": blocking_classifications,
        "remaining_clean_sessions": max(int(min_clean_sessions) - clean_sessions, 0),
        "classifications": classifications,
        "blocker_histogram": dict(sorted(blocker_histogram.items())),
        "latest_session_date": latest_session_date,
        "latest_session_id": latest_session_id,
        "state": state,
        "next_action": next_action,
        "records": classified_records[-20:],
        "diagnostics": diagnostics,
    }


def read_paper_auto_session_records(
    ledger_inputs: Iterable[str | Path],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    records: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for ledger_input in ledger_inputs:
        path = Path(ledger_input)
        if not path.exists():
            diagnostics.append({"severity": "WARNING", "code": "session_ledger_missing", "source_path": str(path)})
            continue
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                diagnostics.append(
                    {
                        "severity": "ERROR",
                        "code": "session_ledger_invalid_json",
                        "source_path": str(path),
                        "line": line_number,
                        "message": str(exc),
                    }
                )
                continue
            if not isinstance(payload, Mapping):
                diagnostics.append(
                    {
                        "severity": "ERROR",
                        "code": "session_ledger_invalid_record",
                        "source_path": str(path),
                        "line": line_number,
                    }
                )
                continue
            if payload.get("record_type") == "paper_auto_cycle_session":
                records.append(dict(payload))
    return records, diagnostics


def classify_paper_auto_session(record: Mapping[str, object]) -> tuple[str, list[str]]:
    state = str(record.get("state") or "UNKNOWN").upper()
    blockers = _string_list(record.get("blockers"))
    if state in {"BLOCKED", "ERROR"} or blockers:
        return "BLOCKED", blockers or [state.lower() or "blocked"]

    closeout_status = str(record.get("closeout_status") or "").upper()
    statement_status = str(record.get("statement_status") or "").upper()
    unreconciled_fills = _int_value(record.get("unreconciled_fills"), default=0)

    if state == "PAPER_SUBMITTED":
        if closeout_status in {"PENDING", "OPEN", "UNMATCHED"}:
            return "CLOSEOUT_PENDING", ["closeout_pending"]
        return "SUBMITTED_NO_FILL", ["submitted_no_fill"]

    if state == "PAPER_CLOSED":
        if closeout_status in {"PENDING", "OPEN", "UNMATCHED"}:
            return "CLOSEOUT_PENDING", ["closeout_pending"]
        if unreconciled_fills > 0 or statement_status in {
            "DIFFERENCES",
            "MISMATCH",
            "UNMATCHED",
            "UNRECONCILED",
            "ERROR",
            "MISSING",
        }:
            return "FILL_UNRECONCILED", ["fills_unreconciled"]
        if statement_status in {"", "NOT_REQUESTED", "UNKNOWN", "STATEMENT_PENDING"}:
            return "STATEMENT_PENDING", ["statement_pending"]
        if record.get("confirm_paper_auto") is not True:
            return "BLOCKED", ["broker_confirmation_missing"]
        return "CLEAN", []

    return "BLOCKED", [state.lower() or "unknown_state"]


def paper_auto_blockers(summary: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    histogram = summary.get("blocker_histogram")
    if not isinstance(histogram, Mapping):
        return blockers
    for code, count in histogram.items():
        blockers.append(
            {
                "severity": "CRITICAL",
                "code": str(code),
                "message": f"paper-auto campaign blocker observed {int(count)} time(s): {code}",
            }
        )
    return blockers


def _string_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _int_value(value: object, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
