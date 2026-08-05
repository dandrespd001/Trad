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

- Net paper PnL (``net_pnl_usd``): accepted only from a performance report's
  broker-statement-reconciled ``paper_metrics.pnl.realized_pnl``. Proxy or
  local-closeout PnL is never certification evidence. The report must be
  ``status == "OK"``, explicitly mark the PnL certification-eligible, and
  show a matched statement with zero missing, extra, or unreconciled fills.
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
import math
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.autonomy_level import AUTONOMY_MARKETS
from trading_ai.execution.paper_auto_sessions import (
    classify_paper_auto_session,
)
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "2.0"
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


@dataclass(frozen=True)
class N0CertificationValidation:
    """Fail-closed verification result for a persisted N0 certificate."""

    valid: bool
    blockers: tuple[str, ...]
    evidence: dict[str, object]


_HASH_EXCLUDED_FIELDS = frozenset({"artifact_hash", "suggested_certify_command"})


def compute_certification_hash(payload: Mapping[str, object]) -> str:
    """Return a stable sha256 hex digest of ``payload``.

    ``artifact_hash`` is excluded because it is the digest being computed.
    ``generated_at`` is deliberately included: an evidence timestamp is part
    of the attestation and must not be mutable without invalidating its hash.
    ``suggested_certify_command`` is excluded because it is a derived,
    circular field: it embeds the ``artifact_hash`` itself and is filled in
    only *after* the hash is computed, so including it would make the stored
    hash of a CERTIFIED_READY artifact unverifiable -- recomputing this
    function over the on-disk ``certification.json`` must always reproduce
    the stored ``artifact_hash``.
    """

    body = {key: value for key, value in payload.items() if key not in _HASH_EXCLUDED_FIELDS}
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
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
    if not _valid_iso_date(as_of_date):
        raise ValueError("as_of_date must be a valid ISO calendar date")
    if (
        not isinstance(min_clean_days, int)
        or isinstance(min_clean_days, bool)
        or min_clean_days < DEFAULT_MIN_CLEAN_DAYS
    ):
        raise ValueError(f"min_clean_days must be an integer >= {DEFAULT_MIN_CLEAN_DAYS}")
    if (
        isinstance(max_drawdown_pct, bool)
        or not isinstance(max_drawdown_pct, (int, float))
        or not math.isfinite(float(max_drawdown_pct))
        or float(max_drawdown_pct) <= 0
        or float(max_drawdown_pct) > DEFAULT_MAX_DRAWDOWN_PCT
    ):
        raise ValueError(
            f"max_drawdown_pct must be finite, positive, and <= {DEFAULT_MAX_DRAWDOWN_PCT}"
        )

    generated = generated_at or _utc_now()
    if not _valid_aware_timestamp(generated):
        raise ValueError("generated_at must be a timezone-aware ISO timestamp")
    if date.fromisoformat(as_of_date) > _aware_datetime(generated).astimezone(UTC).date():
        raise ValueError("as_of_date cannot be after generated_at")
    session_ledger_inputs = list(session_ledgers)
    if not session_ledger_inputs:
        raise ValueError("at least one session ledger is required")
    session_ledger_paths = [Path(item).resolve() for item in session_ledger_inputs]
    if len(set(session_ledger_paths)) != len(session_ledger_paths):
        raise ValueError("session ledger paths must be unique")
    performance_report_path = Path(performance_report).resolve()

    blockers: list[str] = []
    records, ledger_blockers = _read_certification_session_records(session_ledger_paths)
    blockers.extend(ledger_blockers)

    day_classifications: dict[str, set[str]] = {}
    seen_session_ids: set[str] = set()
    for record in records:
        blockers.extend(
            _validate_session_record(
                record,
                certification_as_of=as_of_date,
                seen_session_ids=seen_session_ids,
            )
        )
        classification, _reasons = _classify_session(record)
        as_of = record.get("as_of_date")
        if not isinstance(as_of, str) or not _valid_iso_date(as_of):
            continue
        day_classifications.setdefault(as_of, set()).add(classification)

    blocked_sessions = sum(
        1 for record in records if _classify_session(record)[0] != "CLEAN"
    )
    if blocked_sessions:
        blockers.append("session_evidence_blocked")

    clean_day_dates = sorted(day for day, classes in day_classifications.items() if classes == {"CLEAN"})
    clean_days = len(clean_day_dates)
    total_sessions = len(records)

    net_pnl: float | None = None
    drawdown: float | None = None

    try:
        performance_payload = _read_strict_json_mapping(performance_report_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        blockers.append("performance_artifact_invalid")
        performance_payload = None

    if performance_payload is not None:
        performance_generated_at = performance_payload.get("generated_at")
        if not isinstance(performance_generated_at, str) or not _valid_aware_timestamp(
            performance_generated_at
        ):
            blockers.append("performance_generated_at_invalid")
        if performance_payload.get("status") != "OK":
            blockers.append("performance_status_not_ok")
        if performance_payload.get("blockers") != []:
            blockers.append("performance_has_blockers")
        if performance_payload.get("warnings") != []:
            blockers.append("performance_has_warnings")
        if performance_payload.get("diagnostics") != []:
            blockers.append("performance_has_diagnostics")

        report_as_of_date = _extract_report_as_of_date(performance_payload)
        if report_as_of_date != as_of_date:
            blockers.append("performance_as_of_date_mismatch")

        net_pnl = _extract_net_pnl(performance_payload)
        drawdown = _extract_drawdown_pct(performance_payload)
        if not _performance_uses_reconciled_pnl(performance_payload):
            blockers.append("performance_pnl_not_broker_reconciled")
        if net_pnl is None or drawdown is None:
            blockers.append("performance_fields_missing")
        else:
            if net_pnl <= 0:
                blockers.append("net_pnl_not_positive")
            if drawdown < 0 or drawdown > max_drawdown_pct:
                blockers.append("drawdown_above_limit")

        safety_block = performance_payload.get("safety")
        safety_block = safety_block if isinstance(safety_block, Mapping) else {}
        if _dangerous_safety_flag(safety_block):
            blockers.append("performance_safety_flag")

    try:
        source_manifest = {
            "session_ledgers": [_source_manifest_entry(path) for path in session_ledger_paths],
            "performance_report": _source_manifest_entry(performance_report_path),
        }
    except OSError:
        blockers.append("source_manifest_unavailable")
        source_manifest = {"session_ledgers": [], "performance_report": None}

    blockers = sorted(set(blockers))

    if blockers:
        status = "BLOCKED"
    elif clean_days >= min_clean_days:
        status = "CERTIFIED_READY"
    else:
        status = "ACCUMULATING"

    remaining_clean_days = max(int(min_clean_days) - clean_days, 0) if status == "ACCUMULATING" else 0

    output_root = Path(output_dir) / market / as_of_date
    output_path = output_root / "certification.json"
    markdown_path = output_root / "certification.md"

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
            "session_ledgers": [str(path) for path in session_ledger_paths],
            "performance_report": str(performance_report_path),
        },
        "source_manifest": source_manifest,
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
            f"--evidence-artifact {shlex.quote(str(output_path.resolve()))} "
            "--reviewer <REVIEWER> --reason <REASON>"
        )

    write_json_artifact(payload, output_path)
    write_text_artifact(render_n0_certification_markdown(payload), markdown_path)

    if status == "BLOCKED":
        exit_code = paper_exit_code(PAPER_BLOCKED)
    elif status == "ACCUMULATING":
        exit_code = paper_exit_code(PAPER_WARN)
    else:
        exit_code = paper_exit_code(PAPER_OK)

    return N0CertificationResult(exit_code=exit_code, status=status, output_path=output_path, payload=payload)


