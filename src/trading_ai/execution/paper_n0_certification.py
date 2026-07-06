"""N0 paper-evidence certification report (governance metadata; never executes orders).

This module accumulates the evidence required by
``autonomy_level.PROMOTION_EVIDENCE_REQUIREMENTS["N1_REAL_CANARY"]`` (see
``docs/autonomy-ladder.md``, Sprint A3): at least ``min_clean_days`` distinct
calendar days of clean paper-auto sessions, plus a paper performance report
showing a positive net PnL and a drawdown within limits. It never builds a
broker client, never reads credentials, and never submits an order; it only
reads existing paper-auto session ledgers and a paper performance report and
writes a read-only certification artifact (JSON + Markdown) that an operator
can hand to ``autonomy-certify`` once ``status == "CERTIFIED_READY"``.

Field provenance (documented per spec -- "no inventes nombres", use the real
field names emitted elsewhere in this codebase):

- Net paper PnL (``net_pnl_usd``): read from the performance report's
  ``paper_metrics.pnl`` block (as produced by
  ``paper_performance.build_paper_performance_report``). ``realized_pnl`` is
  used only when the report is broker-statement reconciled
  (``pnl.source == "broker_statement"``); in any other case the proxy
  ``proxy_unrealized_pnl`` (local-closeout based) is used. As a last resort,
  a ``performance.daily_pnl`` field (as emitted by
  ``paper_performance.consolidated_dashboard``) is accepted for artifacts
  produced by that alternate reporting path. Absence of all three is a
  fail-closed ``performance_fields_missing`` blocker -- a missing PnL never
  reads as zero or as "pass".
- Drawdown (``max_drawdown_pct_observed``): read from
  ``performance.max_drawdown_pct`` and/or ``risk.current_drawdown_pct``
  (the two real blocks emitted by ``paper_performance.consolidated_dashboard``
  -- deliberately *not* ``paper_vs_backtest.backtest_metrics.max_drawdown``,
  which reflects the backtest, not paper operation). When both fields are
  present the worse (higher) one is used so the check never understates
  risk. Absence of both is fail-closed ``performance_fields_missing``.
- Report ``as_of_date`` (for the mismatch check): the first of
  ``as_of_date``, ``session_date`` (``consolidated_dashboard``), or
  ``paper_metrics.dates.end`` (``build_paper_performance_report``) that is
  present.

A "clean day" is a calendar ``as_of_date`` for which *every* paper-auto
session recorded that day classifies as ``CLEAN`` per
``paper_auto_sessions.classify_paper_auto_session`` -- a single BLOCKED
session on an otherwise-clean day voids that day's evidence (fail-closed per
day, not per session).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.autonomy_level import AUTONOMY_MARKETS
from trading_ai.execution.paper_auto_sessions import (
    classify_paper_auto_session,
    read_paper_auto_session_records,
    summarize_paper_auto_sessions,
)
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/n0_certification"
DEFAULT_MIN_CLEAN_DAYS = 20
DEFAULT_MAX_DRAWDOWN_PCT = 10.0
EVIDENCE_KIND = "paper_certification"

STATUSES: tuple[str, ...] = ("CERTIFIED_READY", "ACCUMULATING", "BLOCKED")

_TARGET_LEVEL = "N1_REAL_CANARY"


@dataclass(frozen=True)
class N0CertificationResult:
    """Outcome of a run of the N0 paper-evidence certification report."""

    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


_HASH_EXCLUDED_FIELDS = frozenset({"generated_at", "artifact_hash", "suggested_certify_command"})


def compute_certification_hash(payload: Mapping[str, object]) -> str:
    """Return a stable sha256 hex digest of ``payload``.

    ``generated_at`` and ``artifact_hash`` are excluded so that two
    regenerations of the same logical evidence (same clean days, same
    performance figures) hash identically, in the same idiom as
    ``paper_signal_approval.compute_plan_hash``.
    ``suggested_certify_command`` is excluded because it is a derived,
    circular field: it embeds the ``artifact_hash`` itself and is filled in
    only *after* the hash is computed, so including it would make the stored
    hash of a CERTIFIED_READY artifact unverifiable -- recomputing this
    function over the on-disk ``certification.json`` must always reproduce
    the stored ``artifact_hash``.
    """

    body = {key: value for key, value in payload.items() if key not in _HASH_EXCLUDED_FIELDS}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_paper_n0_certification(
    *,
    as_of_date: str,
    market: str = "equities",
    session_ledgers: Iterable[str | Path],
    performance_report: str | Path,
    min_clean_days: int = DEFAULT_MIN_CLEAN_DAYS,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> N0CertificationResult:
    if market not in AUTONOMY_MARKETS:
        raise ValueError(f"unknown autonomy market: {market!r}")

    generated = generated_at or _utc_now()
    session_ledger_inputs = list(session_ledgers)
    session_ledger_paths = [str(Path(item)) for item in session_ledger_inputs]
    performance_report_path = Path(performance_report)

    blockers: list[str] = []

    records, _diagnostics = read_paper_auto_session_records(session_ledger_inputs)
    summary = summarize_paper_auto_sessions(session_ledger_inputs)

    day_classifications: dict[str, set[str]] = {}
    missing_as_of_date = 0
    for record in records:
        classification, _reasons = classify_paper_auto_session(record)
        as_of = record.get("as_of_date")
        if not as_of:
            missing_as_of_date += 1
            continue
        day_classifications.setdefault(str(as_of), set()).add(classification)

    if missing_as_of_date:
        blockers.append("session_missing_as_of_date")

    diagnostics = summary.get("diagnostics")
    diagnostics_list = diagnostics if isinstance(diagnostics, list) else []
    blocked_sessions = _int_value(summary.get("blocked_sessions"))
    if diagnostics_list or blocked_sessions:
        blockers.append("session_evidence_blocked")

    clean_day_dates = sorted(day for day, classes in day_classifications.items() if classes == {"CLEAN"})
    clean_days = len(clean_day_dates)
    total_sessions = len(records)

    net_pnl: float | None = None
    drawdown: float | None = None

    try:
        performance_payload: Mapping[str, object] | None = read_json_artifact(performance_report_path)
    except (OSError, json.JSONDecodeError, ValueError):
        blockers.append("performance_artifact_invalid")
        performance_payload = None

    if performance_payload is not None:
        report_as_of_date = _extract_report_as_of_date(performance_payload)
        if report_as_of_date and report_as_of_date != as_of_date:
            blockers.append("performance_as_of_date_mismatch")

        net_pnl = _extract_net_pnl(performance_payload)
        drawdown = _extract_drawdown_pct(performance_payload)
        if net_pnl is None or drawdown is None:
            blockers.append("performance_fields_missing")
        else:
            if net_pnl <= 0:
                blockers.append("net_pnl_not_positive")
            if drawdown > max_drawdown_pct:
                blockers.append("drawdown_above_limit")

        safety_block = performance_payload.get("safety")
        safety_block = safety_block if isinstance(safety_block, Mapping) else {}
        if _dangerous_safety_flag(safety_block):
            blockers.append("performance_safety_flag")

    blockers = sorted(set(blockers))

    if blockers:
        status = "BLOCKED"
    elif clean_days >= min_clean_days:
        status = "CERTIFIED_READY"
    else:
        status = "ACCUMULATING"

    remaining_clean_days = max(int(min_clean_days) - clean_days, 0) if status == "ACCUMULATING" else 0

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "market": market,
        "status": status,
        "clean_days": clean_days,
        "min_clean_days": int(min_clean_days),
        "remaining_clean_days": remaining_clean_days,
        "clean_day_dates": clean_day_dates,
        "total_sessions": total_sessions,
        "blocked_sessions": blocked_sessions,
        "net_pnl_usd": net_pnl,
        "max_drawdown_pct_observed": drawdown,
        "max_drawdown_pct_limit": float(max_drawdown_pct),
        "evidence_kind": EVIDENCE_KIND,
        "blockers": blockers,
        "sources": {
            "session_ledgers": session_ledger_paths,
            "performance_report": str(performance_report_path),
        },
        "suggested_certify_command": "",
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }

    artifact_hash = compute_certification_hash(payload)
    payload["artifact_hash"] = artifact_hash

    if status == "CERTIFIED_READY":
        payload["suggested_certify_command"] = (
            f"trading-ai autonomy-certify --market {market} --target-level {_TARGET_LEVEL} "
            f"--clean-days {clean_days} --evidence-kind {EVIDENCE_KIND} "
            f"--artifact-hash {artifact_hash} --reviewer <REVIEWER> --reason <REASON>"
        )

    output_root = Path(output_dir) / market / as_of_date
    output_path = output_root / "certification.json"
    markdown_path = output_root / "certification.md"
    write_json_artifact(payload, output_path)
    write_text_artifact(render_n0_certification_markdown(payload), markdown_path)

    if status == "BLOCKED":
        exit_code = paper_exit_code(PAPER_BLOCKED)
    elif status == "ACCUMULATING":
        exit_code = paper_exit_code(PAPER_WARN)
    else:
        exit_code = paper_exit_code(PAPER_OK)

    return N0CertificationResult(exit_code=exit_code, status=status, output_path=output_path, payload=payload)


def render_n0_certification_markdown(payload: Mapping[str, object]) -> str:
    blockers_value = payload.get("blockers")
    blockers = blockers_value if isinstance(blockers_value, list) else []
    clean_day_dates_value = payload.get("clean_day_dates")
    clean_day_dates = clean_day_dates_value if isinstance(clean_day_dates_value, list) else []
    lines = [
        "# N0 Paper Certification",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{payload.get('generated_at') or ''}`",
        f"Market: `{payload.get('market') or ''}`",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        "",
        "## Evidence",
        "",
        f"Clean days: `{payload.get('clean_days', 0)}` / `{payload.get('min_clean_days', 0)}`",
        f"Remaining clean days: `{payload.get('remaining_clean_days', 0)}`",
        f"Total sessions: `{payload.get('total_sessions', 0)}`",
        f"Blocked sessions: `{payload.get('blocked_sessions', 0)}`",
        f"Net PnL (USD): `{payload.get('net_pnl_usd')}`",
        f"Max drawdown observed (%): `{payload.get('max_drawdown_pct_observed')}`",
        f"Max drawdown limit (%): `{payload.get('max_drawdown_pct_limit')}`",
        f"Evidence kind: `{payload.get('evidence_kind') or ''}`",
        f"Clean day dates: `{', '.join(str(item) for item in clean_day_dates)}`",
        "",
        "## Blockers",
        "",
    ]
    if blockers:
        for blocker in blockers:
            lines.append(f"- `{blocker}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Suggested certify command",
            "",
            f"`{payload.get('suggested_certify_command') or ''}`",
            "",
            f"Artifact hash: `{payload.get('artifact_hash') or ''}`",
            "",
            "Live trading allowed: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _extract_report_as_of_date(payload: Mapping[str, object]) -> str | None:
    for key in ("as_of_date", "session_date"):
        value = payload.get(key)
        if value not in {None, ""}:
            return str(value)
    paper_metrics = payload.get("paper_metrics")
    if isinstance(paper_metrics, Mapping):
        dates = paper_metrics.get("dates")
        if isinstance(dates, Mapping):
            end = dates.get("end")
            if end not in {None, ""}:
                return str(end)
    return None


def _extract_net_pnl(payload: Mapping[str, object]) -> float | None:
    paper_metrics = payload.get("paper_metrics")
    pnl = paper_metrics.get("pnl") if isinstance(paper_metrics, Mapping) else None
    pnl = pnl if isinstance(pnl, Mapping) else {}
    if str(pnl.get("source") or "") == "broker_statement":
        realized = _numeric(pnl.get("realized_pnl"))
        if realized is not None:
            return realized
    proxy = _numeric(pnl.get("proxy_unrealized_pnl"))
    if proxy is not None:
        return proxy
    performance_block = payload.get("performance")
    if isinstance(performance_block, Mapping):
        daily_pnl = _numeric(performance_block.get("daily_pnl"))
        if daily_pnl is not None:
            return daily_pnl
    return None


def _extract_drawdown_pct(payload: Mapping[str, object]) -> float | None:
    candidates: list[float] = []
    performance_block = payload.get("performance")
    if isinstance(performance_block, Mapping):
        value = _numeric(performance_block.get("max_drawdown_pct"))
        if value is not None:
            candidates.append(value)
    risk_block = payload.get("risk")
    if isinstance(risk_block, Mapping):
        value = _numeric(risk_block.get("current_drawdown_pct"))
        if value is not None:
            candidates.append(value)
    if not candidates:
        return None
    return max(candidates)


def _dangerous_safety_flag(safety: Mapping[str, object]) -> bool:
    if safety.get("paper_only") is not True:
        return True
    if safety.get("broker_client_built") is True:
        return True
    if safety.get("credentials_read") is True:
        return True
    if safety.get("orders_submitted") is True:
        return True
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        return True
    return False


def _numeric(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_value(value: object) -> int:
    numeric = _numeric(value)
    return int(numeric) if numeric is not None else 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
