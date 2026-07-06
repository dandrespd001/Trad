"""Deterministic live dry-run rehearsal scenarios."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.live_reconciliation import LiveOrderSnapshot, reconcile_live_positions
from trading_ai.execution.live_safe_flatten import run_live_safe_flatten
from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact


@dataclass(frozen=True)
class LiveRehearsalResult:
    exit_code: int
    status: str
    summary_path: Path
    markdown_path: Path
    evidence_index_path: Path
    payload: dict[str, object]


def run_live_rehearsal(
    *,
    fixtures: str | Path,
    output: str | Path,
    generated_at: str | None = None,
) -> LiveRehearsalResult:
    fixture_root = Path(fixtures)
    output_root = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    scenario_results: list[dict[str, object]] = []
    evidence_items: list[dict[str, object]] = []
    for fixture_path in sorted(fixture_root.glob("*.json")):
        scenario = _read_json(fixture_path)
        result = _run_scenario(scenario, output_root=output_root, generated_at=generated)
        result_path = output_root / "scenarios" / f"{result['name']}.json"
        write_json_artifact(result, result_path)
        output_hash = _sha256(result_path)
        result["output_hash"] = output_hash
        write_json_artifact(result, result_path)
        scenario_results.append(result)
        evidence_items.append(
            {
                "name": result["name"],
                "input_path": str(fixture_path),
                "input_hash": result["input_hash"],
                "output_path": str(result_path),
                "output_hash": output_hash,
                "gate": result["observed_gate"],
            }
        )

    status = "PASSED" if scenario_results and all(item.get("passed") is True for item in scenario_results) else "FAILED"
    payload = {
        "schema_version": "1.0",
        "generated_at": generated,
        "status": status,
        "scenario_count": len(scenario_results),
        "scenarios": scenario_results,
        "safety": {
            "dry_run_only": True,
            "fake_broker_only": True,
            "orders_submitted": False,
            "credentials_read": False,
            "live_trading_authorized": False,
        },
    }
    summary_path = output_root / "summary.json"
    markdown_path = output_root / "summary.md"
    evidence_index_path = output_root / "evidence_index.json"
    write_json_artifact(payload, summary_path)
    write_text_artifact(_render_markdown(payload), markdown_path)
    write_json_artifact(
        {
            "schema_version": "1.0",
            "generated_at": generated,
            "status": status,
            "items": evidence_items,
        },
        evidence_index_path,
    )
    return LiveRehearsalResult(
        exit_code=0 if status == "PASSED" else 1,
        status=status,
        summary_path=summary_path,
        markdown_path=markdown_path,
        evidence_index_path=evidence_index_path,
        payload=payload,
    )


def _run_scenario(
    scenario: Mapping[str, object],
    *,
    output_root: Path,
    generated_at: str,
) -> dict[str, object]:
    name = str(scenario.get("name") or "scenario")
    expected_gate = scenario.get("expected_gate")
    expected_blocker = scenario.get("expected_blocker")
    observed_gate = "live_execute_session"
    observed_blocker: str | None = None
    status = "DRY_RUN_READY"

    if not str(scenario.get("reviewer") or "").strip() or not str(scenario.get("reason") or "").strip():
        observed_gate = "human_confirmation"
        observed_blocker = "human_review_required"
        status = "BLOCKED"
    elif scenario.get("readiness_state") != "READY_FOR_LIVE_CANARY":
        observed_gate = "readiness"
        observed_blocker = "readiness_not_ready"
        status = "BLOCKED"
    elif scenario.get("rollback_required") is True:
        observed_gate = "rollback"
        observed_blocker = None
        status = "DRY_RUN_READY"
        run_live_safe_flatten(
            as_of_date="2026-06-16",
            broker=_FakeRollbackBroker(),
            allowlist=("SPY",),
            reviewer=str(scenario.get("reviewer")),
            reason=str(scenario.get("reason")),
            output_dir=output_root / "rollback",
            generated_at=generated_at,
        )
    elif scenario.get("breaker_state") == "TRIPPED":
        observed_gate = "breaker"
        observed_blocker = "breaker_tripped:manual_trip"
        status = "BLOCKED"
    elif scenario.get("market_open") is False:
        observed_gate = "market_calendar"
        observed_blocker = "market_closed"
        status = "BLOCKED"
    elif scenario.get("price_sanity_ok") is False:
        observed_gate = "price_sanity"
        observed_blocker = "price_sanity_failed"
        status = "BLOCKED"
    elif scenario.get("fill_timeout") is True:
        observed_gate = "reconciliation"
        report = reconcile_live_positions(
            expected_positions=[],
            broker_positions=[],
            open_orders=[LiveOrderSnapshot(symbol="SPY", client_order_id="fill-timeout", status="new", age_seconds=301)],
            allowlist=("SPY",),
            fill_timeout_seconds=300,
        )
        observed_blocker = str(report.divergences[-1]["code"]) if report.divergences else None
        status = "BLOCKED"

    passed = expected_gate == observed_gate and expected_blocker == observed_blocker
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "name": name,
        "status": status,
        "passed": passed,
        "expected_gate": expected_gate,
        "observed_gate": observed_gate,
        "expected_blocker": expected_blocker,
        "observed_blocker": observed_blocker,
        "input_hash": _stable_hash(scenario),
        "output_hash": None,
        "safety": {
            "dry_run_only": True,
            "fake_broker_only": True,
            "orders_submitted": False,
            "credentials_read": False,
            "live_trading_authorized": False,
        },
    }


class _FakeRollbackBroker:
    def read_positions(self):
        from trading_ai.execution.live_reconciliation import LivePosition

        return [LivePosition(symbol="SPY", quantity=1.0)]


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _stable_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_markdown(payload: Mapping[str, object]) -> str:
    lines = [
        "# Live Rehearsal",
        "",
        f"Status: **{payload.get('status')}**",
        f"Scenarios: `{payload.get('scenario_count')}`",
        "",
        "| Scenario | Gate | Blocker | Passed |",
        "| --- | --- | --- | --- |",
    ]
    scenarios = payload.get("scenarios")
    if isinstance(scenarios, list):
        for scenario in scenarios:
            if isinstance(scenario, Mapping):
                lines.append(
                    f"| `{scenario.get('name')}` | `{scenario.get('observed_gate')}` | "
                    f"`{scenario.get('observed_blocker')}` | `{scenario.get('passed')}` |"
                )
    lines.extend(["", "Orders submitted: `False`", ""])
    return "\n".join(lines)
