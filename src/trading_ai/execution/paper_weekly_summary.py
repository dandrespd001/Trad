"""Weekly rollup for paper-only operating evidence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_DECISIONS_ROOT = "reports/tmp/paper_decisions"
DEFAULT_PERFORMANCE_ROOT = "reports/tmp/paper_performance"
DEFAULT_CAMPAIGN_ROOT = "reports/tmp/paper_campaign"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_weekly_summary"
DECISION_STATES = ("CONTINUE", "REVIEW", "STOP", "ERROR")


class PaperWeeklySummaryOperationalError(RuntimeError):
    """Raised when the weekly summary cannot be produced."""


@dataclass(frozen=True)
class PaperWeeklySummaryResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_weekly_summary(
    *,
    decisions_root: str | Path = DEFAULT_DECISIONS_ROOT,
    performance_root: str | Path = DEFAULT_PERFORMANCE_ROOT,
    campaign_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    ledger_inputs: Iterable[str | Path] = (),
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    week: str = "auto",
    as_of_date: str = "today",
    history_weeks: int = 1,
    generated_at: str | None = None,
) -> PaperWeeklySummaryResult:
    generated = generated_at or _utc_now()
    report = build_paper_weekly_summary(
        decisions_root=decisions_root,
        performance_root=performance_root,
        campaign_root=campaign_root,
        ledger_inputs=ledger_inputs,
        week=week,
        as_of_date=as_of_date,
        history_weeks=history_weeks,
        generated_at=generated,
    )
    week_token = str(report["week"])
    output_root = Path(output_dir) / week_token
    output_path = output_root / "weekly_summary.json"
    markdown_path = output_root / "weekly_summary.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_weekly_summary_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperWeeklySummaryResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_weekly_summary(
    *,
    decisions_root: str | Path = DEFAULT_DECISIONS_ROOT,
    performance_root: str | Path = DEFAULT_PERFORMANCE_ROOT,
    campaign_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    ledger_inputs: Iterable[str | Path] = (),
    week: str = "auto",
    as_of_date: str = "today",
    history_weeks: int = 1,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    week_token = _resolve_week_token(week, as_of_date=as_of_date)
    decisions = _decision_summary(Path(decisions_root), week=week_token)
    ledger = _ledger_summary([Path(path) for path in ledger_inputs])
    performance = _performance_summary(Path(performance_root), week=week_token)
    campaign = _campaign_summary(Path(campaign_root), week=week_token)
    blockers = _blocker_summary(decisions=decisions, ledger=ledger, performance=performance, campaign=campaign)
    blocker_aging = _blocker_aging_summary(
        Path(decisions_root),
        current_week=week_token,
        as_of_date=_resolve_as_of_date(as_of_date),
        history_weeks=history_weeks,
    )
    status = _weekly_status(
        decisions=decisions, blockers=blockers, performance=performance, blocker_aging=blocker_aging
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "week": week_token,
        "as_of_date": _resolve_as_of_date(as_of_date),
        "status": status,
        "sources": {
            "decisions_root": str(Path(decisions_root)),
            "performance_root": str(Path(performance_root)),
            "campaign_root": str(Path(campaign_root)),
            "ledger_inputs": [str(path) for path in ledger_inputs],
        },
        "decisions": decisions["summary"],
        "ledger": ledger["summary"],
        "performance": performance["summary"],
        "campaign": campaign["summary"],
        "blockers": blockers,
        "blocker_aging": blocker_aging,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_weekly_summary_markdown(report: Mapping[str, object]) -> str:
    decisions = _mapping(report.get("decisions"))
    counts = _mapping(decisions.get("counts"))
    ledger = _mapping(report.get("ledger"))
    performance = _mapping(report.get("performance"))
    blockers = _mapping(report.get("blockers"))
    blocker_aging = _mapping(report.get("blocker_aging"))
    raw_blocker_items = blockers.get("items")
    blocker_items = raw_blocker_items if isinstance(raw_blocker_items, list) else []
    lines = [
        "# Paper Weekly Summary",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Week: `{report.get('week') or ''}`",
        f"Generated at: `{report.get('generated_at') or ''}`",
        "",
        "## Decisions",
        "",
        f"CONTINUE: `{counts.get('CONTINUE', 0)}`",
        f"REVIEW: `{counts.get('REVIEW', 0)}`",
        f"STOP: `{counts.get('STOP', 0)}`",
        f"ERROR: `{counts.get('ERROR', 0)}`",
        "",
        "## Ledger",
        "",
        f"Sessions closed: `{ledger.get('sessions_closed', 0)}`",
        f"Fills: `{ledger.get('fills', 0)}`",
        f"Pending: `{ledger.get('pending', 0)}`",
        f"Unmatched: `{ledger.get('unmatched', 0)}`",
        "",
        "## Performance",
        "",
        f"Latest status: `{_mapping(performance.get('latest')).get('status') or ''}`",
        f"Warnings: `{', '.join(_string_list(performance.get('warnings')))}`",
        "",
        "## Blocker Aging",
        "",
        f"History weeks: `{blocker_aging.get('history_weeks', 1)}`",
        f"Consecutive REVIEW days: `{blocker_aging.get('consecutive_review_days', 0)}`",
        f"Days since last CONTINUE: `{blocker_aging.get('days_since_last_continue')}`",
        f"Recurrent blockers: `{', '.join(_string_list(blocker_aging.get('recurrent_blockers')))}`",
        f"Warnings: `{', '.join(_string_list(blocker_aging.get('warnings')))}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Count | Message |",
        "| --- | --- | ---: | --- |",
    ]
    if blocker_items:
        for blocker in blocker_items:
            if not isinstance(blocker, Mapping):
                continue
            lines.append(
                "| "
                f"`{_escape_markdown(blocker.get('severity') or '')}` "
                f"| `{_escape_markdown(blocker.get('code') or '')}` "
                f"| `{blocker.get('count', 1)}` "
                f"| {_escape_markdown(blocker.get('message') or '')} |"
            )
    else:
        lines.append("| OK | none | 0 | No weekly blockers. |")
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


def _decision_summary(root: Path, *, week: str) -> dict[str, object]:
    items: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    counts: Counter[str] = Counter({state: 0 for state in DECISION_STATES})
    for path in _discover_named_json(root, "decision.json"):
        if not _path_belongs_to_week(path, week):
            continue
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(_blocker("ERROR", "decision_invalid_json", str(exc), source_path=path))
            continue
        state = str(payload.get("decision") or payload.get("state") or "UNKNOWN").upper()
        counts[state] += 1
        item = {
            "path": str(path),
            "generated_at": str(payload.get("generated_at") or ""),
            "as_of_date": str(payload.get("as_of_date") or _date_from_path(path) or ""),
            "decision": state,
            "exit_code": payload.get("exit_code"),
            "reason": payload.get("reason"),
        }
        items.append(item)
        if state == "STOP":
            blockers.append(_blocker("CRITICAL", "stop_decision", "paper day close produced STOP", source_path=path))
        elif state == "ERROR":
            blockers.append(_blocker("ERROR", "error_decision", "paper day close produced ERROR", source_path=path))
        elif state == "REVIEW":
            blockers.append(_blocker("WARNING", "review_decision", "paper day close requires review", source_path=path))
        for blocker in _object_list(payload.get("blockers")):
            if isinstance(blocker, Mapping):
                blockers.append(
                    _blocker(
                        str(blocker.get("severity") or "WARNING").upper(),
                        str(blocker.get("code") or "decision_blocker"),
                        str(blocker.get("message") or blocker.get("code") or "decision blocker"),
                        source_path=blocker.get("source_path") or path,
                    )
                )
    return {
        "summary": {
            "total": len(items),
            "counts": dict(counts),
            "days": sorted(items, key=lambda item: str(item.get("as_of_date") or "")),
        },
        "blockers": blockers,
    }


def _ledger_summary(paths: list[Path]) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    events = 0
    sessions_closed = 0
    fills = 0
    pending = 0
    unmatched = 0
    for path in paths:
        if not path.exists():
            blockers.append(_blocker("WARNING", "missing_ledger", f"ledger input is missing: {path}", source_path=path))
            continue
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                blockers.append(
                    _blocker(
                        "WARNING",
                        "ledger_invalid_json",
                        f"invalid JSON in {path}:{line_number}: {exc}",
                        source_path=path,
                    )
                )
                continue
            if not isinstance(payload, Mapping):
                blockers.append(
                    _blocker("WARNING", "ledger_invalid_event", "ledger event is not an object", source_path=path)
                )
                continue
            events += 1
            event_type = str(payload.get("event_type") or "")
            status = str(payload.get("status") or "").upper()
            if event_type == "paper_session" and status in {"READY", "COMPLETED", "CLOSED"}:
                sessions_closed += 1
            elif event_type == "paper_closeout" and status == "CLOSED":
                fills += 1
            elif event_type == "paper_closeout" and status == "PENDING":
                pending += 1
            elif event_type == "paper_closeout" and status == "UNMATCHED":
                unmatched += 1
    return {
        "summary": {
            "event_count": events,
            "sessions_closed": sessions_closed,
            "fills": fills,
            "pending": pending,
            "unmatched": unmatched,
        },
        "blockers": blockers,
    }


def _performance_summary(root: Path, *, week: str) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    warnings: list[str] = []
    for path in _discover_json_paths(root):
        if not _path_belongs_to_week(path, week, include_unknown=True):
            continue
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(_blocker("WARNING", "performance_invalid_json", str(exc), source_path=path))
            continue
        item = dict(payload)
        item["path"] = str(path)
        reports.append(item)
        warnings.extend(_string_list(payload.get("warnings")))
        for blocker in _object_list(payload.get("blockers")):
            blockers.append(_blocker("WARNING", str(blocker), str(blocker), source_path=path))
    latest = sorted(reports, key=_generated_sort_key, reverse=True)[:1]
    latest_item = latest[0] if latest else None
    metrics = _mapping(_mapping(latest_item).get("paper_metrics"))
    return {
        "summary": {
            "total": len(reports),
            "latest": latest_item,
            "warnings": _dedupe_strings(warnings),
            "complete_sessions": metrics.get("complete_sessions", 0),
            "fills": metrics.get("fills", 0),
            "pending": metrics.get("pending_closeouts", 0),
            "unmatched": metrics.get("unmatched_closeouts", 0),
        },
        "blockers": blockers,
    }


def _campaign_summary(root: Path, *, week: str) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    for path in _discover_json_paths(root):
        if not _path_belongs_to_week(path, week, include_unknown=True):
            continue
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            blockers.append(_blocker("WARNING", "campaign_invalid_json", str(exc), source_path=path))
            continue
        item: dict[str, object] = {
            "path": str(path),
            "generated_at": str(payload.get("generated_at") or ""),
            "status": str(payload.get("status") or "UNKNOWN"),
        }
        reports.append(item)
        for blocker in _object_list(payload.get("blockers")):
            if isinstance(blocker, Mapping):
                blockers.append(
                    _blocker(
                        str(blocker.get("severity") or "WARNING").upper(),
                        str(blocker.get("code") or "campaign_blocker"),
                        str(blocker.get("message") or blocker.get("code") or "campaign blocker"),
                        source_path=blocker.get("source_path") or path,
                    )
                )
    latest = sorted(reports, key=_generated_sort_key, reverse=True)[:1]
    return {
        "summary": {
            "total": len(reports),
            "latest": latest[0] if latest else None,
        },
        "blockers": blockers,
    }


def _blocker_summary(
    *,
    decisions: Mapping[str, object],
    ledger: Mapping[str, object],
    performance: Mapping[str, object],
    campaign: Mapping[str, object],
) -> dict[str, object]:
    raw_blockers = [
        *_object_list(decisions.get("blockers")),
        *_object_list(ledger.get("blockers")),
        *_object_list(performance.get("blockers")),
        *_object_list(campaign.get("blockers")),
    ]
    counter: Counter[str] = Counter(
        str(blocker.get("code") or "unknown") for blocker in raw_blockers if isinstance(blocker, Mapping)
    )
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for blocker in raw_blockers:
        if not isinstance(blocker, Mapping):
            continue
        code = str(blocker.get("code") or "unknown")
        if code in seen:
            continue
        seen.add(code)
        normalized = dict(blocker)
        normalized["count"] = counter[code]
        items.append(normalized)
    recurrent = sorted(code for code, count in counter.items() if count >= 2 and code not in {"review_decision"})
    decision_counts = _mapping(_mapping(decisions.get("summary")).get("counts"))
    review_count = _int_value(decision_counts.get("REVIEW"), default=0)
    if review_count >= 2:
        recurrent.append("recurrent_review")
        if "recurrent_review" not in seen:
            items.append(
                _blocker(
                    "WARNING",
                    "recurrent_review",
                    "multiple REVIEW day-close decisions in the same paper week",
                    count=review_count,
                )
            )
    return {"items": items, "recurrent": _dedupe_strings(recurrent)}


def _weekly_status(
    *,
    decisions: Mapping[str, object],
    blockers: Mapping[str, object],
    performance: Mapping[str, object],
    blocker_aging: Mapping[str, object],
) -> str:
    items = _object_list(blockers.get("items"))
    if any(isinstance(item, Mapping) and str(item.get("severity") or "").upper() == "ERROR" for item in items):
        return "ERROR"
    counts = _mapping(_mapping(decisions.get("summary")).get("counts"))
    if _int_value(counts.get("ERROR"), default=0) > 0:
        return "ERROR"
    if _int_value(counts.get("STOP"), default=0) > 0:
        return "CRITICAL"
    if str(blocker_aging.get("status") or "").upper() == "CRITICAL":
        return "CRITICAL"
    if _int_value(counts.get("REVIEW"), default=0) > 0:
        return "WARN"
    if _string_list(_mapping(performance.get("summary")).get("warnings")):
        return "WARN"
    if str(blocker_aging.get("status") or "").upper() == "WARN":
        return "WARN"
    if items:
        return "WARN"
    return "OK"


def _blocker_aging_summary(
    root: Path,
    *,
    current_week: str,
    as_of_date: str,
    history_weeks: int,
) -> dict[str, object]:
    history_weeks = max(int(history_weeks), 1)
    weeks = _history_week_tokens(_parse_date(as_of_date), history_weeks)
    warnings: list[str] = []
    blocker_days: Counter[str] = Counter()
    blocker_weeks: dict[str, set[str]] = {}
    days: list[dict[str, object]] = []
    latest_stop_error: list[dict[str, object]] = []

    for path in _discover_named_json(root, "decision.json"):
        decision_date = _date_from_path(path)
        if decision_date is None:
            continue
        week = _week_token(decision_date)
        if week not in weeks:
            continue
        try:
            payload = read_json_artifact(path)
        except (OSError, json.JSONDecodeError, ValueError):
            if week == current_week:
                continue
            warnings.append("history_invalid_json")
            continue
        decision = str(payload.get("decision") or payload.get("state") or "UNKNOWN").upper()
        blocker_codes = _decision_blocker_codes(payload)
        day_item: dict[str, object] = {
            "as_of_date": decision_date.isoformat(),
            "week": week,
            "decision": decision,
            "blockers": blocker_codes,
            "path": str(path),
        }
        days.append(day_item)
        if decision in {"STOP", "ERROR"}:
            latest_stop_error.append(day_item)
        for code in blocker_codes:
            blocker_days[code] += 1
            blocker_weeks.setdefault(code, set()).add(week)

    current_days = sorted(
        (day for day in days if day.get("week") == current_week), key=lambda item: str(item.get("as_of_date"))
    )
    consecutive_review = _max_consecutive_review_days(current_days)
    last_continue = _last_decision_date(days, "CONTINUE")
    resolved_as_of = _parse_date(as_of_date)
    days_since_continue = (resolved_as_of - last_continue).days if last_continue is not None else None
    recurrent = sorted(
        code for code, count in blocker_days.items() if count >= 2 or len(blocker_weeks.get(code, set())) >= 2
    )
    historical_recurrent = sorted(
        code
        for code, count in blocker_days.items()
        if code in recurrent and any(week != current_week for week in blocker_weeks.get(code, set()))
    )
    current_stop = any(day.get("week") == current_week and day.get("decision") in {"STOP", "ERROR"} for day in days)
    critical_unresolved = any(
        code in {"stop_decision", "error_decision", "paper_closeout_pending", "paper_closeout_unmatched"}
        for code in recurrent
    )
    if current_stop or critical_unresolved:
        status = "CRITICAL"
    elif recurrent or warnings or consecutive_review >= 2:
        status = "WARN"
    else:
        status = "OK"
    return {
        "status": status,
        "history_weeks": history_weeks,
        "weeks": weeks,
        "blocker_day_counts": dict(blocker_days),
        "blocker_week_counts": {code: len(week_values) for code, week_values in blocker_weeks.items()},
        "recurrent_blockers": recurrent,
        "historical_recurrent_blockers": historical_recurrent,
        "consecutive_review_days": consecutive_review,
        "days_since_last_continue": days_since_continue,
        "latest_stop_error": sorted(latest_stop_error, key=lambda item: str(item.get("as_of_date")), reverse=True)[:5],
        "warnings": _dedupe_strings(warnings),
    }


def _history_week_tokens(as_of: date, history_weeks: int) -> list[str]:
    tokens: list[str] = []
    cursor = as_of
    while len(tokens) < history_weeks:
        token = _week_token(cursor)
        if token not in tokens:
            tokens.append(token)
        cursor -= timedelta(days=7)
    return tokens


def _decision_blocker_codes(payload: Mapping[str, object]) -> list[str]:
    codes: list[str] = []
    decision = str(payload.get("decision") or payload.get("state") or "").upper()
    if decision == "STOP":
        codes.append("stop_decision")
    elif decision == "ERROR":
        codes.append("error_decision")
    elif decision == "REVIEW":
        codes.append("review_decision")
    for blocker in _object_list(payload.get("blockers")):
        if isinstance(blocker, Mapping):
            code = str(blocker.get("code") or "")
            if code:
                codes.append(code)
        elif blocker not in {None, ""}:
            codes.append(str(blocker))
    return _dedupe_strings(codes)


def _max_consecutive_review_days(days: Iterable[Mapping[str, object]]) -> int:
    best = 0
    current = 0
    previous_date: date | None = None
    for item in days:
        item_date = _parse_date(str(item.get("as_of_date") or "0001-01-01"))
        if str(item.get("decision") or "").upper() == "REVIEW":
            if previous_date is not None and (item_date - previous_date).days == 1:
                current += 1
            else:
                current = 1
            best = max(best, current)
            previous_date = item_date
        else:
            current = 0
            previous_date = item_date
    return best


def _last_decision_date(days: Iterable[Mapping[str, object]], decision: str) -> date | None:
    matches = [
        _parse_date(str(item.get("as_of_date"))) for item in days if str(item.get("decision") or "").upper() == decision
    ]
    return max(matches) if matches else None


def _discover_named_json(root: Path, filename: str) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name == filename else []
    if (root / filename).exists():
        return [root / filename]
    return sorted(root.rglob(filename))


def _discover_json_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    paths: list[Path] = []
    latest = root / "latest.json"
    if latest.exists():
        paths.append(latest)
    paths.extend(path for path in sorted(root.rglob("*.json")) if path not in paths)
    return paths


def _path_belongs_to_week(path: Path, week: str, *, include_unknown: bool = False) -> bool:
    candidate = _date_from_path(path)
    if candidate is None:
        return include_unknown
    return _week_token(candidate) == week


def _date_from_path(path: Path) -> date | None:
    for part in reversed(path.parts):
        try:
            return date.fromisoformat(part[:10])
        except ValueError:
            continue
    return None


def _resolve_week_token(week: str, *, as_of_date: str) -> str:
    if week != "auto":
        return week
    return _week_token(_parse_date(_resolve_as_of_date(as_of_date)))


def _resolve_as_of_date(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    return value


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _week_token(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _blocker(
    severity: str, code: str, message: str, *, source_path: object = None, count: int | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {"severity": severity, "code": code, "message": message}
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    if count is not None:
        payload["count"] = count
    return payload


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperWeeklySummaryOperationalError("weekly summary must be a JSON object")
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


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


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


def _generated_sort_key(item: Mapping[str, object]) -> tuple[str, str]:
    return (str(item.get("generated_at") or ""), str(item.get("path") or ""))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
