"""Score shadow challenger outcomes before allowing a paper-only alias."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact

DEFAULT_OUTPUT_DIR = "reports/tmp/paper_shadow_scorecard"
STATE_ACCUMULATING = "ACCUMULATING"
STATE_READY = "READY_FOR_PAPER_ALIAS"
STATE_REJECTED = "REJECTED"
STATE_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PaperShadowScorecardResult:
    exit_code: int
    scorecard_state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_shadow_scorecard(
    *,
    ledger_input: str | Path,
    phase_review: str | Path,
    paper_performance: str | Path,
    min_shadow_trades: int = 20,
    min_win_rate: float = 0.50,
    min_avg_forward_return_bps: float = 0.0,
    max_shadow_drawdown_pct: float = 10.0,
    max_missing_outcome_rate_pct: float = 5.0,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperShadowScorecardResult:
    output_path = Path(output_dir) / "shadow_scorecard.json"
    markdown_path = Path(output_dir) / "shadow_scorecard.md"
    blockers: list[str] = []
    try:
        phase = read_json_artifact(phase_review)
        performance = read_json_artifact(paper_performance)
        records = _read_ledger(Path(ledger_input))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        payload = _payload(
            generated_at, STATE_BLOCKED, [], {}, [str(exc)], ledger_input, phase_review, paper_performance
        )
        return _write(payload, output_path, markdown_path)

    if str(phase.get("phase_status") or "").upper() != "READY_FOR_REVIEW":
        blockers.append("phase_review_not_ready")
    if _performance_critical(performance):
        blockers.append("paper_performance_critical")
    metrics = _metrics(records)
    if blockers:
        state = STATE_BLOCKED
    elif metrics["missing_outcome_rate_pct"] > max_missing_outcome_rate_pct:
        state = STATE_BLOCKED
        blockers.append("missing_outcomes")
    elif metrics["trade_count"] < min_shadow_trades:
        state = STATE_ACCUMULATING
    elif (
        metrics["win_rate"] < min_win_rate
        or metrics["avg_forward_return_bps"] < min_avg_forward_return_bps
        or abs(metrics["max_drawdown_pct"]) > max_shadow_drawdown_pct
    ):
        state = STATE_REJECTED
    else:
        state = STATE_READY
    payload = _payload(generated_at, state, records, metrics, blockers, ledger_input, phase_review, paper_performance)
    return _write(payload, output_path, markdown_path)


def _read_ledger(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, Mapping):
            records.append(dict(item))
    return records


def _metrics(records: list[Mapping[str, object]]) -> dict[str, float]:
    outcomes = [_mapping(record.get("outcome")) for record in records if str(record.get("state") or "") == "RECORDED"]
    returns = [float(outcome.get("forward_return") or 0.0) for outcome in outcomes if outcome]
    wins = [return_value for return_value in returns if return_value > 0]
    no_shadow = [record for record in records if str(record.get("state") or "") == "NO_SHADOW_SIGNAL"]
    missing = [
        record
        for record in records
        if str(record.get("state") or "") == "BLOCKED" and _mapping(record.get("shadow_signal"))
    ]
    shadow_signal_count = len(returns) + len(missing)
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for return_value in returns:
        equity *= 1.0 + return_value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, (equity / peak - 1.0) * 100.0)
    return {
        "record_count": float(len(records)),
        "trade_count": float(len(returns)),
        "shadow_signal_count": float(shadow_signal_count),
        "no_shadow_signal_count": float(len(no_shadow)),
        "blocked_outcome_count": float(len(missing)),
        "win_rate": (len(wins) / len(returns)) if returns else 0.0,
        "avg_forward_return_bps": (sum(returns) / len(returns) * 10000.0) if returns else 0.0,
        "max_drawdown_pct": max_drawdown,
        "missing_outcome_rate_pct": (len(missing) / shadow_signal_count * 100.0) if shadow_signal_count else 0.0,
    }


def _performance_critical(payload: Mapping[str, object]) -> bool:
    if str(payload.get("status") or "").upper() in {"CRITICAL", "ERROR"}:
        return True
    metrics = _mapping(payload.get("paper_metrics"))
    return float(metrics.get("rejections") or 0.0) > 0.0


def _payload(generated_at, state, records, metrics, blockers, ledger_input, phase_review, paper_performance):
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "scorecard_state": state,
        "status": state,
        "metrics": metrics,
        "record_count": len(records),
        "blockers": list(blockers),
        "sources": {
            "ledger_input": str(Path(ledger_input)),
            "phase_review": str(Path(phase_review)),
            "paper_performance": str(Path(paper_performance)),
        },
        "safety": {"paper_only": True, "orders_submitted": False, "live_trading_authorized": False},
    }


def _write(payload: dict[str, object], output_path: Path, markdown_path: Path) -> PaperShadowScorecardResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(f"# Paper Shadow Scorecard\n\nState: **{payload.get('scorecard_state')}**\n", markdown_path)
    state = str(payload.get("scorecard_state") or STATE_BLOCKED)
    return PaperShadowScorecardResult(
        0 if state in {STATE_ACCUMULATING, STATE_READY, STATE_REJECTED} else 1,
        state,
        output_path,
        markdown_path,
        payload,
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
