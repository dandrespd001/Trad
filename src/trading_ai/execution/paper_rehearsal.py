"""Deterministic offline rehearsal for paper operating controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.evaluation.model_review_decision import run_model_review_decision
from trading_ai.execution.paper_common import (
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_ops_check import run_paper_ops_check
from trading_ai.execution.paper_performance import run_paper_performance_report
from trading_ai.execution.paper_statement import run_paper_statement_validate
from trading_ai.execution.paper_weekly_summary import run_paper_weekly_summary


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_rehearsal"
SCENARIOS = ("complete", "missing-performance", "stop", "invalid-statement")


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
    write_json_artifact({"status": "OK", "as_of_date": as_of_date, "blockers": []}, root / "campaign" / as_of_date / "campaign.json")
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
    write_json_artifact({"ready_for_paper_review": True, "as_of_date": as_of_date}, root / "sessions" / as_of_date / "session.json")
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
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
