"""Shared paper-auto session ledger classification."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime
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
    latest_session: tuple[str, str] | None = None
    broker_confirmed = 0

    for record in records:
        classification, reasons = classify_paper_auto_session(record)
        classifications[classification] += 1
        session_id = str(record.get("session_id") or "")
        as_of_date = str(record.get("as_of_date") or "")
        if as_of_date and session_id:
            candidate = (as_of_date, session_id)
            latest_session = max(latest_session or candidate, candidate)
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

    clean_target = (
        min_clean_sessions
        if isinstance(min_clean_sessions, int)
        and not isinstance(min_clean_sessions, bool)
        and min_clean_sessions > 0
        else 0
    )
    if clean_target == 0:
        diagnostics.append(
            {
                "severity": "ERROR",
                "code": "min_clean_sessions_invalid",
            }
        )
        blocker_histogram["min_clean_sessions_invalid"] = 1

    clean_sessions = classifications["CLEAN"]
    blocking_classifications = sum(count for name, count in classifications.items() if name != "CLEAN")
    if diagnostics or blocking_classifications:
        state = "BLOCKED"
        next_action = "resolve_blockers"
    elif clean_sessions >= clean_target:
        state = "READY_FOR_REVIEW"
        next_action = "review_next_phase"
    else:
        state = "ACCUMULATING"
        next_action = "continue_paper_auto_campaign"

    return {
        "target_clean_sessions": clean_target,
        "total_sessions": len(records),
        "clean_sessions": clean_sessions,
        "broker_confirmed_sessions": broker_confirmed,
        "blocked_sessions": blocking_classifications,
        "remaining_clean_sessions": max(clean_target - clean_sessions, 0),
        "classifications": classifications,
        "blocker_histogram": dict(sorted(blocker_histogram.items())),
        "latest_session_date": latest_session[0] if latest_session else None,
        "latest_session_id": latest_session[1] if latest_session else None,
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
    seen_session_ids: set[str] = set()
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
                record = dict(payload)
                issues = _record_issues(record)
                if issues:
                    diagnostics.extend(
                        {
                            "severity": "ERROR",
                            "code": issue,
                            "source_path": str(path),
                            "line": line_number,
                        }
                        for issue in issues
                    )
                    continue
                session_id = str(record["session_id"])
                if session_id in seen_session_ids:
                    diagnostics.append(
                        {
                            "severity": "ERROR",
                            "code": "session_ledger_duplicate_session_id",
                            "source_path": str(path),
                            "line": line_number,
                            "session_id": session_id,
                        }
                    )
                    continue
                seen_session_ids.add(session_id)
                records.append(record)
    return records, diagnostics


def classify_paper_auto_session(record: Mapping[str, object]) -> tuple[str, list[str]]:
    issues = _record_issues(record)
    if issues:
        return "BLOCKED", [f"session_record_invalid:{issue}" for issue in issues]
    state = str(record.get("state") or "UNKNOWN").upper()
    blockers = _string_list(record.get("blockers"))
    if state in {"BLOCKED", "ERROR"} or blockers:
        return "BLOCKED", blockers or [state.lower() or "blocked"]

    closeout_status = str(record.get("closeout_status") or "").upper()
    statement_status = str(record.get("statement_status") or "").upper()
    unreconciled_fills = _strict_nonnegative_int(record.get("unreconciled_fills"))
    assert unreconciled_fills is not None

    if state == "PAPER_SUBMITTED":
        if closeout_status in {"PENDING", "OPEN", "UNMATCHED"}:
            return "CLOSEOUT_PENDING", ["closeout_pending"]
        return "SUBMITTED_NO_FILL", ["submitted_no_fill"]

    if state == "PAPER_CLOSED":
        if closeout_status in {"PENDING", "OPEN", "UNMATCHED"}:
            return "CLOSEOUT_PENDING", ["closeout_pending"]
        if closeout_status != "CLOSED":
            return "BLOCKED", ["closeout_status_invalid"]
        if unreconciled_fills > 0:
            return "FILL_UNRECONCILED", ["fills_unreconciled"]
        if statement_status in {"", "NOT_REQUESTED", "UNKNOWN", "STATEMENT_PENDING"}:
            return "STATEMENT_PENDING", ["statement_pending"]
        if statement_status != "MATCHED":
            return "FILL_UNRECONCILED", ["fills_unreconciled"]
        if record.get("confirm_paper_auto") is not True:
            return "BLOCKED", ["broker_confirmation_missing"]
        if record.get("exit_code") != 0:
            return "BLOCKED", ["session_exit_code_nonzero"]
        if record.get("order_state") != "paper_order_sent":
            return "BLOCKED", ["order_state_invalid"]
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
    if value in {None, ""}:
        return default
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _record_issues(record: Mapping[str, object]) -> list[str]:
    issues: list[str] = []
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        issues.append("session_record_identity_invalid")
    as_of = _iso_date(record.get("as_of_date"))
    generated = _aware_datetime(record.get("generated_at"))
    if as_of is None or generated is None or generated.date() != as_of:
        issues.append("session_record_timestamp_invalid")
    state = str(record.get("state") or "").upper()
    if state not in {"BLOCKED", "ERROR", "PAPER_SUBMITTED", "PAPER_CLOSED"}:
        issues.append("session_record_state_invalid")
    if not isinstance(record.get("confirm_paper_auto"), bool):
        issues.append("session_record_confirmation_invalid")
    exit_code = record.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code < 0:
        issues.append("session_record_exit_code_invalid")
    if _strict_nonnegative_int(record.get("unreconciled_fills")) is None:
        issues.append("session_record_unreconciled_fills_invalid")
    blockers = record.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        issues.append("session_record_blockers_invalid")
    safety = record.get("safety")
    if (
        not isinstance(safety, Mapping)
        or safety.get("paper_only") is not True
        or safety.get("live_trading_authorized") is not False
    ):
        issues.append("session_record_safety_invalid")
    if _contains_nonfinite(record):
        issues.append("session_record_nonfinite")
    return list(dict.fromkeys(issues))


def _strict_nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _contains_nonfinite(value: object) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_nonfinite(item) for item in value)
    return False
