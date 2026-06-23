"""Read-only operator status for confirmed paper-auto cycles."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    reason_codes,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_CYCLE_ROOT = "reports/tmp/paper_auto_cycle"
DEFAULT_LEDGER = "reports/tmp/paper_auto_cycle/session_ledger.jsonl"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_operator_status"


class PaperOperatorStatusOperationalError(RuntimeError):
    """Raised when paper operator status cannot be produced."""


@dataclass(frozen=True)
class PaperOperatorStatusResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_operator_status(
    *,
    as_of_date: str,
    cycle_root: str | Path = DEFAULT_CYCLE_ROOT,
    ledger: str | Path = DEFAULT_LEDGER,
    monitor: str | Path | None = None,
    performance: str | Path | None = None,
    lock_dir: str | Path | None = None,
    max_lock_age_minutes: int = 90,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperOperatorStatusResult:
    generated = generated_at or _utc_now()
    payload = build_paper_operator_status(
        as_of_date=as_of_date,
        cycle_root=cycle_root,
        ledger=ledger,
        monitor=monitor,
        performance=performance,
        lock_dir=lock_dir,
        max_lock_age_minutes=max_lock_age_minutes,
        generated_at=generated,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "operator_status.json"
    markdown_path = output_root / "operator_status.md"
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_operator_status_markdown(payload), markdown_path)
    status = str(payload.get("status") or "ERROR")
    return PaperOperatorStatusResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def build_paper_operator_status(
    *,
    as_of_date: str,
    cycle_root: str | Path = DEFAULT_CYCLE_ROOT,
    ledger: str | Path = DEFAULT_LEDGER,
    monitor: str | Path | None = None,
    performance: str | Path | None = None,
    lock_dir: str | Path | None = None,
    max_lock_age_minutes: int = 90,
    generated_at: str,
) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    cycle_path = _cycle_path(Path(cycle_root), as_of_date=as_of_date)
    cycle = _read_optional_json(cycle_path)
    if cycle is None:
        blockers.append(
            _blocker("WARNING", "cycle_missing", "latest paper auto cycle is missing", source_path=cycle_path)
        )
        latest_cycle = None
    else:
        latest_cycle = _cycle_summary(cycle_path, cycle)
        blockers.extend(_cycle_blockers(cycle_path, cycle))

    ledger_summary, ledger_blockers = _ledger_summary(Path(ledger))
    blockers.extend(ledger_blockers)
    lock_summary, lock_blockers = _lock_summary(
        lock_dir=lock_dir,
        as_of_date=as_of_date,
        max_lock_age_minutes=max_lock_age_minutes,
    )
    blockers.extend(lock_blockers)

    monitor_summary: dict[str, object] | None = None
    if monitor is not None:
        monitor_payload = _read_optional_json(monitor)
        if monitor_payload is None:
            blockers.append(
                _blocker("ERROR", "monitor_invalid", "monitor artifact is missing or invalid", source_path=monitor)
            )
        else:
            monitor_summary = _artifact_summary(monitor, monitor_payload)
            blockers.extend(_monitor_blockers(monitor, monitor_payload))

    performance_summary: dict[str, object] | None = None
    closeout_status = "UNKNOWN"
    statement_status = "UNKNOWN"
    unreconciled_fills = 0
    if performance is not None:
        performance_payload = _read_optional_json(performance)
        if performance_payload is None:
            blockers.append(
                _blocker(
                    "ERROR",
                    "performance_invalid",
                    "performance artifact is missing or invalid",
                    source_path=performance,
                )
            )
        else:
            performance_summary = _artifact_summary(performance, performance_payload)
            blockers.extend(_performance_blockers(performance, performance_payload))
            metrics = _mapping(performance_payload.get("paper_metrics"))
            pending = _int_value(metrics.get("pending_closeouts"), default=0)
            unmatched = _int_value(metrics.get("unmatched_closeouts"), default=0)
            closeout_status = "PENDING" if pending else "UNMATCHED" if unmatched else "CLEAN"
            statement_summary = _mapping(performance_payload.get("statement_status"))
            statement = _mapping(performance_payload.get("statement_reconciliation"))
            statement_status = str(statement_summary.get("status") or statement.get("status") or "UNKNOWN")
            unreconciled_fills = _int_value(
                statement_summary.get("unreconciled_fills"),
                default=_int_value(statement.get("missing_fills"), default=0),
            )

    blockers = _dedupe_blockers(blockers)
    status = _status_from_blockers(blockers)
    clean = status == "OK"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "status": status,
        "clean_for_paper_auto": clean,
        "next_safe_action": "run_confirmed_paper_auto_cycle" if clean else "resolve_operator_blockers",
        "lock_status": lock_summary["status"],
        "closeout_status": closeout_status,
        "statement_status": statement_status,
        "unreconciled_fills": unreconciled_fills,
        "sources": {
            "cycle_root": str(Path(cycle_root)),
            "cycle": str(cycle_path),
            "ledger": str(Path(ledger)),
            "monitor": str(Path(monitor)) if monitor is not None else None,
            "performance": str(Path(performance)) if performance is not None else None,
            "lock_dir": str(Path(lock_dir)) if lock_dir is not None else None,
        },
        "latest_cycle": latest_cycle,
        "lock": lock_summary,
        "ledger_summary": ledger_summary,
        "monitor_summary": monitor_summary,
        "performance_summary": performance_summary,
        "blockers": blockers,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    return _redact_payload(payload)


def render_paper_operator_status_markdown(payload: Mapping[str, object]) -> str:
    blockers = _object_list(payload.get("blockers"))
    ledger = _mapping(payload.get("ledger_summary"))
    lines = [
        "# Paper Operator Status",
        "",
        f"Status: **{payload.get('status') or 'ERROR'}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Clean for paper auto: `{payload.get('clean_for_paper_auto') is True}`",
        f"Next safe action: `{payload.get('next_safe_action') or ''}`",
        f"Lock status: `{payload.get('lock_status') or 'CLEAR'}`",
        "",
        "## Ledger",
        "",
        f"Clean sessions: `{ledger.get('clean_sessions', 0)}`",
        f"Blocked sessions: `{ledger.get('blocked_sessions', 0)}`",
        f"Broker-confirmed sessions: `{ledger.get('broker_confirmed_sessions', 0)}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Message |",
        "| --- | --- | --- |",
    ]
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
        lines.append("| OK | none | No operator blockers. |")
    lines.extend(["", "Paper only: `True`", "Live trading authorized: `False`", ""])
    return "\n".join(lines)


def _cycle_path(root: Path, *, as_of_date: str) -> Path:
    direct = root / as_of_date / "cycle.json"
    if direct.exists():
        return direct
    latest = sorted(root.rglob("cycle.json")) if root.exists() else []
    return latest[-1] if latest else direct


def _cycle_summary(path: Path, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": str(path),
        "generated_at": str(payload.get("generated_at") or ""),
        "as_of_date": str(payload.get("as_of_date") or ""),
        "state": str(payload.get("state") or "UNKNOWN"),
        "exit_code": _int_value(payload.get("exit_code"), default=2),
        "reasons": reason_codes(payload.get("reasons")),
    }


def _cycle_blockers(path: Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    state = str(payload.get("state") or "").upper()
    if state == "ERROR":
        blockers.append(_blocker("ERROR", "cycle_error", "latest paper auto cycle is ERROR", source_path=path))
    elif state == "BLOCKED":
        for reason in reason_codes(payload.get("reasons")) or ["cycle_blocked"]:
            blockers.append(
                _blocker("CRITICAL", reason, f"latest paper auto cycle is blocked: {reason}", source_path=path)
            )
    elif state == "PAPER_SUBMITTED":
        blockers.append(
            _blocker(
                "CRITICAL",
                "paper_cycle_pending_closeout",
                "previous paper cycle submitted and needs closeout evidence",
                source_path=path,
            )
        )
    blockers.extend(_safety_blockers(path, payload))
    return blockers


def _ledger_summary(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    records: list[dict[str, object]] = []
    blockers: list[dict[str, object]] = []
    if not path.exists():
        return (
            {
                "path": str(path),
                "total_sessions": 0,
                "clean_sessions": 0,
                "blocked_sessions": 0,
                "broker_confirmed_sessions": 0,
                "last_session_id": None,
                "blocker_histogram": {},
            },
            [_blocker("WARNING", "session_ledger_missing", "paper auto session ledger is missing", source_path=path)],
        )
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            blockers.append(
                _blocker(
                    "ERROR",
                    "session_ledger_invalid_json",
                    f"invalid ledger JSON at line {line_number}: {exc}",
                    source_path=path,
                )
            )
            continue
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    clean_sessions = [
        record
        for record in records
        if str(record.get("state") or "").upper() in {"PAPER_SUBMITTED", "PAPER_CLOSED"}
        and not reason_codes(record.get("blockers"))
    ]
    blocked_sessions = [
        record
        for record in records
        if str(record.get("state") or "").upper() in {"BLOCKED", "ERROR"} or reason_codes(record.get("blockers"))
    ]
    histogram: dict[str, int] = {}
    for record in blocked_sessions:
        for blocker in reason_codes(record.get("blockers")) or [str(record.get("state") or "blocked").lower()]:
            histogram[blocker] = histogram.get(blocker, 0) + 1
    last = records[-1] if records else {}
    return (
        {
            "path": str(path),
            "total_sessions": len(records),
            "clean_sessions": len(clean_sessions),
            "blocked_sessions": len(blocked_sessions),
            "broker_confirmed_sessions": sum(1 for record in records if record.get("confirm_paper_auto") is True),
            "last_session_id": last.get("session_id"),
            "blocker_histogram": dict(sorted(histogram.items())),
        },
        blockers,
    )


def _lock_summary(
    *,
    lock_dir: str | Path | None,
    as_of_date: str,
    max_lock_age_minutes: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if lock_dir is None:
        return (
            {
                "status": "CLEAR",
                "path": None,
                "age_minutes": None,
                "max_lock_age_minutes": int(max_lock_age_minutes),
                "recommended_action": "none",
            },
            [],
        )
    path = Path(lock_dir) / f"paper_auto_cycle_{as_of_date}.lock"
    if not path.exists():
        return (
            {
                "status": "CLEAR",
                "path": str(path),
                "age_minutes": None,
                "max_lock_age_minutes": int(max_lock_age_minutes),
                "recommended_action": "none",
            },
            [],
        )
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        age_minutes = max((datetime.now(UTC) - modified).total_seconds() / 60.0, 0.0)
    except OSError:
        age_minutes = None
    stale = age_minutes is None or age_minutes > float(max_lock_age_minutes)
    status = "STALE" if stale else "ACTIVE"
    code = "cycle_lock_stale" if stale else "cycle_lock_active"
    action = "inspect_lock_then_remove_if_no_cycle_running" if stale else "wait_for_active_cycle_or_investigate"
    return (
        {
            "status": status,
            "path": str(path),
            "age_minutes": age_minutes,
            "max_lock_age_minutes": int(max_lock_age_minutes),
            "recommended_action": action,
        },
        [_blocker("CRITICAL", code, f"paper auto cycle lock is {status.lower()}", source_path=path)],
    )


def _monitor_blockers(path: str | Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if status == "CRITICAL":
        blockers.append(_blocker("CRITICAL", "monitor_critical", "paper monitor is CRITICAL", source_path=path))
    elif status == "ERROR":
        blockers.append(_blocker("ERROR", "monitor_error", "paper monitor is ERROR", source_path=path))
    counts = _mapping(_mapping(payload.get("broker_snapshot")).get("counts"))
    if _int_value(counts.get("orders"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "open_broker_orders", "paper broker snapshot has open orders", source_path=path)
        )
    if _int_value(counts.get("positions"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "existing_positions", "paper broker snapshot has positions", source_path=path)
        )
    for alert in _object_list(payload.get("alerts")):
        if not isinstance(alert, Mapping):
            continue
        severity = str(alert.get("severity") or "").upper()
        if severity in {"CRITICAL", "ERROR"}:
            blockers.append(
                _blocker(
                    severity,
                    str(alert.get("code") or "monitor_alert"),
                    str(alert.get("message") or alert.get("code") or "monitor alert"),
                    source_path=path,
                )
            )
    blockers.extend(_safety_blockers(path, payload))
    return blockers


def _performance_blockers(path: str | Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if status == "ERROR":
        blockers.append(_blocker("ERROR", "performance_error", "paper performance report is ERROR", source_path=path))
    metrics = _mapping(payload.get("paper_metrics"))
    if _int_value(metrics.get("pending_closeouts"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "closeout_pending", "paper performance has pending closeouts", source_path=path)
        )
    if _int_value(metrics.get("unmatched_closeouts"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "closeout_unmatched", "paper performance has unmatched closeouts", source_path=path)
        )
    statement = _mapping(payload.get("statement_reconciliation"))
    statement_status = str(statement.get("status") or "").upper()
    if statement_status in {"ERROR", "MISMATCH", "UNMATCHED", "DIFFERENCES", "MISSING"}:
        blockers.append(
            _blocker(
                "CRITICAL", "statement_mismatch", "paper statement reconciliation is not matched", source_path=path
            )
        )
    statement_summary = _mapping(payload.get("statement_status"))
    if str(statement_summary.get("status") or "").upper() == "STATEMENT_PENDING":
        blockers.append(
            _blocker("CRITICAL", "statement_pending", "paper statement is pending for local fills", source_path=path)
        )
    if _int_value(statement.get("missing_fills"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "fills_unreconciled", "paper statement has unreconciled fills", source_path=path)
        )
    if _int_value(statement_summary.get("unreconciled_fills"), default=0) > 0:
        blockers.append(
            _blocker("CRITICAL", "fills_unreconciled", "paper statement has unreconciled fills", source_path=path)
        )
    for blocker in reason_codes(payload.get("blockers")):
        blockers.append(_blocker("CRITICAL", blocker, blocker, source_path=path))
    blockers.extend(_safety_blockers(path, payload))
    return blockers


def _artifact_summary(path: str | Path, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": str(Path(path)),
        "status": str(payload.get("status") or "UNKNOWN"),
        "generated_at": str(payload.get("generated_at") or ""),
    }


def _safety_blockers(path: str | Path, payload: Mapping[str, object]) -> list[dict[str, object]]:
    safety = _mapping(payload.get("safety"))
    blockers: list[dict[str, object]] = []
    if safety.get("broker_client_built") is True:
        blockers.append(
            _blocker(
                "CRITICAL", "broker_client_built", "broker client was built before operator status", source_path=path
            )
        )
    if safety.get("credentials_read") is True:
        blockers.append(
            _blocker("CRITICAL", "credentials_read", "credentials were read before operator status", source_path=path)
        )
    if safety.get("orders_submitted") is True:
        blockers.append(
            _blocker("CRITICAL", "orders_submitted", "orders were submitted before operator status", source_path=path)
        )
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append(
            _blocker("CRITICAL", "live_trading_not_allowed", "live trading must remain disabled", source_path=path)
        )
    return blockers


def _status_from_blockers(blockers: Iterable[Mapping[str, object]]) -> str:
    severities = {str(blocker.get("severity") or "").upper() for blocker in blockers}
    if "ERROR" in severities:
        return "ERROR"
    if "CRITICAL" in severities:
        return "CRITICAL"
    if "WARNING" in severities:
        return "WARN"
    return "OK"


def _blocker(severity: str, code: str, message: str, *, source_path: object = None) -> dict[str, object]:
    item: dict[str, object] = {"severity": severity, "code": code, "message": redact_secrets(message, env={})}
    if source_path not in {None, ""}:
        item["source_path"] = str(source_path)
    return item


def _dedupe_blockers(blockers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for blocker in blockers:
        item = dict(blocker)
        key = (str(item.get("severity") or ""), str(item.get("code") or ""), str(item.get("source_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _read_optional_json(path: str | Path) -> dict[str, object] | None:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


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


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(_redact_value(payload)))


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
