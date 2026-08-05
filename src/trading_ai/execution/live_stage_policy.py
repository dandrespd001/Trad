"""Separate live-stage policy for post-canary scale review."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact

LIVE_CANARY_STAGE = "LIVE_CANARY"
LIVE_SCALE_UP_STAGE = "LIVE_SCALE_UP"
LIVE_STAGES = frozenset({LIVE_CANARY_STAGE, LIVE_SCALE_UP_STAGE})


@dataclass(frozen=True)
class LiveStageArtifacts:
    json_path: Path
    markdown_path: Path
    payload: dict[str, object]


def evaluate_live_stage_policy(
    *,
    target_stage: str,
    requested_notional_usd: float,
    canary_sessions: Sequence[Mapping[str, object]],
    clean_sessions_required: int,
    reviewer: str,
    reason: str,
    approval_reference: str,
    release_gate_passed: bool,
    secrets_rotated: bool,
    llm_drift_ok: bool,
    model_drift_ok: bool,
    max_slippage_bps: float,
    max_latency_ms: float = 1_000.0,
    max_drawdown_pct: float = 0.0,
) -> dict[str, object]:
    stage = str(target_stage).strip().upper()
    blockers: list[str] = []
    session_blockers: list[str] = []
    clean_count = 0
    normalized_sessions: list[dict[str, object]] = []
    for index, session in enumerate(canary_sessions):
        if not isinstance(session, Mapping):
            session_blockers.append(f"session_payload_invalid:{index}")
            normalized_sessions.append(_normalize_session({}))
        else:
            normalized_sessions.append(_normalize_session(session))

    requested_notional = _finite_float(requested_notional_usd)
    slippage_limit = _finite_float(max_slippage_bps)
    latency_limit = _finite_float(max_latency_ms)
    drawdown_limit = _finite_float(max_drawdown_pct)
    clean_required = (
        clean_sessions_required
        if isinstance(clean_sessions_required, int) and not isinstance(clean_sessions_required, bool)
        else 0
    )

    if stage not in LIVE_STAGES:
        blockers.append("invalid_live_stage")
    if requested_notional is None or requested_notional <= 0:
        blockers.append("requested_notional_usd_invalid")
    if stage == LIVE_CANARY_STAGE and not _is_usd_one(requested_notional):
        blockers.append("live_canary_notional_must_be_usd_1")
    if stage == LIVE_SCALE_UP_STAGE and (
        requested_notional is None or not 50.0 <= requested_notional <= 100.0
    ):
        blockers.append("live_scale_up_notional_out_of_range")
    if not _nonempty_text(reviewer) or not _nonempty_text(reason) or not _nonempty_text(approval_reference):
        blockers.append("human_approval_required")
    if clean_required <= 0:
        blockers.append("clean_sessions_required_invalid")
    if release_gate_passed is not True:
        blockers.append("release_gate_not_green")
    if secrets_rotated is not True:
        blockers.append("secrets_not_rotated")
    if llm_drift_ok is not True:
        blockers.append("llm_drift_review_failed")
    if model_drift_ok is not True:
        blockers.append("model_drift_review_failed")
    if slippage_limit is None or slippage_limit < 0:
        blockers.append("max_slippage_bps_invalid")
    if latency_limit is None or latency_limit <= 0:
        blockers.append("max_latency_ms_invalid")
    if drawdown_limit is None or not 0 <= drawdown_limit <= 1:
        blockers.append("max_drawdown_pct_invalid")

    seen_order_ids: set[str] = set()
    for index, session in enumerate(normalized_sessions):
        order_id = _nonempty_text(session.get("order_id"))
        if order_id is not None:
            if order_id in seen_order_ids:
                session_blockers.append(f"duplicate_order_id:{index}")
            seen_order_ids.add(order_id)
        blockers_for_session = _session_blockers(
            session,
            index=index,
            max_slippage_bps=slippage_limit,
            max_latency_ms=latency_limit,
            max_drawdown_pct=drawdown_limit,
        )
        if blockers_for_session or f"duplicate_order_id:{index}" in session_blockers:
            session_blockers.extend(blockers_for_session)
        else:
            clean_count += 1
    if clean_count < clean_required:
        blockers.append("clean_live_sessions_below_threshold")

    all_blockers = _dedupe([*blockers, *session_blockers])
    scorecard = _build_scorecard(normalized_sessions, clean_sessions=clean_count)
    return {
        "schema_version": "1.0",
        "status": "APPROVED_FOR_LIVE_SCALE_REVIEW" if not all_blockers else "BLOCKED",
        "target_stage": stage,
        "requested_notional_usd": requested_notional,
        "recommended_notional_range_usd": [50.0, 100.0] if stage == LIVE_SCALE_UP_STAGE and not all_blockers else [],
        "clean_sessions_required": clean_required,
        "reviewer": _nonempty_text(reviewer),
        "reason": _nonempty_text(reason),
        "approval_reference": _nonempty_text(approval_reference),
        "release_gate_passed": release_gate_passed is True,
        "secrets_rotated": secrets_rotated is True,
        "llm_drift_ok": llm_drift_ok is True,
        "model_drift_ok": model_drift_ok is True,
        "scorecard": scorecard,
        "blockers": all_blockers,
        "safety": {
            "recommendation_only": True,
            "orders_submitted": False,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
        },
    }


def write_live_stage_scorecard(
    *,
    output_dir: str | Path = "reports/tmp/live_stage_policy",
    as_of_date: str,
    **kwargs: object,
) -> LiveStageArtifacts:
    payload = evaluate_live_stage_policy(**kwargs)
    output_root = Path(output_dir) / as_of_date
    json_path = output_root / "live_stage_scorecard.json"
    markdown_path = output_root / "live_stage_scorecard.md"
    write_json_artifact(payload, json_path)
    write_text_artifact(render_live_stage_scorecard(payload), markdown_path)
    return LiveStageArtifacts(json_path=json_path, markdown_path=markdown_path, payload=payload)


def render_live_stage_scorecard(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_lines = [f"- `{item}`" for item in blockers] if isinstance(blockers, list) and blockers else ["- none"]
    scorecard = _mapping(payload.get("scorecard"))
    return "\n".join(
        [
            "# Live Stage Scorecard",
            "",
            f"Status: **{payload.get('status')}**",
            f"Target stage: `{payload.get('target_stage')}`",
            f"Requested notional USD: `{payload.get('requested_notional_usd')}`",
            f"Recommended range USD: `{payload.get('recommended_notional_range_usd')}`",
            f"Reviewer: `{payload.get('reviewer')}`",
            "",
            "## Live Evidence",
            "",
            f"- Clean sessions: `{scorecard.get('clean_sessions')}`",
            f"- Max slippage bps: `{scorecard.get('max_slippage_bps')}`",
            f"- Max latency ms: `{scorecard.get('max_latency_ms')}`",
            f"- Breaker trips: `{scorecard.get('breaker_trips')}`",
            f"- Rollbacks: `{scorecard.get('rollbacks')}`",
            f"- Net edge bps: `{scorecard.get('net_edge_bps')}`",
            "",
            "## Blockers",
            "",
            *blocker_lines,
            "",
        ]
    )


def _session_blockers(
    session: Mapping[str, object],
    *,
    index: int,
    max_slippage_bps: float | None,
    max_latency_ms: float | None,
    max_drawdown_pct: float | None,
) -> list[str]:
    blockers: list[str] = []
    if session.get("status") != "SUBMITTED":
        blockers.append(f"session_not_submitted:{index}")
    if _nonempty_text(session.get("order_id")) is None:
        blockers.append(f"session_order_id_missing:{index}")
    if session.get("orders_submitted") is not True:
        blockers.append(f"session_order_missing:{index}")
    if not _is_usd_one(_finite_float(session.get("notional_usd"))):
        blockers.append(f"session_notional_not_usd_1:{index}")
    if str(session.get("fill_status", "")).lower() != "filled":
        blockers.append(f"session_fill_not_confirmed:{index}")
    if session.get("rollback_triggered") is not False:
        blockers.append(f"session_rollback_triggered:{index}")
    if session.get("breaker_tripped") is not False:
        blockers.append(f"session_breaker_tripped:{index}")
    slippage = _finite_float(session.get("slippage_bps"))
    latency = _finite_float(session.get("latency_ms"))
    drawdown = _finite_float(session.get("drawdown_pct"))
    net_edge = _finite_float(session.get("net_edge_bps"))
    if slippage is None:
        blockers.append(f"session_slippage_bps_invalid:{index}")
    elif max_slippage_bps is not None and slippage > max_slippage_bps:
        blockers.append(f"session_slippage_bps_exceeded:{index}")
    if latency is None or latency < 0:
        blockers.append(f"session_latency_ms_invalid:{index}")
    elif max_latency_ms is not None and latency > max_latency_ms:
        blockers.append(f"session_latency_ms_exceeded:{index}")
    if drawdown is None or not 0 <= drawdown <= 1:
        blockers.append(f"session_drawdown_invalid:{index}")
    elif max_drawdown_pct is not None and drawdown > max_drawdown_pct:
        blockers.append(f"session_drawdown_exceeded:{index}")
    if net_edge is None or net_edge <= 0:
        blockers.append(f"session_net_edge_not_positive:{index}")
    return blockers


def _build_scorecard(sessions: Sequence[Mapping[str, object]], *, clean_sessions: int) -> dict[str, object]:
    slippages = _finite_values(session.get("slippage_bps") for session in sessions)
    latencies = _finite_values(session.get("latency_ms") for session in sessions)
    drawdowns = _finite_values(session.get("drawdown_pct") for session in sessions)
    net_edges = _finite_values(session.get("net_edge_bps") for session in sessions)
    return {
        "sessions": len(sessions),
        "clean_sessions": clean_sessions,
        "max_slippage_bps": max(slippages) if slippages else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "breaker_trips": sum(1 for session in sessions if session.get("breaker_tripped") is True),
        "rollbacks": sum(1 for session in sessions if session.get("rollback_triggered") is True),
        "alerts": sorted({str(session.get("alert_tier")) for session in sessions if session.get("alert_tier")}),
        "max_drawdown_pct": max(drawdowns, default=None),
        "net_edge_bps": min(net_edges) if net_edges else None,
        "order_ids": [str(session.get("order_id")) for session in sessions if session.get("order_id")],
    }


def _normalize_session(session: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": str(session.get("status", "")).strip().upper(),
        "order_id": _nonempty_text(session.get("order_id")),
        "notional_usd": _finite_float(session.get("notional_usd")),
        "orders_submitted": _optional_bool(session.get("orders_submitted")),
        "fill_status": str(session.get("fill_status", "")).strip().lower(),
        "rollback_triggered": _optional_bool(session.get("rollback_triggered")),
        "breaker_tripped": _optional_bool(session.get("breaker_tripped")),
        "slippage_bps": _finite_float(session.get("slippage_bps")),
        "latency_ms": _finite_float(session.get("latency_ms")),
        "alert_tier": session.get("alert_tier"),
        "drawdown_pct": _finite_float(session.get("drawdown_pct")),
        "net_edge_bps": _finite_float(session.get("net_edge_bps")),
    }


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _is_usd_one(value: float | None) -> bool:
    return value is not None and abs(value - 1.0) < 0.000001


def _nonempty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _finite_values(values: Iterable[object]) -> list[float]:
    result: list[float] = []
    for value in values:
        numeric = _finite_float(value)
        if numeric is not None:
            result.append(numeric)
    return result


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