def validate_n0_certification_artifact(
    artifact_path: str | Path,
    *,
    market: str,
    required_clean_days: int = DEFAULT_MIN_CLEAN_DAYS,
    max_allowed_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
) -> N0CertificationValidation:
    """Verify a persisted N0 certificate and its complete source chain.

    The embedded summary is never trusted on its own. The certificate hash,
    every source digest, session identities/classifications, and the
    broker-reconciled performance facts are recomputed from disk. Any unknown
    producer schema or malformed field blocks promotion.
    """

    path = Path(artifact_path)
    if (
        market not in AUTONOMY_MARKETS
        or not isinstance(required_clean_days, int)
        or isinstance(required_clean_days, bool)
        or required_clean_days < DEFAULT_MIN_CLEAN_DAYS
        or isinstance(max_allowed_drawdown_pct, bool)
        or not isinstance(max_allowed_drawdown_pct, (int, float))
        or not math.isfinite(float(max_allowed_drawdown_pct))
        or float(max_allowed_drawdown_pct) <= 0
        or float(max_allowed_drawdown_pct) > DEFAULT_MAX_DRAWDOWN_PCT
    ):
        return N0CertificationValidation(
            valid=False,
            blockers=("evidence_validator_config_invalid",),
            evidence={"artifact_path": str(path)},
        )
    blockers: list[str] = []
    try:
        payload = _read_strict_json_mapping(path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return N0CertificationValidation(
            valid=False,
            blockers=("evidence_artifact_invalid",),
            evidence={"artifact_path": str(path)},
        )

    if payload.get("schema_version") != SCHEMA_VERSION:
        blockers.append("evidence_schema_mismatch")
    if payload.get("status") != "CERTIFIED_READY":
        blockers.append("evidence_status_not_certified_ready")
    if payload.get("market") != market:
        blockers.append("evidence_market_mismatch")
    if payload.get("evidence_kind") != EVIDENCE_KIND:
        blockers.append("evidence_kind_mismatch")
    if payload.get("blockers") != []:
        blockers.append("evidence_contains_blockers")

    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not _valid_aware_timestamp(generated_at):
        blockers.append("evidence_generated_at_invalid")
    as_of_date = payload.get("as_of_date")
    if (
        not isinstance(as_of_date, str)
        or not _valid_iso_date(as_of_date)
        or (
            isinstance(generated_at, str)
            and _valid_aware_timestamp(generated_at)
            and date.fromisoformat(as_of_date)
            > _aware_datetime(generated_at).astimezone(UTC).date()
        )
    ):
        blockers.append("evidence_as_of_date_invalid")

    stored_hash = payload.get("artifact_hash")
    if not _valid_sha256(stored_hash):
        blockers.append("evidence_artifact_hash_invalid")
        verified_hash = ""
    else:
        verified_hash = str(stored_hash)
        try:
            if compute_certification_hash(payload) != verified_hash:
                blockers.append("evidence_artifact_hash_mismatch")
        except (TypeError, ValueError):
            blockers.append("evidence_artifact_hash_mismatch")

    clean_days = _strict_nonnegative_int(payload.get("clean_days"))
    min_clean_days = _strict_nonnegative_int(payload.get("min_clean_days"))
    total_sessions = _strict_nonnegative_int(payload.get("total_sessions"))
    blocked_sessions = _strict_nonnegative_int(payload.get("blocked_sessions"))
    if clean_days is None or clean_days < required_clean_days:
        blockers.append("insufficient_clean_days")
    if min_clean_days is None or min_clean_days < required_clean_days:
        blockers.append("evidence_min_clean_days_too_low")
    if payload.get("remaining_clean_days") != 0:
        blockers.append("evidence_remaining_clean_days_invalid")
    if total_sessions is None or total_sessions < required_clean_days:
        blockers.append("evidence_total_sessions_invalid")
    if blocked_sessions != 0:
        blockers.append("evidence_blocked_sessions_present")

    clean_dates_raw = payload.get("clean_day_dates")
    clean_dates = clean_dates_raw if isinstance(clean_dates_raw, list) else []
    if not isinstance(clean_dates_raw, list) or (
        any(not isinstance(item, str) or not _valid_iso_date(item) for item in clean_dates)
        or len(set(clean_dates)) != len(clean_dates)
        or clean_dates != sorted(clean_dates)
        or clean_days != len(clean_dates)
        or (isinstance(as_of_date, str) and any(str(item) > as_of_date for item in clean_dates))
    ):
        blockers.append("evidence_clean_day_dates_invalid")

    net_pnl = _numeric(payload.get("net_pnl_usd"))
    observed_drawdown = _numeric(payload.get("max_drawdown_pct_observed"))
    stored_drawdown_limit = _numeric(payload.get("max_drawdown_pct_limit"))
    if net_pnl is None or net_pnl <= 0:
        blockers.append("net_pnl_not_positive")
    if (
        observed_drawdown is None
        or observed_drawdown < 0
        or stored_drawdown_limit is None
        or stored_drawdown_limit <= 0
        or stored_drawdown_limit > max_allowed_drawdown_pct
        or observed_drawdown > stored_drawdown_limit
    ):
        blockers.append("evidence_drawdown_invalid")

    safety = payload.get("safety")
    if not isinstance(safety, Mapping) or _dangerous_safety_flag(safety):
        blockers.append("evidence_safety_invalid")

    session_paths, performance_path = _validate_source_manifest(
        payload,
        blockers=blockers,
    )
    recomputed_records: list[dict[str, object]] = []
    recomputed_clean_dates: list[str] = []
    recomputed_blocked = 0
    if session_paths and isinstance(as_of_date, str) and _valid_iso_date(as_of_date):
        recomputed_records, source_blockers = _read_certification_session_records(session_paths)
        blockers.extend(source_blockers)
        day_classes: dict[str, set[str]] = {}
        seen_ids: set[str] = set()
        for record in recomputed_records:
            blockers.extend(
                _validate_session_record(
                    record,
                    certification_as_of=as_of_date,
                    seen_session_ids=seen_ids,
                )
            )
            classification, _reasons = _classify_session(record)
            if classification != "CLEAN":
                recomputed_blocked += 1
            record_date = record.get("as_of_date")
            if isinstance(record_date, str) and _valid_iso_date(record_date):
                day_classes.setdefault(record_date, set()).add(classification)
        recomputed_clean_dates = sorted(
            day for day, classes in day_classes.items() if classes == {"CLEAN"}
        )
        if recomputed_blocked:
            blockers.append("session_evidence_blocked")
        if (
            clean_days != len(recomputed_clean_dates)
            or clean_dates != recomputed_clean_dates
            or total_sessions != len(recomputed_records)
            or blocked_sessions != recomputed_blocked
        ):
            blockers.append("evidence_session_summary_mismatch")

    if performance_path is not None:
        try:
            performance = _read_strict_json_mapping(performance_path)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            blockers.append("performance_artifact_invalid")
        else:
            blockers.extend(
                _validate_performance_for_certification(
                    performance,
                    as_of_date=as_of_date if isinstance(as_of_date, str) else "",
                    max_drawdown_pct=max_allowed_drawdown_pct,
                )
            )
            source_pnl = _extract_net_pnl(performance)
            source_drawdown = _extract_drawdown_pct(performance)
            if not _same_finite_number(net_pnl, source_pnl):
                blockers.append("evidence_performance_summary_mismatch")
            if not _same_finite_number(observed_drawdown, source_drawdown):
                blockers.append("evidence_performance_summary_mismatch")

    # Close the verify/read race for ordinary filesystem changes by checking
    # all source digests again after their semantic contents were consumed.
    if session_paths and performance_path is not None:
        _validate_source_manifest(payload, blockers=blockers)

    blockers = sorted(set(blockers))
    evidence = {
        "artifact_path": str(path.resolve()),
        "artifact_hash": verified_hash,
        "evidence_kind": payload.get("evidence_kind"),
        "clean_days": clean_days,
        "as_of_date": as_of_date,
        "market": payload.get("market"),
        "source_chain_verified": not blockers,
    }
    return N0CertificationValidation(
        valid=not blockers,
        blockers=tuple(blockers),
        evidence=evidence,
    )


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


def _read_certification_session_records(
    paths: Iterable[Path],
) -> tuple[list[dict[str, object]], list[str]]:
    records: list[dict[str, object]] = []
    blockers: list[str] = []
    for path in paths:
        try:
            if not path.is_file():
                blockers.append("session_ledger_missing")
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            blockers.append("session_ledger_unreadable")
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(
                    line,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_strict_object_pairs,
                )
            except (json.JSONDecodeError, ValueError):
                blockers.append("session_ledger_invalid_json")
                continue
            if not isinstance(payload, Mapping):
                blockers.append("session_ledger_invalid_record")
                continue
            if payload.get("record_type") != "paper_auto_cycle_session":
                blockers.append("session_ledger_unknown_record_type")
                continue
            records.append(dict(payload))
    if not records:
        blockers.append("session_evidence_empty")
    return records, blockers


def _validate_session_record(
    record: Mapping[str, object],
    *,
    certification_as_of: str,
    seen_session_ids: set[str],
) -> list[str]:
    blockers: list[str] = []
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        blockers.append("session_id_invalid")
    elif session_id in seen_session_ids:
        blockers.append("session_id_duplicate")
    else:
        seen_session_ids.add(session_id)

    record_date = record.get("as_of_date")
    if (
        not isinstance(record_date, str)
        or not _valid_iso_date(record_date)
        or record_date > certification_as_of
    ):
        blockers.append("session_as_of_date_invalid")
    generated_at = record.get("generated_at")
    if not isinstance(generated_at, str) or not _valid_aware_timestamp(generated_at):
        blockers.append("session_generated_at_invalid")
    if not isinstance(record.get("exit_code"), int) or isinstance(record.get("exit_code"), bool):
        blockers.append("session_exit_code_invalid")
    if not isinstance(record.get("confirm_paper_auto"), bool):
        blockers.append("session_confirmation_invalid")
    if _strict_nonnegative_int(record.get("unreconciled_fills")) is None:
        blockers.append("session_unreconciled_fills_invalid")
    record_blockers = record.get("blockers")
    if not isinstance(record_blockers, list) or any(
        not isinstance(item, str) or not item.strip() for item in record_blockers
    ):
        blockers.append("session_blockers_invalid")

    classification, _reasons = _classify_session(record)
    if classification == "CLEAN" and (
        record.get("state") != "PAPER_CLOSED"
        or record.get("exit_code") != 0
        or record.get("confirm_paper_auto") is not True
        or record.get("order_state") != "paper_order_sent"
        or record.get("closeout_status") != "CLOSED"
        or record.get("statement_status") != "MATCHED"
        or record.get("unreconciled_fills") != 0
        or record.get("blockers") != []
    ):
        blockers.append("session_clean_contract_invalid")
    return blockers


def _validate_performance_for_certification(
    payload: Mapping[str, object],
    *,
    as_of_date: str,
    max_drawdown_pct: float,
) -> list[str]:
    blockers: list[str] = []
    generated_at = payload.get("generated_at")
    if (
        not isinstance(generated_at, str)
        or not _valid_aware_timestamp(generated_at)
        or (
            _valid_iso_date(as_of_date)
            and date.fromisoformat(as_of_date)
            > _aware_datetime(generated_at).astimezone(UTC).date()
        )
    ):
        blockers.append("performance_generated_at_invalid")
    if payload.get("status") != "OK":
        blockers.append("performance_status_not_ok")
    if payload.get("blockers") != []:
        blockers.append("performance_has_blockers")
    if payload.get("warnings") != []:
        blockers.append("performance_has_warnings")
    if payload.get("diagnostics") != []:
        blockers.append("performance_has_diagnostics")
    if _extract_report_as_of_date(payload) != as_of_date:
        blockers.append("performance_as_of_date_mismatch")
    if not _performance_uses_reconciled_pnl(payload):
        blockers.append("performance_pnl_not_broker_reconciled")
    net_pnl = _extract_net_pnl(payload)
    if net_pnl is None:
        blockers.append("performance_fields_missing")
    elif net_pnl <= 0:
        blockers.append("net_pnl_not_positive")
    drawdown = _extract_drawdown_pct(payload)
    if drawdown is None:
        blockers.append("performance_fields_missing")
    elif drawdown < 0 or drawdown > max_drawdown_pct:
        blockers.append("drawdown_above_limit")
    safety = payload.get("safety")
    if not isinstance(safety, Mapping) or _dangerous_safety_flag(safety):
        blockers.append("performance_safety_flag")
    return blockers


def _performance_uses_reconciled_pnl(payload: Mapping[str, object]) -> bool:
    paper_metrics = payload.get("paper_metrics")
    pnl = paper_metrics.get("pnl") if isinstance(paper_metrics, Mapping) else None
    if not isinstance(pnl, Mapping):
        return False
    if (
        pnl.get("source") != "broker_statement"
        or pnl.get("broker_statement") is not True
        or pnl.get("certification_eligible") is not True
        or _numeric(pnl.get("realized_pnl")) is None
    ):
        return False
    statement = payload.get("statement_reconciliation")
    statement_status = payload.get("statement_status")
    if not isinstance(statement, Mapping) or statement.get("status") != "MATCHED":
        return False
    if not isinstance(statement_status, Mapping) or statement_status.get("status") != "MATCHED":
        return False
    for field in ("missing_fills", "extra_fills"):
        if _strict_nonnegative_int(statement.get(field)) != 0:
            return False
    return _strict_nonnegative_int(statement_status.get("unreconciled_fills")) == 0


def _source_manifest_entry(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise OSError(f"source is not a regular file: {resolved}")
    return {
        "path": str(resolved),
        "sha256": _sha256_path(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _validate_source_manifest(
    payload: Mapping[str, object],
    *,
    blockers: list[str],
) -> tuple[list[Path], Path | None]:
    manifest = payload.get("source_manifest")
    sources = payload.get("sources")
    if not isinstance(manifest, Mapping) or not isinstance(sources, Mapping):
        blockers.append("evidence_source_manifest_invalid")
        return [], None
    ledger_entries = manifest.get("session_ledgers")
    performance_entry = manifest.get("performance_report")
    if not isinstance(ledger_entries, list) or not ledger_entries:
        blockers.append("evidence_source_manifest_invalid")
        return [], None
    ledger_paths: list[Path] = []
    for entry in ledger_entries:
        verified = _verified_manifest_path(entry, blockers=blockers)
        if verified is not None:
            ledger_paths.append(verified)
    performance_path = _verified_manifest_path(performance_entry, blockers=blockers)

    source_ledgers = sources.get("session_ledgers")
    source_performance = sources.get("performance_report")
    if (
        not isinstance(source_ledgers, list)
        or [str(path) for path in ledger_paths] != source_ledgers
        or (performance_path is not None and str(performance_path) != source_performance)
    ):
        blockers.append("evidence_sources_mismatch")
    if len(set(ledger_paths)) != len(ledger_paths):
        blockers.append("evidence_source_duplicate")
    return ledger_paths, performance_path


def _verified_manifest_path(entry: object, *, blockers: list[str]) -> Path | None:
    if not isinstance(entry, Mapping):
        blockers.append("evidence_source_manifest_invalid")
        return None
    path_raw = entry.get("path")
    digest = entry.get("sha256")
    size = _strict_nonnegative_int(entry.get("size_bytes"))
    if not isinstance(path_raw, str) or not Path(path_raw).is_absolute() or not _valid_sha256(digest):
        blockers.append("evidence_source_manifest_invalid")
        return None
    path = Path(path_raw)
    try:
        if not path.is_file() or size != path.stat().st_size or digest != _sha256_path(path):
            blockers.append("evidence_source_integrity_mismatch")
            return None
    except OSError:
        blockers.append("evidence_source_integrity_mismatch")
        return None
    return path


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_strict_json_mapping(path: Path) -> dict[str, object]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_object_pairs,
    )
    if not isinstance(payload, Mapping):
        raise TypeError("JSON artifact root must be an object")
    return dict(payload)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _classify_session(record: Mapping[str, object]) -> tuple[str, list[str]]:
    try:
        return classify_paper_auto_session(record)
    except (OverflowError, TypeError, ValueError):
        return "BLOCKED", ["session_record_invalid"]


def _valid_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_aware_timestamp(value: str) -> bool:
    try:
        parsed = _aware_datetime(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _aware_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _strict_nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _same_finite_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)


def _extract_report_as_of_date(payload: Mapping[str, object]) -> str | None:
    for key in ("as_of_date", "session_date"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if value is not None:
            return None
    paper_metrics = payload.get("paper_metrics")
    if isinstance(paper_metrics, Mapping):
        dates = paper_metrics.get("dates")
        if isinstance(dates, Mapping):
            end = dates.get("end")
            if isinstance(end, str) and end:
                return end
    return None


def _extract_net_pnl(payload: Mapping[str, object]) -> float | None:
    paper_metrics = payload.get("paper_metrics")
    pnl = paper_metrics.get("pnl") if isinstance(paper_metrics, Mapping) else None
    pnl = pnl if isinstance(pnl, Mapping) else {}
    if str(pnl.get("source") or "") == "broker_statement":
        realized = _numeric(pnl.get("realized_pnl"))
        if realized is not None:
            return realized
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
    if safety.get("broker_client_built") is not False:
        return True
    if safety.get("credentials_read") is not False:
        return True
    if safety.get("orders_submitted") is True:
        return True
    return (
        safety.get("live_trading_authorized") is not False
        or safety.get("live_trading_allowed") is not False
    )


def _numeric(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
