"""Cronable paper-only auto cycle with governed LLM signal proposals."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.evaluation.paper_daily_prepare import PaperDailyPrepareResult, prepare_paper_daily
from trading_ai.execution.llm_signal_proposals import LLMSignalProposalsResult, run_llm_signal_proposals
from trading_ai.execution.paper_bot_cycle import PaperBotCycleResult, run_paper_bot_cycle
from trading_ai.execution.paper_common import (
    as_of_date_to_iso,
    read_json_artifact,
    reason_codes,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_review_decision import (
    DECISION_APPROVE_PAPER_CONFIRMATION,
    PaperReviewDecisionResult,
    run_paper_review_decision,
)
from trading_ai.execution.paper_safety import aggregate_safety
from trading_ai.execution.paper_signal_arbitration import PaperSignalArbitrationResult, run_paper_signal_arbitration

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_auto_cycle"
DEFAULT_MAX_LOCK_AGE_MINUTES = 90

STATE_EVIDENCE_ONLY = "EVIDENCE_ONLY"
STATE_NO_TRADE_REVIEW = "NO_TRADE_REVIEW"
STATE_PAPER_SUBMITTED = "PAPER_SUBMITTED"
STATE_PAPER_CLOSED = "PAPER_CLOSED"
STATE_BLOCKED = "BLOCKED"
STATE_ERROR = "ERROR"


class PaperAutoCycleOperationalError(RuntimeError):
    """Raised when the paper auto cycle cannot be written."""


@dataclass(frozen=True)
class PaperAutoCycleResult:
    exit_code: int
    state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_auto_cycle(
    *,
    as_of_date: str,
    source: str | Path | None = None,
    approved_dir: str | Path | None = None,
    dataset_id: str | None = "core_etfs",
    frequency: str | None = "1d",
    start: str,
    end: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    confirm_paper_auto: bool = False,
    provider: str = "manual_csv",
    license_note: str | None = None,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    signal_model: str | Path = "models/latest_model.json",
    paper_model_alias: str | Path | None = None,
    approved_output_dir: str | Path = "data/raw/approved",
    registry_dir: str | Path = "reports/registry",
    use_openai: bool = False,
    confirm_llm: bool = False,
    monitor: str | Path | None = None,
    performance: str | Path | None = None,
    operator_status: str | Path | None = None,
    campaign_report: str | Path | None = None,
    lock_dir: str | Path | None = None,
    session_ledger: str | Path | None = None,
    require_clean_state: bool = False,
    generated_at: str | None = None,
) -> PaperAutoCycleResult:
    raw_as_of_date = str(as_of_date).strip()
    if confirm_paper_auto:
        relative_date_reasons = _confirmed_auto_relative_date_reasons(
            as_of_date=raw_as_of_date,
            start=start,
            end=end,
        )
        if relative_date_reasons:
            generated = generated_at or _utc_now()
            output_root = Path(output_dir) / raw_as_of_date
            ledger_path = _session_ledger_path(session_ledger, output_dir=output_dir)
            return _write_cycle(
                output_root=output_root,
                as_of_date=raw_as_of_date,
                generated_at=generated,
                state=STATE_ERROR,
                exit_code=2,
                confirm_paper_auto=confirm_paper_auto,
                paths={"session_ledger": str(ledger_path)},
                steps=[],
                reasons=relative_date_reasons,
                session_ledger=ledger_path,
            )
    as_of_date = as_of_date_to_iso(as_of_date)
    generated = generated_at or _utc_now()
    output_root = Path(output_dir) / as_of_date
    ledger_path = _session_ledger_path(session_ledger, output_dir=output_dir)
    lock_path = _cycle_lock_path(lock_dir, as_of_date=as_of_date)
    lock_fd: int | None = None
    if lock_path is not None:
        try:
            lock_fd = _acquire_cycle_lock(lock_path, generated_at=generated)
        except FileExistsError:
            if _cycle_lock_is_stale(lock_path, max_age_minutes=DEFAULT_MAX_LOCK_AGE_MINUTES):
                _remove_stale_cycle_lock(lock_path)
                with suppress(FileExistsError):
                    lock_fd = _acquire_cycle_lock(lock_path, generated_at=generated)
            if lock_fd is None:
                return _write_cycle(
                    output_root=output_root,
                    as_of_date=as_of_date,
                    generated_at=generated,
                    state=STATE_BLOCKED,
                    exit_code=1,
                    confirm_paper_auto=confirm_paper_auto,
                    paths={"cycle_lock": str(lock_path)},
                    steps=[],
                    reasons=["cycle_lock_active"],
                    session_ledger=ledger_path,
                )
    try:
        if confirm_paper_auto and not require_clean_state:
            return _write_cycle(
                output_root=output_root,
                as_of_date=as_of_date,
                generated_at=generated,
                state=STATE_BLOCKED,
                exit_code=1,
                confirm_paper_auto=confirm_paper_auto,
                paths={"session_ledger": str(ledger_path)},
                steps=[],
                reasons=["require_clean_state_required"],
                session_ledger=ledger_path,
            )
        duplicate_reasons = _duplicate_confirmed_cycle_reasons(
            output_dir=output_dir,
            session_ledger=ledger_path,
            as_of_date=as_of_date,
            require_clean_state=require_clean_state,
            confirm_paper_auto=confirm_paper_auto,
        )
        if duplicate_reasons:
            return _write_cycle(
                output_root=output_root,
                as_of_date=as_of_date,
                generated_at=generated,
                state=STATE_BLOCKED,
                exit_code=1,
                confirm_paper_auto=confirm_paper_auto,
                paths={"session_ledger": str(ledger_path)},
                steps=[],
                reasons=duplicate_reasons,
                session_ledger=ledger_path,
            )
        return _run_paper_auto_cycle_steps(
            as_of_date=as_of_date,
            source=source,
            approved_dir=approved_dir,
            dataset_id=dataset_id,
            frequency=frequency,
            start=start,
            end=end,
            output_dir=output_dir,
            confirm_paper_auto=confirm_paper_auto,
            provider=provider,
            license_note=license_note,
            config=config,
            risk=risk,
            signal_model=signal_model,
            paper_model_alias=paper_model_alias,
            approved_output_dir=approved_output_dir,
            registry_dir=registry_dir,
            use_openai=use_openai,
            confirm_llm=confirm_llm,
            monitor=monitor,
            performance=performance,
            operator_status=operator_status,
            campaign_report=campaign_report,
            session_ledger=ledger_path,
            require_clean_state=require_clean_state,
            generated_at=generated,
        )
    finally:
        if lock_fd is not None and lock_path is not None:
            _release_cycle_lock(lock_fd, lock_path)


def _run_paper_auto_cycle_steps(
    *,
    as_of_date: str,
    source: str | Path | None = None,
    approved_dir: str | Path | None = None,
    dataset_id: str | None = "core_etfs",
    frequency: str | None = "1d",
    start: str,
    end: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    confirm_paper_auto: bool = False,
    provider: str = "manual_csv",
    license_note: str | None = None,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    signal_model: str | Path = "models/latest_model.json",
    paper_model_alias: str | Path | None = None,
    approved_output_dir: str | Path = "data/raw/approved",
    registry_dir: str | Path = "reports/registry",
    use_openai: bool = False,
    confirm_llm: bool = False,
    monitor: str | Path | None = None,
    performance: str | Path | None = None,
    operator_status: str | Path | None = None,
    campaign_report: str | Path | None = None,
    session_ledger: str | Path | None = None,
    require_clean_state: bool = False,
    generated_at: str | None = None,
) -> PaperAutoCycleResult:
    if bool(source) == bool(approved_dir):
        raise PaperAutoCycleOperationalError("provide exactly one of source or approved_dir")

    output_root = Path(output_dir) / as_of_date
    generated = generated_at or _utc_now()
    paths: dict[str, object] = {}
    reasons: list[str] = []
    steps: list[dict[str, object]] = []

    prepare = prepare_paper_daily(
        source=source,
        approved_dir=approved_dir,
        dataset_id=dataset_id,
        frequency=frequency,
        start=start,
        end=end,
        as_of_date=as_of_date,
        provider=provider,
        license_note=license_note,
        config=config,
        risk=risk,
        signal_model=signal_model,
        paper_model_alias=paper_model_alias,
        approved_output_dir=approved_output_dir,
        output_dir=output_root / "prepare",
        registry_dir=registry_dir,
        run_offline_smoke=True,
    )
    paths["readiness"] = str(prepare.readiness_path)
    paths["readiness_markdown"] = str(prepare.readiness_markdown_path)
    model_route = _mapping(prepare.payload.get("model_route"))
    steps.append(
        _step("prepare_paper_daily", prepare.status, prepare.exit_code, {"readiness": str(prepare.readiness_path)})
    )
    if prepare.exit_code != 0:
        reasons.extend(reason_codes(prepare.payload.get("reasons")) or [f"prepare_{prepare.status.lower()}"])
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED if prepare.exit_code == 1 else STATE_ERROR,
            exit_code=prepare.exit_code,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    signal_paths, signal_path_reasons = _resolve_signal_paths(prepare, output_root=output_root)
    paths.update(signal_paths)
    if signal_path_reasons:
        reasons.extend(signal_path_reasons)
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED,
            exit_code=1,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )
    context_digest_path, context_digest_markdown_path = _write_llm_context_digest(
        output_root=output_root,
        as_of_date=as_of_date,
        generated_at=generated,
        readiness_path=prepare.readiness_path,
        features_path=signal_paths["features"],
        model_signals_path=signal_paths["model_signals"],
    )
    paths["llm_context_digest"] = str(context_digest_path)
    paths["llm_context_digest_markdown"] = str(context_digest_markdown_path)
    proposals = run_llm_signal_proposals(
        as_of_date=as_of_date,
        readiness=prepare.readiness_path,
        features=signal_paths["features"],
        model_signals=signal_paths["model_signals"],
        output_dir=output_root / "llm_signal_proposals",
        use_openai=use_openai,
        confirm_llm=confirm_llm,
        context_digest=context_digest_path,
        generated_at=generated,
    )
    paths["llm_proposals"] = str(proposals.output_path)
    paths["llm_proposals_markdown"] = str(proposals.markdown_path)
    steps.append(
        _step("llm_signal_proposals", proposals.status, proposals.exit_code, {"json": str(proposals.output_path)})
    )
    if proposals.exit_code != 0:
        reasons.extend(_error_codes(proposals.payload) or [f"llm_proposals_{proposals.status.lower()}"])
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED if proposals.exit_code == 1 else STATE_ERROR,
            exit_code=proposals.exit_code,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    arbitration = run_paper_signal_arbitration(
        as_of_date=as_of_date,
        model_signals=signal_paths["model_signals"],
        llm_proposals=proposals.output_path,
        readiness=prepare.readiness_path,
        features=signal_paths["features"],
        output_dir=output_root / "arbitration",
        generated_at=generated,
    )
    paths["signal_plan"] = str(arbitration.output_path)
    paths["signal_plan_markdown"] = str(arbitration.markdown_path)
    steps.append(
        _step(
            "paper_signal_arbitration",
            arbitration.decision,
            arbitration.exit_code,
            {"json": str(arbitration.output_path)},
        )
    )
    if arbitration.exit_code != 0:
        reasons.extend(_reason_codes(arbitration.payload) or [f"arbitration_{arbitration.decision.lower()}"])
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED,
            exit_code=arbitration.exit_code,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    ops_path, evidence_path, kill_reasons = _write_auto_ops_evidence(
        output_root=output_root,
        as_of_date=as_of_date,
        generated_at=generated,
        readiness_path=prepare.readiness_path,
        proposals=proposals,
        arbitration=arbitration,
        monitor=monitor,
        performance=performance,
    )
    paths["ops_check"] = str(ops_path)
    paths["evidence_index"] = str(evidence_path)
    if monitor is not None:
        paths["monitor"] = str(Path(monitor))
    if performance is not None:
        paths["performance"] = str(Path(performance))
    if kill_reasons:
        reasons.extend(kill_reasons)
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED,
            exit_code=1,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    if not arbitration.eligible_for_paper:
        reasons.extend(_reason_codes(arbitration.payload) or ["no_trade_review"])
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_NO_TRADE_REVIEW,
            exit_code=0,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    if not confirm_paper_auto:
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_EVIDENCE_ONLY,
            exit_code=0,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=["confirm_paper_auto_missing"],
            session_ledger=session_ledger,
            model_route=model_route,
        )

    if operator_status is not None:
        paths["operator_status"] = str(Path(operator_status))
    if campaign_report is not None:
        paths["campaign_report"] = str(Path(campaign_report))
    clean_state_reasons = _operator_status_blockers(
        operator_status=operator_status,
        campaign_report=campaign_report,
        as_of_date=as_of_date,
        require_clean_state=require_clean_state,
    )
    if clean_state_reasons:
        reasons.extend(clean_state_reasons)
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_BLOCKED,
            exit_code=1,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    review = run_paper_review_decision(
        as_of_date=as_of_date,
        decision=DECISION_APPROVE_PAPER_CONFIRMATION,
        reviewer="paper-auto-cycle",
        reason="baseline and governed LLM proposal agreed on paper-only buy signal",
        output_dir=output_root / "auto_review",
        generated_at=generated,
    )
    paths["auto_review"] = str(review.output_path)
    paths["auto_review_markdown"] = str(review.markdown_path)
    steps.append(_step("paper_review_decision", review.status, review.exit_code, {"json": str(review.output_path)}))
    if review.exit_code != 0:
        reasons.extend(_error_codes(review.payload) or ["auto_review_error"])
        return _write_cycle(
            output_root=output_root,
            as_of_date=as_of_date,
            generated_at=generated,
            state=STATE_ERROR,
            exit_code=review.exit_code,
            confirm_paper_auto=confirm_paper_auto,
            paths=paths,
            steps=steps,
            reasons=reasons,
            session_ledger=session_ledger,
            model_route=model_route,
        )

    bot = run_paper_bot_cycle(
        as_of_date=as_of_date,
        readiness=prepare.readiness_path,
        human_review=review.output_path,
        ops_check=ops_path,
        evidence_index=evidence_path,
        signal_plan=arbitration.output_path,
        output_dir=output_root / "paper_bot_cycle",
        confirm_readiness=True,
        confirm_paper=True,
        confirm_auto_submit=True,
        confirm_auto_close=True,
        require_clean_state=require_clean_state,
        generated_at=generated,
    )
    paths["paper_bot_cycle"] = str(bot.output_path)
    paths["paper_bot_cycle_markdown"] = str(bot.markdown_path)
    steps.append(_step("paper_bot_cycle", bot.state, bot.exit_code, {"json": str(bot.output_path)}))
    reasons.extend(reason_codes(bot.payload.get("reasons")))
    return _write_cycle(
        output_root=output_root,
        as_of_date=as_of_date,
        generated_at=generated,
        state=bot.state if bot.state in {STATE_PAPER_SUBMITTED, STATE_PAPER_CLOSED} else bot.state,
        exit_code=bot.exit_code,
        confirm_paper_auto=confirm_paper_auto,
        paths=paths,
        steps=steps,
        reasons=reasons,
        paper_bot_cycle=bot,
        auto_review=review,
        session_ledger=session_ledger,
        model_route=model_route,
    )


def _confirmed_auto_relative_date_reasons(*, as_of_date: str, start: str, end: str) -> list[str]:
    reasons: list[str] = []
    if as_of_date == "today":
        reasons.append("confirmed_auto_as_of_date_must_be_explicit")
    if str(start).strip() == "today":
        reasons.append("confirmed_auto_from_must_be_explicit")
    if str(end).strip() == "today":
        reasons.append("confirmed_auto_to_must_be_explicit")
    return reasons


def render_paper_auto_cycle_markdown(payload: Mapping[str, object]) -> str:
    artifacts = _mapping(payload.get("artifacts"))
    reasons = _object_list(payload.get("reasons"))
    lines = [
        "# Paper Auto Cycle",
        "",
        f"State: **{payload.get('state') or STATE_ERROR}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Confirm paper auto: `{_mapping(payload.get('confirmations')).get('confirm_paper_auto') is True}`",
        "",
        "## Artifacts",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    for name, path in sorted(artifacts.items()):
        lines.append(f"| `{_escape(name)}` | `{_escape(path)}` |")
    lines.extend(["", "## Reasons", ""])
    if reasons:
        lines.extend(f"- `{_escape(reason)}`" for reason in reasons)
    else:
        lines.append("- `none`")
    lines.extend(
        [
            "",
            "Paper only: `True`",
            "Broker client built by wrapper: `False`",
            "Credentials read by wrapper: `False`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_cycle(
    *,
    output_root: Path,
    as_of_date: str,
    generated_at: str,
    state: str,
    exit_code: int,
    confirm_paper_auto: bool,
    paths: Mapping[str, object],
    steps: list[dict[str, object]],
    reasons: list[str],
    paper_bot_cycle: PaperBotCycleResult | None = None,
    auto_review: PaperReviewDecisionResult | None = None,
    session_ledger: str | Path | None = None,
    model_route: Mapping[str, object] | None = None,
) -> PaperAutoCycleResult:
    output_path = output_root / "cycle.json"
    markdown_path = output_root / "cycle.md"
    daily_status_path = output_root / "daily_status.json"
    daily_status_markdown_path = output_root / "daily_status.md"
    artifacts = {key: str(value) for key, value in paths.items()}
    artifacts["cycle_json"] = str(output_path)
    artifacts["cycle_markdown"] = str(markdown_path)
    artifacts["daily_status"] = str(daily_status_path)
    artifacts["daily_status_markdown"] = str(daily_status_markdown_path)
    safety = aggregate_safety(
        paper_bot_cycle.payload if paper_bot_cycle is not None else None,
        auto_review.payload if auto_review is not None else None,
    )
    payload = _redact_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "state": state,
            "exit_code": exit_code,
            "confirmations": {"confirm_paper_auto": confirm_paper_auto},
            "model_route": dict(
                model_route
                or {
                    "route_state": "CHAMPION",
                    "active_model_path": None,
                    "alias_hash": None,
                    "reason": "paper_model_alias_not_provided",
                }
            ),
            "steps": steps,
            "artifacts": artifacts,
            "paper_bot_cycle": _result_summary(paper_bot_cycle),
            "auto_review": _result_summary(auto_review),
            "reasons": _dedupe_strings(reasons),
            "authority": {
                "llm_authority": "none",
                "orders_submitted_by_wrapper": bool(safety.get("orders_submitted")),
                "observed_child_orders_submitted": bool(safety.get("orders_submitted")),
                "risk_changed": False,
                "live_trading_authorized": False,
            },
            "safety": safety,
        }
    )
    daily_status = _daily_status_payload(payload)
    write_json_artifact(payload, output_path)
    write_text_artifact(render_paper_auto_cycle_markdown(payload), markdown_path)
    write_json_artifact(daily_status, daily_status_path)
    write_text_artifact(_render_daily_status_markdown(daily_status), daily_status_markdown_path)
    if session_ledger is not None:
        _append_session_ledger(
            session_ledger,
            _session_record(payload, paper_bot_cycle=paper_bot_cycle, auto_review=auto_review),
        )
    return PaperAutoCycleResult(
        exit_code=exit_code,
        state=state,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _resolve_signal_paths(result: PaperDailyPrepareResult, *, output_root: Path) -> tuple[dict[str, str], list[str]]:
    readiness = result.payload
    session_path = _mapping(_mapping(readiness.get("offline_smoke")).get("artifacts")).get("session_json")
    model_signals_path: str | None = None
    features_path: str | None = None
    reasons: list[str] = []
    run_root = output_root.resolve()
    if session_path:
        try:
            session_json = _resolve_output_artifact(
                session_path, root=run_root, field="offline_smoke.artifacts.session_json"
            )
            session = read_json_artifact(session_json)
            model_signals_path_obj = _resolve_output_artifact(
                _mapping(session.get("paths")).get("signal_report"),
                root=run_root,
                base_dir=session_json.parent,
                field="session.paths.signal_report",
            )
            model_signals_path = str(model_signals_path_obj)
            freshness_path = _resolve_output_artifact(
                _mapping(session.get("paths")).get("freshness_report"),
                root=run_root,
                base_dir=session_json.parent,
                field="session.paths.freshness_report",
            )
            freshness = read_json_artifact(freshness_path)
            features_path_obj = _resolve_output_artifact(
                freshness.get("features_path"),
                root=run_root,
                base_dir=freshness_path.parent,
                field="freshness_report.features_path",
            )
            features_path = str(features_path_obj)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError):
            model_signals_path = None
            features_path = None
            reasons.append("invalid_session_json")
    else:
        reasons.append("missing_session_json")
    if not model_signals_path or not Path(model_signals_path).exists():
        reasons.append("missing_model_signals")
    if not features_path or not Path(features_path).exists():
        reasons.append("missing_features")
    paths: dict[str, str] = {}
    if model_signals_path:
        paths["model_signals"] = model_signals_path
    if features_path:
        paths["features"] = features_path
    return paths, _dedupe_strings(reasons)


def _resolve_output_artifact(
    value: object,
    *,
    root: Path,
    base_dir: Path | None = None,
    field: str,
) -> Path:
    if value in {None, ""}:
        raise RuntimeError(f"{field} is required")
    candidate = Path(str(value)).expanduser()
    if candidate.is_absolute():
        candidates = [candidate]
    else:
        candidates = []
        if base_dir is not None:
            candidates.append(base_dir / candidate)
        candidates.extend([Path.cwd() / candidate, root / candidate])
    resolved_root = root.resolve()
    found_outside_root = False
    for path in candidates:
        if not path.exists():
            continue
        resolved_candidate = path.resolve()
        if not _is_relative_to_root(resolved_candidate, resolved_root):
            found_outside_root = True
            continue
        return resolved_candidate
    if found_outside_root:
        raise RuntimeError(f"{field} is outside output root: {candidate}")
    searched = ", ".join(str(path) for path in candidates)
    raise RuntimeError(f"{field} does not exist: {searched}")


def _is_relative_to_root(candidate: Path, root: Path) -> bool:
    try:
        return candidate.is_relative_to(root)
    except AttributeError:
        candidate_text = str(candidate)
        root_text = str(root)
        return candidate_text == root_text or candidate_text.startswith(root_text + "/")


def _session_ledger_path(session_ledger: str | Path | None, *, output_dir: str | Path) -> Path:
    if session_ledger is not None:
        return Path(session_ledger)
    return Path(output_dir) / "session_ledger.jsonl"


def _append_session_ledger(path: str | Path, record: Mapping[str, object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_redact_value(record), sort_keys=True) + "\n")


def _session_record(
    cycle: Mapping[str, object],
    *,
    paper_bot_cycle: PaperBotCycleResult | None,
    auto_review: PaperReviewDecisionResult | None,
) -> dict[str, object]:
    artifacts = _mapping(cycle.get("artifacts"))
    state = str(cycle.get("state") or STATE_ERROR)
    generated_at = str(cycle.get("generated_at") or "")
    as_of_date = str(cycle.get("as_of_date") or "")
    return {
        "schema_version": "1.0",
        "record_type": "paper_auto_cycle_session",
        "session_id": _session_id(as_of_date=as_of_date, generated_at=generated_at),
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "state": state,
        "exit_code": _int_value(cycle.get("exit_code"), default=2),
        "confirm_paper_auto": _mapping(cycle.get("confirmations")).get("confirm_paper_auto") is True,
        "order_state": _order_state(state),
        "broker_artifacts": {
            "paper_bot_cycle": str(artifacts.get("paper_bot_cycle") or ""),
            "auto_review": str(artifacts.get("auto_review") or ""),
        },
        "closeout_status": _closeout_status(cycle, paper_bot_cycle=paper_bot_cycle),
        "statement_status": _statement_status(cycle),
        "unreconciled_fills": _unreconciled_fills(cycle),
        "blockers": reason_codes(cycle.get("reasons")),
        "artifacts": dict(artifacts),
        "paper_bot_cycle": _result_summary(paper_bot_cycle),
        "auto_review": _result_summary(auto_review),
        "authority": {
            "llm_authority": "none",
            "orders_submitted_by_wrapper": bool(_mapping(cycle.get("safety")).get("orders_submitted")),
            "observed_child_orders_submitted": bool(_mapping(cycle.get("safety")).get("orders_submitted")),
            "risk_changed": False,
            "live_trading_authorized": False,
        },
        "safety": dict(_mapping(cycle.get("safety"))),
    }


def _session_id(*, as_of_date: str, generated_at: str) -> str:
    compact = "".join(character if character.isalnum() else "" for character in generated_at)
    return f"paper-auto-{as_of_date}-{compact or 'unknown'}"


def _order_state(state: str) -> str:
    if state in {STATE_PAPER_SUBMITTED, STATE_PAPER_CLOSED}:
        return "paper_order_sent"
    return "not_sent"


def _closeout_status(cycle: Mapping[str, object], *, paper_bot_cycle: PaperBotCycleResult | None) -> str:
    if str(cycle.get("state") or "") == STATE_PAPER_CLOSED:
        return "CLOSED"
    if str(cycle.get("state") or "") == STATE_PAPER_SUBMITTED:
        return "PENDING"
    if paper_bot_cycle is not None and str(paper_bot_cycle.state) == STATE_PAPER_CLOSED:
        return "CLOSED"
    return "NOT_APPLICABLE"


def _statement_status(cycle: Mapping[str, object]) -> str:
    reasons = set(reason_codes(cycle.get("reasons")))
    if "statement_mismatch" in reasons:
        return "MISMATCH"
    if "fills_unreconciled" in reasons:
        return "UNRECONCILED"
    return "NOT_REQUESTED"


def _unreconciled_fills(cycle: Mapping[str, object]) -> int:
    return 1 if "fills_unreconciled" in set(reason_codes(cycle.get("reasons"))) else 0


def _operator_status_blockers(
    *,
    operator_status: str | Path | None,
    campaign_report: str | Path | None,
    as_of_date: str,
    require_clean_state: bool,
) -> list[str]:
    if not require_clean_state:
        return []
    if operator_status is None:
        return ["operator_status_required"]
    payload = _read_optional_json(operator_status)
    if payload is None:
        return ["operator_status_invalid"]
    reasons: list[str] = []
    if str(payload.get("as_of_date") or "") != as_of_date:
        reasons.append("operator_status_stale")
    if str(payload.get("status") or "").upper() != "OK" or payload.get("clean_for_paper_auto") is not True:
        blocker_codes = [
            str(blocker.get("code"))
            for blocker in _object_list(payload.get("blockers"))
            if isinstance(blocker, Mapping) and blocker.get("code")
        ]
        reasons.extend(blocker_codes or ["operator_status_not_clean"])
    safety = _mapping(payload.get("safety"))
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        reasons.append("live_trading_not_allowed")
    if safety.get("credentials_read") is True:
        reasons.append("credentials_read")
    if safety.get("broker_client_built") is True:
        reasons.append("broker_client_built")
    if safety.get("orders_submitted") is True:
        reasons.append("orders_submitted")
    reasons.extend(
        _campaign_report_blockers(
            campaign_report=campaign_report,
            as_of_date=as_of_date,
        )
    )
    return _dedupe_strings(reasons)


def _duplicate_confirmed_cycle_reasons(
    *,
    output_dir: str | Path,
    session_ledger: str | Path,
    as_of_date: str,
    require_clean_state: bool,
    confirm_paper_auto: bool,
) -> list[str]:
    if not (require_clean_state and confirm_paper_auto):
        return []
    reasons: list[str] = []
    cycle_path = Path(output_dir) / as_of_date / "cycle.json"
    cycle = _read_optional_json(cycle_path)
    if cycle is not None and _is_confirmed_same_date_cycle(cycle, as_of_date=as_of_date):
        reasons.append("duplicate_confirmed_cycle")
    ledger_path = Path(session_ledger)
    if ledger_path.exists():
        try:
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, Mapping) and _is_confirmed_same_date_cycle(record, as_of_date=as_of_date):
                reasons.append("duplicate_confirmed_cycle")
                break
    return _dedupe_strings(reasons)


def _is_confirmed_same_date_cycle(payload: Mapping[str, object], *, as_of_date: str) -> bool:
    state = str(payload.get("state") or "").upper()
    if str(payload.get("as_of_date") or "") != as_of_date:
        return False
    if state not in {STATE_PAPER_SUBMITTED, STATE_PAPER_CLOSED}:
        return False
    if payload.get("confirm_paper_auto") is True:
        return True
    confirmations = _mapping(payload.get("confirmations"))
    if confirmations.get("confirm_paper_auto") is True:
        return True
    return str(payload.get("order_state") or "").lower() == "paper_order_sent"


def _campaign_report_blockers(*, campaign_report: str | Path | None, as_of_date: str) -> list[str]:
    if campaign_report is None:
        return []
    payload = _read_optional_json(campaign_report)
    if payload is None:
        return ["campaign_report_invalid"]
    reasons: list[str] = []
    if str(payload.get("as_of_date") or "") not in {"", as_of_date}:
        reasons.append("campaign_report_stale")
    status = str(payload.get("status") or "").upper()
    if status in {"CRITICAL", "ERROR", "BLOCKED"}:
        reasons.append(f"campaign_{status.lower()}")
    paper_auto = _mapping(payload.get("paper_auto_campaign"))
    state = str(paper_auto.get("state") or "").upper()
    if state == "BLOCKED":
        histogram = paper_auto.get("blocker_histogram")
        if isinstance(histogram, Mapping):
            reasons.extend(str(code) for code in histogram)
        else:
            reasons.append("paper_auto_campaign_blocked")
    safety = _mapping(payload.get("safety"))
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        reasons.append("live_trading_not_allowed")
    if safety.get("credentials_read") is True:
        reasons.append("credentials_read")
    if safety.get("broker_client_built") is True:
        reasons.append("broker_client_built")
    if safety.get("orders_submitted") is True:
        reasons.append("orders_submitted")
    return reasons


def _write_auto_ops_evidence(
    *,
    output_root: Path,
    as_of_date: str,
    generated_at: str,
    readiness_path: Path,
    proposals: LLMSignalProposalsResult,
    arbitration: PaperSignalArbitrationResult,
    monitor: str | Path | None = None,
    performance: str | Path | None = None,
) -> tuple[Path, Path, list[str]]:
    external_artifacts, external_issues = _external_operational_issues(monitor=monitor, performance=performance)
    issues = _kill_switch_issues(proposals.payload, arbitration.payload, *external_artifacts.values())
    issues.extend(external_issues)
    status = "CRITICAL" if issues else "OK"
    ops_path = output_root / "ops_check" / "ops_check.json"
    evidence_path = output_root / "evidence_index" / "evidence_index.json"
    ops_payload = _redact_payload(
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": status,
            "issues": issues,
            "sources": {
                "readiness": str(readiness_path),
                "llm_proposals": str(proposals.output_path),
                "signal_plan": str(arbitration.output_path),
                "monitor": str(Path(monitor)) if monitor is not None else None,
                "performance": str(Path(performance)) if performance is not None else None,
            },
            "safety": {
                "paper_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
    )
    evidence_payload = _redact_payload(
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": "ERROR" if issues else "OK",
            "issues": issues,
            "artifacts": {
                "readiness": {"present": True, "status": "READY", "path": str(readiness_path)},
                "llm_proposals": {"present": True, "status": proposals.status, "path": str(proposals.output_path)},
                "signal_plan": {"present": True, "status": arbitration.decision, "path": str(arbitration.output_path)},
                "ops_check": {"present": True, "status": status, "path": str(ops_path)},
                **_external_evidence_artifacts(monitor=monitor, performance=performance, payloads=external_artifacts),
            },
            "safety": ops_payload["safety"],
        }
    )
    write_json_artifact(ops_payload, ops_path)
    write_json_artifact(evidence_payload, evidence_path)
    return ops_path, evidence_path, [str(issue.get("code")) for issue in issues]


def _kill_switch_issues(
    *payloads: Mapping[str, object],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for payload in payloads:
        safety = _mapping(payload.get("safety"))
        if safety.get("broker_client_built") is True:
            issues.append(_issue("CRITICAL", "broker_client_built", "broker client was built before confirmation"))
        if safety.get("credentials_read") is True:
            issues.append(_issue("CRITICAL", "credentials_read", "credentials were read before confirmation"))
        if safety.get("orders_submitted") is True:
            issues.append(_issue("CRITICAL", "orders_submitted", "orders were submitted before confirmation"))
        if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
            issues.append(_issue("CRITICAL", "live_trading_not_allowed", "live trading must remain disabled"))
    return issues


def _write_llm_context_digest(
    *,
    output_root: Path,
    as_of_date: str,
    generated_at: str,
    readiness_path: str | Path,
    features_path: str | Path,
    model_signals_path: str | Path,
) -> tuple[Path, Path]:
    output_path = output_root / "llm_context" / "context_digest.json"
    markdown_path = output_root / "llm_context" / "context_digest.md"
    items: list[dict[str, object]] = []
    status = "OK"
    readiness = _read_optional_json(readiness_path)
    model_signals = _read_optional_json(model_signals_path)
    if readiness is None:
        status = "WARN"
        items.append({"id": "readiness", "kind": "readiness", "path": str(Path(readiness_path)), "status": "MISSING"})
    else:
        items.append(
            {
                "id": "readiness",
                "kind": "readiness",
                "path": str(Path(readiness_path)),
                "status": str(readiness.get("status") or ""),
                "ready_for_paper_daily": readiness.get("ready_for_paper_daily") is True,
            }
        )
    if model_signals is None:
        status = "WARN"
        items.append(
            {"id": "model_signals", "kind": "model_signals", "path": str(Path(model_signals_path)), "status": "MISSING"}
        )
    else:
        signals = model_signals.get("signals")
        items.append(
            {
                "id": "model_signals",
                "kind": "model_signals",
                "path": str(Path(model_signals_path)),
                "status": "OK",
                "signal_count": len(signals) if isinstance(signals, list) else 0,
                "selected_symbol": _mapping(model_signals.get("selected_signal")).get("symbol"),
            }
        )
    items.append(
        {
            "id": "features",
            "kind": "features",
            "path": str(Path(features_path)),
            "status": "PRESENT" if Path(features_path).exists() else "MISSING",
        }
    )
    payload = _redact_payload(
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "as_of_date": as_of_date,
            "status": status,
            "items": items,
            "authority": {
                "llm_authority": "none",
                "orders_submitted": False,
                "risk_changed": False,
                "live_trading_authorized": False,
            },
            "safety": {
                "paper_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
    )
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_context_digest_markdown(payload), markdown_path)
    return output_path, markdown_path


def _external_operational_issues(
    *,
    monitor: str | Path | None,
    performance: str | Path | None,
) -> tuple[dict[str, Mapping[str, object]], list[dict[str, object]]]:
    payloads: dict[str, Mapping[str, object]] = {}
    issues: list[dict[str, object]] = []
    if monitor is not None:
        monitor_payload = _read_optional_json(monitor)
        if monitor_payload is None:
            issues.append(_issue("ERROR", "monitor_invalid", "monitor artifact is missing or invalid"))
        else:
            payloads["monitor"] = monitor_payload
            issues.extend(_monitor_issues(monitor_payload))
    if performance is not None:
        performance_payload = _read_optional_json(performance)
        if performance_payload is None:
            issues.append(_issue("ERROR", "performance_invalid", "performance artifact is missing or invalid"))
        else:
            payloads["performance"] = performance_payload
            issues.extend(_performance_issues(performance_payload))
    return payloads, _dedupe_issues(issues)


def _monitor_issues(payload: Mapping[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if status == "CRITICAL":
        issues.append(_issue("CRITICAL", "monitor_critical", "paper monitor is CRITICAL"))
    elif status == "ERROR":
        issues.append(_issue("ERROR", "monitor_error", "paper monitor is ERROR"))
    snapshot = _mapping(payload.get("broker_snapshot"))
    counts = _mapping(snapshot.get("counts"))
    if _int_value(counts.get("orders"), default=0) > 0:
        issues.append(_issue("CRITICAL", "open_broker_orders", "broker snapshot reports open paper orders"))
    if _int_value(counts.get("positions"), default=0) > 0:
        issues.append(_issue("CRITICAL", "existing_positions", "broker snapshot reports existing paper positions"))
    for alert in _object_list(payload.get("alerts")):
        if not isinstance(alert, Mapping):
            continue
        severity = str(alert.get("severity") or "").upper()
        if severity in {"CRITICAL", "ERROR"}:
            code = str(alert.get("code") or "monitor_alert")
            issues.append(_issue(severity, code, str(alert.get("message") or code)))
    return issues


def _performance_issues(payload: Mapping[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    status = str(payload.get("status") or "").upper()
    if status == "ERROR":
        issues.append(_issue("ERROR", "performance_error", "paper performance report is ERROR"))
    metrics = _mapping(payload.get("paper_metrics"))
    if _int_value(metrics.get("pending_closeouts"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_pending", "paper performance reports pending closeouts"))
    if _int_value(metrics.get("unmatched_closeouts"), default=0) > 0:
        issues.append(_issue("CRITICAL", "closeout_unmatched", "paper performance reports unmatched closeouts"))
    statement = _mapping(payload.get("statement_reconciliation"))
    statement_status = str(statement.get("status") or "").upper()
    if statement_status in {"ERROR", "MISMATCH", "UNMATCHED"}:
        issues.append(_issue("CRITICAL", "statement_mismatch", "paper statement reconciliation is not matched"))
    if _int_value(statement.get("missing_fills"), default=0) > 0:
        issues.append(_issue("CRITICAL", "fills_unreconciled", "paper statement is missing local fills"))
    for blocker in reason_codes(payload.get("blockers")):
        if blocker:
            issues.append(_issue("CRITICAL", blocker, blocker))
    return issues


def _external_evidence_artifacts(
    *,
    monitor: str | Path | None,
    performance: str | Path | None,
    payloads: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    if monitor is not None:
        artifacts["monitor"] = {
            "present": "monitor" in payloads,
            "status": str(payloads.get("monitor", {}).get("status") or "MISSING"),
            "path": str(Path(monitor)),
        }
    if performance is not None:
        artifacts["performance"] = {
            "present": "performance" in payloads,
            "status": str(payloads.get("performance", {}).get("status") or "MISSING"),
            "path": str(Path(performance)),
        }
    return artifacts


def _step(name: str, status: str, exit_code: int, artifacts: Mapping[str, object]) -> dict[str, object]:
    return {"name": name, "status": status, "exit_code": exit_code, "artifacts": dict(artifacts)}


def _result_summary(result: object | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "exit_code": getattr(result, "exit_code", None),
        "status": getattr(result, "status", None),
        "state": getattr(result, "state", None),
        "output_path": str(getattr(result, "output_path", "")),
        "markdown_path": str(getattr(result, "markdown_path", "")),
    }


def _issue(severity: str, code: str, message: str) -> dict[str, object]:
    return {"severity": severity, "code": code, "message": message}


def _daily_status_payload(cycle: Mapping[str, object]) -> dict[str, object]:
    state = str(cycle.get("state") or STATE_ERROR)
    normalized_reason_codes = reason_codes(cycle.get("reasons"))
    artifacts = _mapping(cycle.get("artifacts"))
    return _redact_payload(
        {
            "schema_version": "1.0",
            "generated_at": str(cycle.get("generated_at") or ""),
            "as_of_date": str(cycle.get("as_of_date") or ""),
            "state": state,
            "exit_code": _int_value(cycle.get("exit_code"), default=2),
            "next_safe_action": _next_safe_action(state),
            "reason_codes": normalized_reason_codes,
            "primary_artifacts": {
                "cycle": str(artifacts.get("cycle_json") or ""),
                "readiness": str(artifacts.get("readiness") or ""),
                "llm_context_digest": str(artifacts.get("llm_context_digest") or ""),
                "llm_proposals": str(artifacts.get("llm_proposals") or ""),
                "signal_plan": str(artifacts.get("signal_plan") or ""),
                "ops_check": str(artifacts.get("ops_check") or ""),
                "evidence_index": str(artifacts.get("evidence_index") or ""),
                "paper_bot_cycle": str(artifacts.get("paper_bot_cycle") or ""),
            },
            "safety": dict(_mapping(cycle.get("safety"))),
        }
    )


def _next_safe_action(state: str) -> str:
    if state == STATE_EVIDENCE_ONLY:
        return "review_artifacts"
    if state == STATE_NO_TRADE_REVIEW:
        return "manual_review"
    if state in {STATE_PAPER_SUBMITTED, STATE_PAPER_CLOSED}:
        return "review_broker_evidence"
    if state == STATE_BLOCKED:
        return "resolve_blockers"
    return "fix_operational_error"


def _render_daily_status_markdown(payload: Mapping[str, object]) -> str:
    reasons = reason_codes(payload.get("reason_codes"))
    lines = [
        "# Paper Auto Daily Status",
        "",
        f"State: **{payload.get('state') or STATE_ERROR}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Next safe action: `{payload.get('next_safe_action') or ''}`",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(f"- `{_escape(reason)}`" for reason in reasons) if reasons else lines.append("- `none`")
    lines.extend(["", "Paper only: `True`", "LLM authority: `none`", ""])
    return "\n".join(lines)


def _render_context_digest_markdown(payload: Mapping[str, object]) -> str:
    items = _object_list(payload.get("items"))
    lines = [
        "# LLM Context Digest",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        "",
        "| ID | Kind | Status | Path |",
        "| --- | --- | --- | --- |",
    ]
    if items:
        for item in items:
            if isinstance(item, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(item.get('id') or '')}` "
                    f"| `{_escape(item.get('kind') or '')}` "
                    f"| `{_escape(item.get('status') or '')}` "
                    f"| `{_escape(item.get('path') or '')}` |"
                )
    else:
        lines.append("| none | none | MISSING |  |")
    lines.extend(["", "LLM authority: `none`", "Broker client built: `False`", ""])
    return "\n".join(lines)


def _read_optional_json(path: str | Path) -> dict[str, object] | None:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _dedupe_issues(issues: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (str(issue.get("severity") or ""), str(issue.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def _int_value(value: object, *, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _cycle_lock_path(lock_dir: str | Path | None, *, as_of_date: str) -> Path | None:
    if lock_dir is None:
        return None
    return Path(lock_dir) / f"paper_auto_cycle_{as_of_date}.lock"


def _acquire_cycle_lock(path: Path, *, generated_at: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, f"generated_at={generated_at}\n".encode())
    return fd


def _cycle_lock_is_stale(path: Path, *, max_age_minutes: int) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    age_minutes = max((datetime.now(UTC) - modified).total_seconds() / 60.0, 0.0)
    return age_minutes > float(max_age_minutes)


def _remove_stale_cycle_lock(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _release_cycle_lock(fd: int, path: Path) -> None:
    try:
        os.close(fd)
    finally:
        with suppress(FileNotFoundError):
            path.unlink()


def _reason_codes(payload: Mapping[str, object]) -> list[str]:
    return [str(reason.get("code")) for reason in _object_list(payload.get("reasons")) if isinstance(reason, Mapping)]


def _error_codes(payload: Mapping[str, object]) -> list[str]:
    return [str(error.get("code")) for error in _object_list(payload.get("errors")) if isinstance(error, Mapping)]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _redact_payload(value: object) -> dict[str, object]:
    redacted = _redact_value(value)
    if not isinstance(redacted, dict):
        raise PaperAutoCycleOperationalError("paper auto cycle must be a JSON object")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
