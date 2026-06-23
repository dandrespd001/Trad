"""Deterministic offline rehearsal for paper operating controls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.evaluation.model_review_decision import run_model_review_decision
from trading_ai.execution.paper_common import (
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_ops_check import run_paper_ops_check
from trading_ai.execution.paper_performance import run_paper_performance_report
from trading_ai.execution.paper_phase_review import run_paper_phase_review_report
from trading_ai.execution.paper_statement import run_paper_statement_validate
from trading_ai.execution.paper_weekly_summary import run_paper_weekly_summary

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_rehearsal"
SCENARIOS = (
    "complete",
    "missing-performance",
    "stop",
    "invalid-statement",
    "open-order",
    "existing-position",
    "stale-dataset",
    "statement-mismatch",
    "fill-unreconciled",
    "malicious-llm-context",
    "59-stable-sessions",
    "60-stable-ready",
    "duplicate-cycle",
    "stale-lock",
    "corrupt-ledger",
    "quality-blocked",
    "phase-not-ready",
    "retrain-due",
    "not-due",
    "duplicate-retrain",
    "candidate-rejected",
    "drift-blocked",
    "shadow-insufficient",
    "shadow-ready",
    "alias-approved",
    "alias-blocked",
    "alias-expired",
    "challenger-underperforms",
    "malicious-alias-llm",
    "alias-invalid-model",
    "malicious-adaptive-llm",
)
BLOCKED_REHEARSAL_REASONS = {
    "open-order": "open_broker_orders",
    "existing-position": "existing_positions",
    "stale-dataset": "dataset_stale",
    "statement-mismatch": "statement_mismatch",
    "fill-unreconciled": "fills_unreconciled",
    "malicious-llm-context": "order_submission_instruction",
}


class PaperOpsRehearsalOperationalError(RuntimeError):
    """Raised when the paper ops rehearsal cannot be produced."""


@dataclass(frozen=True)
class PaperOpsRehearsalResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_ops_rehearsal(
    *,
    as_of_date: str,
    scenario: str = "complete",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperOpsRehearsalResult:
    if scenario not in SCENARIOS:
        raise PaperOpsRehearsalOperationalError(f"unsupported paper ops rehearsal scenario: {scenario}")
    output_root = Path(output_dir) / as_of_date
    report = build_paper_ops_rehearsal(
        as_of_date=as_of_date,
        scenario=scenario,
        output_root=output_root,
        generated_at=generated_at,
    )
    output_path = output_root / "rehearsal.json"
    markdown_path = output_root / "rehearsal.md"
    redacted = _redact_payload(report)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_ops_rehearsal_markdown(redacted), markdown_path)
    status = str(redacted.get("status") or "ERROR")
    return PaperOpsRehearsalResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_ops_rehearsal(
    *,
    as_of_date: str,
    scenario: str,
    output_root: Path,
    generated_at: str | None = None,
) -> dict[str, object]:
    generated = generated_at or _utc_now()
    fixture_root = output_root / "fixtures"
    evidence_root = output_root / "evidence"
    if scenario in {
        "phase-not-ready",
        "retrain-due",
        "not-due",
        "duplicate-retrain",
        "candidate-rejected",
        "drift-blocked",
        "shadow-insufficient",
        "shadow-ready",
        "alias-approved",
        "alias-blocked",
        "alias-expired",
        "challenger-underperforms",
        "malicious-alias-llm",
        "alias-invalid-model",
        "malicious-adaptive-llm",
    }:
        return _adaptive_training_rehearsal_payload(
            as_of_date=as_of_date,
            scenario=scenario,
            generated_at=generated,
            fixture_root=fixture_root,
            evidence_root=evidence_root,
        )
    if scenario in {
        "59-stable-sessions",
        "60-stable-ready",
        "duplicate-cycle",
        "stale-lock",
        "corrupt-ledger",
        "quality-blocked",
    }:
        return _phase_campaign_rehearsal_payload(
            as_of_date=as_of_date,
            scenario=scenario,
            generated_at=generated,
            fixture_root=fixture_root,
            evidence_root=evidence_root,
        )
    if scenario in BLOCKED_REHEARSAL_REASONS:
        return _blocked_rehearsal_payload(
            as_of_date=as_of_date,
            scenario=scenario,
            reason=BLOCKED_REHEARSAL_REASONS[scenario],
            generated_at=generated,
            fixture_root=fixture_root,
            evidence_root=evidence_root,
        )
    _write_rehearsal_fixtures(fixture_root, as_of_date=as_of_date, scenario=scenario)
    artifacts: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    errors: list[str] = []

    statement_result = run_paper_statement_validate(
        statement=fixture_root / "statement.json",
        as_of_date=as_of_date,
        output_dir=evidence_root / "statements",
        generated_at=generated,
    )
    artifacts["statement_validate"] = _result_summary(statement_result.status, statement_result.output_path)
    if statement_result.status == "ERROR":
        errors.append("statement_validation_failed")
        status = "ERROR"
        return _rehearsal_payload(
            as_of_date=as_of_date,
            scenario=scenario,
            generated_at=generated,
            status=status,
            artifacts=artifacts,
            warnings=warnings,
            errors=errors,
            fixture_root=fixture_root,
            evidence_root=evidence_root,
        )

    if scenario == "missing-performance":
        warnings.append("performance_skipped")
    else:
        performance_result = run_paper_performance_report(
            sessions_root=fixture_root / "sessions",
            broker_statement=statement_result.output_path,
            backtest_report=fixture_root / "backtest.json",
            min_stable_sessions=1,
            min_stable_fills=1,
            output=evidence_root / "performance" / as_of_date / "performance.json",
            markdown_output=evidence_root / "performance" / as_of_date / "performance.md",
            generated_at=generated,
        )
        artifacts["performance"] = _result_summary(performance_result.status, performance_result.output_path)

    ops_result = run_paper_ops_check(
        as_of_date=as_of_date,
        readiness_root=fixture_root / "readiness",
        sessions_root=fixture_root / "sessions",
        monitor_root=fixture_root / "monitor",
        campaign_root=fixture_root / "campaign",
        decisions_root=fixture_root / "decisions",
        performance_root=evidence_root / "performance",
        ledger_inputs=(),
        output_dir=evidence_root / "ops",
        generated_at=generated,
    )
    artifacts["ops_check"] = _result_summary(ops_result.status, ops_result.output_path)
    weekly_result = run_paper_weekly_summary(
        decisions_root=fixture_root / "decisions",
        performance_root=evidence_root / "performance",
        campaign_root=fixture_root / "campaign",
        ledger_inputs=(),
        output_dir=evidence_root / "weekly",
        as_of_date=as_of_date,
        history_weeks=1,
        generated_at=generated,
    )
    artifacts["weekly_summary"] = _result_summary(weekly_result.status, weekly_result.output_path)
    review_result = run_model_review_decision(
        challenger_report=fixture_root / "challenger_report.json",
        decision="DEFER",
        reviewer="offline-rehearsal",
        reason="deterministic offline paper operations rehearsal",
        output_dir=evidence_root / "model_challenger_decisions",
        generated_at=generated,
    )
    artifacts["model_review_decision"] = _result_summary(review_result.status, review_result.output_path)
    status = _overall_status([ops_result.status, weekly_result.status], warnings=warnings, errors=errors)
    return _rehearsal_payload(
        as_of_date=as_of_date,
        scenario=scenario,
        generated_at=generated,
        status=status,
        artifacts=artifacts,
        warnings=warnings,
        errors=errors,
        fixture_root=fixture_root,
        evidence_root=evidence_root,
    )


def render_paper_ops_rehearsal_markdown(report: Mapping[str, object]) -> str:
    artifacts = _mapping(report.get("artifacts"))
    lines = [
        "# Paper Ops Rehearsal",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Scenario: `{report.get('scenario') or ''}`",
        f"As of date: `{report.get('as_of_date') or ''}`",
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
    lines.extend(["", "Live trading allowed: `False`", "Credentials read: `False`", ""])
    return "\n".join(lines)


def _write_rehearsal_fixtures(root: Path, *, as_of_date: str, scenario: str) -> None:
    decision = "STOP" if scenario == "stop" else "CONTINUE"
    write_json_artifact(
        {"status": "READY", "ready_for_paper_daily": True, "as_of_date": as_of_date, "reasons": []},
        root / "readiness" / as_of_date / "readiness.json",
    )
    write_json_artifact(
        {
            "status": "OK",
            "monitor_summary": {
                "as_of_date": as_of_date,
                "critical_count": 0,
                "warning_count": 0,
                "pending_closeout_count": 0,
                "unmatched_closeout_count": 0,
            },
            "alerts": [],
        },
        root / "monitor" / as_of_date / "monitor.json",
    )
    write_json_artifact(
        {"status": "OK", "as_of_date": as_of_date, "blockers": []}, root / "campaign" / as_of_date / "campaign.json"
    )
    write_json_artifact(
        {
            "status": "OK",
            "decision": decision,
            "as_of_date": as_of_date,
            "blockers": [] if decision == "CONTINUE" else [{"severity": "CRITICAL", "code": "manual_stop"}],
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
        root / "decisions" / as_of_date / "decision.json",
    )
    expected_order = {
        "symbol": "SPY",
        "side": "buy",
        "client_order_id": f"signal-spy-{as_of_date.replace('-', '')}",
        "notional": 1.0,
    }
    write_json_artifact(
        {"ready_for_paper_review": True, "as_of_date": as_of_date}, root / "sessions" / as_of_date / "session.json"
    )
    write_json_artifact(
        {
            "status": "CLOSED",
            "session": {"as_of_date": as_of_date},
            "expected_order": expected_order,
            "broker_order": {
                "client_order_id": expected_order["client_order_id"],
                "symbol": "SPY",
                "side": "buy",
                "status": "filled",
                "filled_quantity": 0.002,
                "filled_avg_price": 500.0,
                "filled_at": f"{as_of_date}T00:03:00+00:00",
            },
        },
        root / "sessions" / as_of_date / "closeout" / "paper_closeout.json",
    )
    statement_fill: dict[str, object] = {
        "client_order_id": expected_order["client_order_id"],
        "symbol": "SPY",
        "side": "buy",
        "quantity": 0.002,
        "filled_avg_price": 500.0,
        "filled_at": f"{as_of_date}T00:03:00+00:00",
        "realized_pnl": 0.03,
    }
    if scenario == "invalid-statement":
        statement_fill.pop("filled_avg_price")
    write_json_artifact({"fills": [statement_fill]}, root / "statement.json")
    write_json_artifact(
        {"metrics": {"trade_count": 1, "turnover": 0.01, "estimated_costs": 0.0, "sharpe": 0.1, "max_drawdown": -0.01}},
        root / "backtest.json",
    )
    write_json_artifact(
        {
            "schema_version": "1.0",
            "generated_at": f"{as_of_date}T12:00:00+00:00",
            "status": "REVIEWABLE",
            "authority": {"mutates_latest_model": False, "automatic_champion_replacement": False},
            "safety": {"live_trading_authorized": False, "live_trading_allowed": False},
        },
        root / "challenger_report.json",
    )


def _blocked_rehearsal_payload(
    *,
    as_of_date: str,
    scenario: str,
    reason: str,
    generated_at: str,
    fixture_root: Path,
    evidence_root: Path,
) -> dict[str, object]:
    fixture_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    cycle_root = evidence_root / "paper_auto_cycle"
    cycle_path = cycle_root / as_of_date / "cycle.json"
    ledger_path = cycle_root / "session_ledger.jsonl"
    operator_path = evidence_root / "operator_status" / as_of_date / "operator_status.json"
    campaign_path = evidence_root / "campaign" / "campaign.json"
    performance_path = evidence_root / "performance" / "performance.json"
    context_path = evidence_root / "llm_context_pack" / as_of_date / "context_pack.json"
    monitor_path = evidence_root / "monitor" / "monitor.json"

    monitor_counts = {
        "orders": 1 if scenario == "open-order" else 0,
        "positions": 1 if scenario == "existing-position" else 0,
    }
    _write_simple_json(
        monitor_path,
        {
            "status": "CRITICAL",
            "broker_snapshot": {"status": "OK", "counts": monitor_counts},
            "alerts": [{"severity": "CRITICAL", "code": reason, "message": reason}],
            "safety": _safe_flags(),
        },
    )
    _write_simple_json(
        performance_path,
        {
            "status": "WARN",
            "paper_metrics": {
                "fills": 1,
                "pending_closeouts": 1 if reason == "closeout_pending" else 0,
                "unmatched_closeouts": 0,
            },
            "statement_status": {
                "status": "DIFFERENCES" if reason == "statement_mismatch" else "MATCHED",
                "statement_present": True,
                "unreconciled_fills": 1 if reason == "fills_unreconciled" else 0,
            },
            "blockers": [reason] if reason in {"statement_mismatch", "fills_unreconciled"} else [],
            "safety": _safe_flags(),
        },
    )
    _write_simple_json(
        campaign_path,
        {
            "status": "CRITICAL",
            "as_of_date": as_of_date,
            "paper_auto_campaign": {
                "state": "BLOCKED",
                "clean_sessions": 0,
                "blocker_histogram": {reason: 1},
                "next_action": "resolve_blockers",
            },
            "safety": _safe_flags(),
        },
    )
    _write_simple_json(
        operator_path,
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": "CRITICAL",
            "clean_for_paper_auto": False,
            "next_safe_action": "resolve_operator_blockers",
            "blockers": [{"severity": "CRITICAL", "code": reason, "message": reason}],
            "sources": {
                "monitor": str(monitor_path),
                "performance": str(performance_path),
                "campaign": str(campaign_path),
            },
            "safety": _safe_flags(),
        },
    )
    if scenario == "malicious-llm-context":
        _write_simple_json(
            context_path,
            {
                "status": "BLOCKED",
                "as_of_date": as_of_date,
                "blockers": [{"severity": "CRITICAL", "code": reason, "message": "context attempted to submit order"}],
                "guardrail_results": {"llm_authority": "none", "orders_blocked": True, "secret_access_blocked": True},
                "authority": {"llm_authority": "none", "orders_submitted": False, "risk_changed": False},
                "safety": _safe_flags(),
            },
        )

    cycle_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "state": "BLOCKED",
        "exit_code": 1,
        "confirmations": {"confirm_paper_auto": True},
        "reasons": [reason],
        "artifacts": {
            "operator_status": str(operator_path),
            "campaign_report": str(campaign_path),
            "monitor": str(monitor_path),
            "performance": str(performance_path),
            "llm_context_pack": str(context_path) if context_path.exists() else None,
            "cycle_json": str(cycle_path),
        },
        "authority": {"llm_authority": "none", "orders_submitted_by_wrapper": False, "risk_changed": False},
        "safety": _safe_flags(),
    }
    _write_simple_json(cycle_path, cycle_payload)
    _append_rehearsal_ledger(
        ledger_path, as_of_date=as_of_date, generated_at=generated_at, reason=reason, cycle_path=cycle_path
    )
    artifacts = {
        "paper_auto_cycle": _result_summary("BLOCKED", cycle_path),
        "session_ledger": _result_summary("BLOCKED", ledger_path),
        "operator_status": _result_summary("CRITICAL", operator_path),
        "campaign_report": _result_summary("CRITICAL", campaign_path),
        "performance": _result_summary("WARN", performance_path),
        "monitor": _result_summary("CRITICAL", monitor_path),
    }
    if context_path.exists():
        artifacts["llm_context_pack"] = _result_summary("BLOCKED", context_path)
    return _rehearsal_payload(
        as_of_date=as_of_date,
        scenario=scenario,
        generated_at=generated_at,
        status="CRITICAL",
        artifacts=artifacts,
        warnings=[],
        errors=[],
        fixture_root=fixture_root,
        evidence_root=evidence_root,
    )


def _append_rehearsal_ledger(path: Path, *, as_of_date: str, generated_at: str, reason: str, cycle_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_type": "paper_auto_cycle_session",
        "session_id": f"paper-auto-{as_of_date}-{reason}",
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "state": "BLOCKED",
        "exit_code": 1,
        "confirm_paper_auto": True,
        "order_state": "not_sent",
        "closeout_status": "NOT_APPLICABLE",
        "statement_status": "NOT_REQUESTED",
        "unreconciled_fills": 0,
        "blockers": [reason],
        "artifacts": {"cycle_json": str(cycle_path)},
        "safety": _safe_flags(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _phase_campaign_rehearsal_payload(
    *,
    as_of_date: str,
    scenario: str,
    generated_at: str,
    fixture_root: Path,
    evidence_root: Path,
) -> dict[str, object]:
    fixture_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    stable_sessions = 60 if scenario != "59-stable-sessions" else 59
    phase_status = "READY_FOR_REVIEW" if stable_sessions >= 60 else "ACCUMULATING"
    reason_by_scenario = {
        "duplicate-cycle": "duplicate_confirmed_cycle",
        "stale-lock": "cycle_lock_stale",
        "corrupt-ledger": "session_ledger_invalid_json",
        "quality-blocked": "llm_baseline_disagreement",
    }
    reason = reason_by_scenario.get(scenario)
    phase_input_root = evidence_root / "phase_inputs"
    campaign_path = phase_input_root / "campaign.json"
    performance_path = phase_input_root / "performance.json"
    operator_path = phase_input_root / "operator_status.json"
    quality_path = phase_input_root / "strategy_quality.json"
    evidence_path = phase_input_root / "evidence_index.json"
    weekly_path = phase_input_root / "weekly_summary.json"
    ledger_path = evidence_root / "paper_auto_cycle" / "session_ledger.jsonl"
    cycle_path = evidence_root / "paper_auto_cycle" / as_of_date / "cycle.json"
    lock_path = evidence_root / "paper_auto_cycle" / "locks" / f"paper_auto_cycle_{as_of_date}.lock"

    campaign_status = "OK"
    campaign_blockers: list[dict[str, object]] = []
    stability_blockers: dict[str, int] = {}
    if scenario in {"duplicate-cycle", "corrupt-ledger"} and reason is not None:
        campaign_status = "ERROR" if scenario == "corrupt-ledger" else "CRITICAL"
        campaign_blockers.append(
            {"severity": "ERROR" if scenario == "corrupt-ledger" else "CRITICAL", "code": reason, "message": reason}
        )
        stability_blockers[reason] = 1
    _write_simple_json(
        campaign_path,
        {
            "status": campaign_status,
            "as_of_date": as_of_date,
            "stability_campaign": {
                "state": "BLOCKED" if stability_blockers else phase_status,
                "target_clean_sessions": 60,
                "clean_sessions": stable_sessions,
                "remaining_clean_sessions": max(60 - stable_sessions, 0),
                "broker_confirmed_sessions": stable_sessions,
                "blocker_histogram": stability_blockers,
                "critical_blockers": sorted(stability_blockers.keys()),
                "next_action": "resolve_blockers"
                if stability_blockers
                else "review_next_phase"
                if stable_sessions >= 60
                else "continue_paper_auto_campaign",
            },
            "paper_auto_campaign": {
                "state": "READY_FOR_REVIEW",
                "target_clean_sessions": 20,
                "clean_sessions": 20,
                "remaining_clean_sessions": 0,
                "broker_confirmed_sessions": 20,
                "blocker_histogram": {},
                "next_action": "review_next_phase",
            },
            "blockers": campaign_blockers,
            "safety": _safe_flags(),
        },
    )
    _write_simple_json(
        performance_path,
        {
            "status": "OK",
            "paper_metrics": {"fills": stable_sessions, "pending_closeouts": 0, "unmatched_closeouts": 0},
            "statement_status": {"status": "MATCHED", "unreconciled_fills": 0},
            "statement_reconciliation": {"status": "MATCHED", "missing_fills": 0},
            "blockers": [],
            "safety": _safe_flags(),
        },
    )
    operator_blockers = []
    operator_status_value = "OK"
    if scenario == "stale-lock" and reason is not None:
        operator_status_value = "CRITICAL"
        operator_blockers.append({"severity": "CRITICAL", "code": reason, "message": reason})
        _write_simple_json(lock_path, {"status": "STALE", "as_of_date": as_of_date, "safety": _safe_flags()})
    _write_simple_json(
        operator_path,
        {
            "status": operator_status_value,
            "as_of_date": as_of_date,
            "clean_for_paper_auto": operator_status_value == "OK",
            "lock_status": "STALE" if scenario == "stale-lock" else "CLEAR",
            "blockers": operator_blockers,
            "safety": _safe_flags(),
        },
    )
    quality_status = "BLOCKED" if scenario == "quality-blocked" else "PASS"
    _write_simple_json(
        quality_path,
        {
            "status": "CRITICAL" if quality_status == "BLOCKED" else "OK",
            "quality_status": quality_status,
            "blockers": [reason] if quality_status == "BLOCKED" and reason is not None else [],
            "safety": _safe_flags(),
        },
    )
    evidence_issues = []
    if scenario == "corrupt-ledger" and reason is not None:
        evidence_issues.append({"severity": "ERROR", "code": reason, "message": reason})
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text("{not-json\n", encoding="utf-8")
    _write_simple_json(
        evidence_path,
        {
            "status": "ERROR" if evidence_issues else "OK",
            "issues": evidence_issues,
            "artifacts": {},
            "safety": _safe_flags(),
        },
    )
    _write_simple_json(weekly_path, {"status": "OK", "blockers": [], "safety": _safe_flags()})
    if scenario == "duplicate-cycle":
        _write_simple_json(
            cycle_path,
            {
                "status": "OK",
                "state": "PAPER_CLOSED",
                "as_of_date": as_of_date,
                "confirmations": {"confirm_paper_auto": True},
                "safety": _safe_flags(),
            },
        )
        _append_rehearsal_ledger(
            ledger_path,
            as_of_date=as_of_date,
            generated_at=generated_at,
            reason="duplicate_confirmed_cycle",
            cycle_path=cycle_path,
        )

    phase_result = run_paper_phase_review_report(
        as_of_date=as_of_date,
        campaign_report=campaign_path,
        performance_report=performance_path,
        operator_status=operator_path,
        strategy_quality=quality_path,
        evidence_index=evidence_path,
        weekly_summary=weekly_path,
        output_dir=evidence_root / "phase_review",
        generated_at=generated_at,
    )
    artifacts = {
        "campaign_report": _result_summary(campaign_status, campaign_path),
        "performance": _result_summary("OK", performance_path),
        "operator_status": _result_summary(operator_status_value, operator_path),
        "strategy_quality": _result_summary("CRITICAL" if quality_status == "BLOCKED" else "OK", quality_path),
        "evidence_index": _result_summary("ERROR" if evidence_issues else "OK", evidence_path),
        "weekly_summary": _result_summary("OK", weekly_path),
        "phase_review": _result_summary(phase_result.status, phase_result.output_path),
    }
    if ledger_path.exists():
        artifacts["session_ledger"] = _result_summary(
            "ERROR" if scenario == "corrupt-ledger" else "RECORDED", ledger_path
        )
    if cycle_path.exists():
        artifacts["paper_auto_cycle"] = _result_summary("PAPER_CLOSED", cycle_path)
    if lock_path.exists():
        artifacts["lock"] = _result_summary("STALE", lock_path)
    return _rehearsal_payload(
        as_of_date=as_of_date,
        scenario=scenario,
        generated_at=generated_at,
        status=phase_result.status,
        artifacts=artifacts,
        warnings=[],
        errors=["session_ledger_invalid_json"] if scenario == "corrupt-ledger" else [],
        fixture_root=fixture_root,
        evidence_root=evidence_root,
    )


def _write_simple_json(path: Path, payload: Mapping[str, object]) -> None:
    write_json_artifact(dict(payload), path)


def _adaptive_training_rehearsal_payload(
    *,
    as_of_date: str,
    scenario: str,
    generated_at: str,
    fixture_root: Path,
    evidence_root: Path,
) -> dict[str, object]:
    mapping = {
        "phase-not-ready": ("CRITICAL", "BLOCKED"),
        "retrain-due": ("OK", "CANDIDATE_REVIEWABLE"),
        "not-due": ("OK", "NOT_DUE"),
        "duplicate-retrain": ("OK", "NOT_DUE"),
        "candidate-rejected": ("WARN", "CANDIDATE_REJECTED"),
        "drift-blocked": ("CRITICAL", "BLOCKED"),
        "shadow-insufficient": ("WARN", "ACCUMULATING"),
        "shadow-ready": ("OK", "READY_FOR_SHADOW"),
        "alias-approved": ("OK", "ACTIVE_PAPER_ALIAS"),
        "alias-blocked": ("CRITICAL", "BLOCKED"),
        "alias-expired": ("CRITICAL", "BLOCKED"),
        "challenger-underperforms": ("WARN", "REJECTED"),
        "malicious-alias-llm": ("CRITICAL", "BLOCKED"),
        "alias-invalid-model": ("CRITICAL", "BLOCKED"),
        "malicious-adaptive-llm": ("CRITICAL", "BLOCKED"),
    }
    status, state = mapping[scenario]
    blockers = []
    if scenario == "phase-not-ready":
        blockers.append(
            {"severity": "CRITICAL", "code": "phase_review_not_ready", "message": "phase review is not ready"}
        )
    elif scenario == "drift-blocked":
        blockers.append({"severity": "CRITICAL", "code": "drift_critical", "message": "drift blocks challenger review"})
    elif scenario == "malicious-adaptive-llm":
        blockers.append(
            {
                "severity": "CRITICAL",
                "code": "model_promotion_instruction",
                "message": "adaptive LLM context attempted model promotion",
            }
        )
    elif scenario == "malicious-alias-llm":
        blockers.append(
            {
                "severity": "CRITICAL",
                "code": "alias_activation_instruction",
                "message": "LLM attempted alias activation without authority",
            }
        )
    elif scenario == "alias-invalid-model":
        blockers.append(
            {
                "severity": "CRITICAL",
                "code": "alias_invalid_model",
                "message": "paper alias points to an invalid model artifact",
            }
        )
    elif scenario == "alias-blocked":
        blockers.append(
            {"severity": "CRITICAL", "code": "alias_not_approved", "message": "paper alias approval gate is blocked"}
        )
    elif scenario == "alias-expired":
        blockers.append({"severity": "CRITICAL", "code": "alias_expired", "message": "paper alias TTL is expired"})
    artifact_path = evidence_root / "adaptive_training" / "training_cycle.json"
    _write_simple_json(
        artifact_path,
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "training_state": state if state != "READY_FOR_SHADOW" else "CANDIDATE_REVIEWABLE",
            "review_only": True,
            "model_mutated": False,
            "live_trading_authorized": False,
            "blockers": blockers,
            "safety": _safe_flags(),
        },
    )
    artifacts = {"adaptive_training_cycle": _result_summary(status, artifact_path)}
    if scenario in {
        "shadow-insufficient",
        "shadow-ready",
        "alias-approved",
        "alias-blocked",
        "alias-expired",
        "challenger-underperforms",
        "malicious-alias-llm",
        "alias-invalid-model",
    }:
        scorecard_state = {
            "shadow-insufficient": "ACCUMULATING",
            "challenger-underperforms": "REJECTED",
        }.get(
            scenario,
            "READY_FOR_PAPER_ALIAS"
            if scenario == "alias-approved"
            else "BLOCKED"
            if scenario != "shadow-ready"
            else "READY_FOR_PAPER_ALIAS",
        )
        scorecard_path = evidence_root / "shadow" / "shadow_scorecard.json"
        _write_simple_json(
            scorecard_path,
            {
                "scorecard_state": scorecard_state,
                "metrics": {"trade_count": 20},
                "blockers": blockers,
                "safety": _safe_flags(),
            },
        )
        artifacts["shadow_scorecard"] = _result_summary(scorecard_state, scorecard_path)
    if scenario in {
        "shadow-ready",
        "shadow-insufficient",
        "alias-approved",
        "alias-blocked",
        "alias-expired",
        "challenger-underperforms",
    }:
        shadow_path = evidence_root / "shadow" / "shadow_plan.json"
        _write_simple_json(
            shadow_path,
            {
                "shadow_state": "READY_FOR_SHADOW",
                "challenger": {"shadow_only": True, "promotes_model": False},
                "safety": _safe_flags(),
            },
        )
        artifacts["shadow_plan"] = _result_summary("OK", shadow_path)
    if scenario in {"alias-approved", "alias-blocked", "alias-expired", "malicious-alias-llm", "alias-invalid-model"}:
        alias_path = evidence_root / "alias" / "current.json"
        alias_state = "ACTIVE_PAPER_ALIAS" if scenario == "alias-approved" else "BLOCKED"
        _write_simple_json(
            alias_path,
            {
                "alias_state": alias_state,
                "active_model_path": None
                if scenario == "alias-invalid-model"
                else str(evidence_root / "alias" / "paper_model.json"),
                "expires_on": "2026-01-01" if scenario == "alias-expired" else "2026-07-16",
                "latest_model": {"path": "models/latest_model.json", "mutated": False},
                "blockers": blockers,
                "safety": _safe_flags(),
            },
        )
        artifacts["paper_model_alias"] = _result_summary(alias_state, alias_path)
    return {
        **_rehearsal_payload(
            as_of_date=as_of_date,
            scenario=scenario,
            generated_at=generated_at,
            status=status,
            artifacts=artifacts,
            warnings=["candidate_rejected"] if scenario == "candidate-rejected" else [],
            errors=[],
            fixture_root=fixture_root,
            evidence_root=evidence_root,
        ),
        "adaptive_training": {
            "state": state,
            "model_mutated": False,
            "live_trading_authorized": False,
            "blockers": blockers,
        },
    }


def _safe_flags() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def _rehearsal_payload(
    *,
    as_of_date: str,
    scenario: str,
    generated_at: str,
    status: str,
    artifacts: Mapping[str, object],
    warnings: list[str],
    errors: list[str],
    fixture_root: Path,
    evidence_root: Path,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "scenario": scenario,
        "status": status,
        "fixture_root": str(fixture_root),
        "evidence_root": str(evidence_root),
        "artifacts": dict(artifacts),
        "warnings": warnings,
        "errors": errors,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def _result_summary(status: str, path: Path) -> dict[str, object]:
    return {"status": status, "path": str(path)}


def _overall_status(statuses: list[str], *, warnings: list[str], errors: list[str]) -> str:
    normalized = {status.upper() for status in statuses}
    if errors or "ERROR" in normalized:
        return "ERROR"
    if "CRITICAL" in normalized:
        return "CRITICAL"
    if warnings or "WARN" in normalized:
        return "WARN"
    return "OK"


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperOpsRehearsalOperationalError("paper ops rehearsal must be a JSON object")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
