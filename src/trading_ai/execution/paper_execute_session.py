"""Execute an approved offline paper-session package against Alpaca paper."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.config import load_risk_config, load_universe_config
from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    PaperOrder,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
    PaperPreflightDecision,
    evaluate_paper_preflight,
)
from trading_ai.execution.paper_common import as_of_date_to_date, reason_codes, redact_secrets
from trading_ai.execution.paper_graduation import (
    evaluate_paper_graduation,
    graduation_reasons,
    load_optional_json_report,
)
from trading_ai.execution.paper_position_plan import (
    build_position_plan,
    close_actions,
    dynamic_client_order_id,
    hold_actions,
    open_actions,
)
from trading_ai.execution.paper_risk_state import (
    DEFAULT_RISK_STATE_PATH,
    RiskState,
    compute_order_risk_inputs,
    evaluate_kill_switch,
    load_risk_state,
    roll_daily_equity,
    save_risk_state,
)
from trading_ai.risk.policy import RiskLimits

SCHEMA_VERSION = "1.0"


class PaperExecuteOperationalError(RuntimeError):
    """Raised for malformed inputs or missing execution prerequisites."""


@dataclass(frozen=True)
class PaperSessionExecutionResult:
    exit_code: int
    status: str
    output_dir: Path | None
    json_path: Path | None
    markdown_path: Path | None
    reasons: tuple[str, ...]


def run_paper_execute_session(
    *,
    session_dir: str | Path,
    confirm_paper: bool,
    confirm_submit: bool,
    confirm_dynamic_position_actions: bool = False,
    output_dir: str | Path | None = None,
    as_of_date: str | date = "today",
    max_feature_age_days: int = 5,
    risk_state_path: str | Path = DEFAULT_RISK_STATE_PATH,
) -> PaperSessionExecutionResult:
    if not confirm_paper or not confirm_submit:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_submit:
            missing.append("--confirm-submit")
        raise PaperExecuteOperationalError("paper execution requires " + " and ".join(missing))

    root = Path(session_dir)
    if not root.exists() or not root.is_dir():
        raise PaperExecuteOperationalError(f"session directory does not exist: {root}")
    resolved_output_dir = Path(output_dir) if output_dir is not None else root / "execution"
    resolved_as_of_date = as_of_date_to_date(as_of_date)

    package = _load_approved_session_package(root)
    risk_limits = _load_risk_from_session(package.session, root)
    local_reasons = _local_gate_reasons(package)
    local_reasons.extend(_paper_graduation_gate_reasons(package, root, risk_limits))
    if not local_reasons:
        universe = _load_universe_from_session(package.session, root)
        local_reasons = _approved_order_reasons(
            signal_report=package.signal_report,
            allowlist=universe.symbols,
            approved_notional=risk_limits.paper_notional_usd,
        )
    if local_reasons:
        return PaperSessionExecutionResult(
            exit_code=1,
            status="BLOCKED",
            output_dir=None,
            json_path=None,
            markdown_path=None,
            reasons=tuple(local_reasons),
        )

    universe = _load_universe_from_session(package.session, root)
    order = _approved_order_from_signal_report(package.signal_report)
    selected_signal = _mapping_required(package.signal_report.get("selected_signal"), "selected_signal")
    risk_state = load_risk_state(risk_state_path)

    try:
        client = build_alpaca_paper_client()
        broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk_limits, dry_run=False)
        account = broker.read_account()
        open_orders = broker.list_orders(status="open")
        positions = broker.read_positions()
        risk_state = roll_daily_equity(
            risk_state,
            equity=account.equity,
            as_of_date=resolved_as_of_date,
        )
        risk_state = evaluate_kill_switch(
            risk_state,
            max_drawdown_pct=float(risk_limits.max_drawdown_pct),
            equity=account.equity,
            max_consecutive_error_days=int(risk_limits.max_consecutive_error_days),
        )
        save_risk_state(risk_state, risk_state_path)
        order_risk_inputs = compute_order_risk_inputs(
            side=order.side,
            symbol=order.symbol,
            notional=order.notional,
            quantity=order.quantity,
            account_equity=account.equity,
            positions=positions,
            state=risk_state,
        )
        order = replace(
            order,
            daily_pnl_pct=order_risk_inputs.daily_pnl_pct,
            current_drawdown_pct=order_risk_inputs.current_drawdown_pct,
            projected_gross_exposure=order_risk_inputs.projected_gross_exposure,
            estimated_position_weight=order_risk_inputs.estimated_position_weight,
        )
    except Exception as exc:
        reason = redact_secrets(str(exc))
        _record_error_day(risk_state, risk_state_path=risk_state_path, risk_limits=risk_limits)
        payload = _execution_payload(
            status="ERROR",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
            confirm_dynamic_position_actions=confirm_dynamic_position_actions,
            as_of_date=resolved_as_of_date,
            max_feature_age_days=max_feature_age_days,
            package=package,
            preflight=None,
            order=order,
            account=None,
            positions=(),
            open_orders=(),
            broker_result=None,
            final_order=None,
            operational_error=reason,
        )
        json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
        return PaperSessionExecutionResult(
            exit_code=2,
            status="ERROR",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=(reason,),
        )
    preflight = evaluate_paper_preflight(
        signal=selected_signal,
        client_order_id=order.client_order_id,
        open_orders=open_orders,
        positions=positions,
        as_of_date=resolved_as_of_date,
        max_feature_age_days=max_feature_age_days,
    )
    signals = _signal_list(package.signal_report.get("signals"))
    position_plan = build_position_plan(
        signals=signals,
        selected_signal=selected_signal,
        positions=positions,
        signal_quality=_mapping_or_none(package.signal_report.get("signal_quality")),
        paper_notional_usd=float(risk_limits.paper_notional_usd),
        stop_loss_atr_mult=float(risk_limits.stop_loss_atr_mult),
        take_profit_atr_mult=float(risk_limits.take_profit_atr_mult),
        trailing_atr_mult=float(risk_limits.trailing_atr_mult),
        trailing_high_by_symbol=risk_state.trailing_stops,
    )
    risk_state = replace(risk_state, trailing_stops=_plan_trailing_highs(position_plan))
    save_risk_state(risk_state, risk_state_path)
    planned_closes = close_actions(position_plan)
    planned_opens = open_actions(position_plan)
    planned_holds = hold_actions(position_plan)
    if planned_closes and not confirm_dynamic_position_actions:
        payload = _execution_payload(
            status="BLOCKED",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
            confirm_dynamic_position_actions=confirm_dynamic_position_actions,
            as_of_date=resolved_as_of_date,
            max_feature_age_days=max_feature_age_days,
            package=package,
            preflight=preflight,
            order=order,
            account=account,
            positions=positions,
            open_orders=open_orders,
            broker_result=None,
            final_order=None,
            position_plan=position_plan,
            position_order_results=[],
        )
        json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
        return PaperSessionExecutionResult(
            exit_code=1,
            status="BLOCKED",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=("dynamic_position_close_confirmation_required",),
        )
    if not preflight.allowed and not planned_closes and not planned_holds:
        payload = _execution_payload(
            status="BLOCKED",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
            confirm_dynamic_position_actions=confirm_dynamic_position_actions,
            as_of_date=resolved_as_of_date,
            max_feature_age_days=max_feature_age_days,
            package=package,
            preflight=preflight,
            order=order,
            account=account,
            positions=positions,
            open_orders=open_orders,
            broker_result=None,
            final_order=None,
            position_plan=position_plan,
            position_order_results=[],
        )
        json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
        return PaperSessionExecutionResult(
            exit_code=1,
            status="BLOCKED",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=tuple(preflight.reasons),
        )

    try:
        position_order_results: list[dict[str, object]] = []
        for close_action in planned_closes:
            close_order = _order_from_close_action(close_action, as_of_date=resolved_as_of_date.isoformat())
            close_result = broker.submit_order(close_order)
            final_close_order = (
                broker.get_order_by_client_id(close_order.client_order_id) if close_result.accepted else None
            )
            position_order_results.append(
                {
                    "action": dict(close_action),
                    "order_sent": _paper_order_intent_to_dict(close_order),
                    "broker_result": _paper_order_result_to_dict(close_result),
                    "final_order": _paper_order_snapshot_to_dict(final_close_order)
                    if final_close_order is not None
                    else None,
                }
            )
        close_blockers = _dynamic_close_blockers(position_order_results)
        if close_blockers:
            payload = _execution_payload(
                status="BLOCKED",
                session_dir=root,
                output_dir=resolved_output_dir,
                confirm_paper=confirm_paper,
                confirm_submit=confirm_submit,
                confirm_dynamic_position_actions=confirm_dynamic_position_actions,
                as_of_date=resolved_as_of_date,
                max_feature_age_days=max_feature_age_days,
                package=package,
                preflight=preflight,
                order=order,
                account=account,
                positions=positions,
                open_orders=open_orders,
                broker_result=None,
                final_order=None,
                position_plan=position_plan,
                position_order_results=position_order_results,
            )
            json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
            return PaperSessionExecutionResult(
                exit_code=1,
                status="BLOCKED",
                output_dir=resolved_output_dir,
                json_path=json_path,
                markdown_path=markdown_path,
                reasons=tuple(close_blockers),
            )
        broker_result = None
        final_order = None
        if risk_state.kill_switch_active:
            # Safe mode: protective closes above still run, but no new exposure.
            broker.activate_kill_switch(risk_state.kill_switch_reason or "kill_switch_active")
        if planned_opens:
            if not preflight.allowed:
                payload = _execution_payload(
                    status="BLOCKED",
                    session_dir=root,
                    output_dir=resolved_output_dir,
                    confirm_paper=confirm_paper,
                    confirm_submit=confirm_submit,
                    confirm_dynamic_position_actions=confirm_dynamic_position_actions,
                    as_of_date=resolved_as_of_date,
                    max_feature_age_days=max_feature_age_days,
                    package=package,
                    preflight=preflight,
                    order=order,
                    account=account,
                    positions=positions,
                    open_orders=open_orders,
                    broker_result=None,
                    final_order=None,
                    position_plan=position_plan,
                    position_order_results=position_order_results,
                )
                json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
                return PaperSessionExecutionResult(
                    exit_code=1,
                    status="BLOCKED",
                    output_dir=resolved_output_dir,
                    json_path=json_path,
                    markdown_path=markdown_path,
                    reasons=tuple(preflight.reasons),
                )
            broker_result = broker.submit_order(order)
            if broker_result.accepted:
                final_order = broker.get_order_by_client_id(order.client_order_id)
    except Exception as exc:
        reason = redact_secrets(str(exc))
        _record_error_day(risk_state, risk_state_path=risk_state_path, risk_limits=risk_limits)
        payload = _execution_payload(
            status="ERROR",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
            confirm_dynamic_position_actions=confirm_dynamic_position_actions,
            as_of_date=resolved_as_of_date,
            max_feature_age_days=max_feature_age_days,
            package=package,
            preflight=preflight,
            order=order,
            account=account,
            positions=positions,
            open_orders=open_orders,
            broker_result=None,
            final_order=None,
            position_plan=position_plan,
            position_order_results=[],
            operational_error=reason,
        )
        json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
        return PaperSessionExecutionResult(
            exit_code=2,
            status="ERROR",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=(reason,),
        )
    if broker_result is not None:
        status = "SUBMITTED" if broker_result.accepted else "BLOCKED"
        reasons = tuple(broker_result.reasons)
        exit_code = 0 if broker_result.accepted else 1
    elif planned_closes:
        close_blockers = _dynamic_close_blockers(position_order_results)
        status = "SUBMITTED" if not close_blockers else "BLOCKED"
        reasons = tuple(close_blockers)
        exit_code = 0 if not close_blockers else 1
    elif planned_holds:
        status = "HELD"
        reasons = ()
        exit_code = 0
    else:
        status = "BLOCKED"
        reasons = ("no_dynamic_position_action",)
        exit_code = 1
    payload = _execution_payload(
        status=status,
        session_dir=root,
        output_dir=resolved_output_dir,
        confirm_paper=confirm_paper,
        confirm_submit=confirm_submit,
        confirm_dynamic_position_actions=confirm_dynamic_position_actions,
        as_of_date=resolved_as_of_date,
        max_feature_age_days=max_feature_age_days,
        package=package,
        preflight=preflight,
        order=order,
        account=account,
        positions=positions,
        open_orders=open_orders,
        broker_result=broker_result,
        final_order=final_order,
        position_plan=position_plan,
        position_order_results=position_order_results,
    )
    reconciliation = payload.get("fill_reconciliation")
    requires_attention = (
        bool(reconciliation.get("requires_attention", False)) if isinstance(reconciliation, Mapping) else False
    )
    if requires_attention:
        # A protective close did not fully fill, so residual exposure remains. Count it
        # toward the error streak; once it reaches max_consecutive_error_days the
        # kill-switch latches and the next paper-daily run is blocked before any submit.
        _record_error_day(risk_state, risk_state_path=risk_state_path, risk_limits=risk_limits)
        reasons = (*reasons, "unreconciled_close_fills")
    else:
        _record_clean_day(risk_state, risk_state_path=risk_state_path)
    json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
    return PaperSessionExecutionResult(
        exit_code=exit_code,
        status=status,
        output_dir=resolved_output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        reasons=reasons,
    )


@dataclass(frozen=True)
class _ApprovedSessionPackage:
    session: Mapping[str, object]
    audit_report: Mapping[str, object]
    signal_report: Mapping[str, object]
    freshness_report: Mapping[str, object]


def _load_approved_session_package(session_dir: Path) -> _ApprovedSessionPackage:
    session_path = session_dir / "session.json"
    session = _read_json_object(session_path)
    paths = _mapping_required(session.get("paths"), "session.paths")
    audit_path = _session_artifact_path(paths, "audit_report", session_dir / "audit" / "paper_audit.json", session_dir)
    signal_path = _session_artifact_path(
        paths,
        "signal_report",
        session_dir / "paper" / "paper_signal_order.json",
        session_dir,
    )
    freshness_path = _session_artifact_path(
        paths,
        "freshness_report",
        session_dir / "fresh_data" / "freshness.json",
        session_dir,
    )
    return _ApprovedSessionPackage(
        session=session,
        audit_report=_read_json_object(audit_path),
        signal_report=_read_json_object(signal_path),
        freshness_report=_read_json_object(freshness_path),
    )


def _read_json_object(path: Path) -> Mapping[str, object]:
    if not path.exists():
        raise PaperExecuteOperationalError(f"required paper-session artifact is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PaperExecuteOperationalError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise PaperExecuteOperationalError(f"{path} must contain a JSON object")
    return payload


def _session_artifact_path(
    paths: Mapping[str, object],
    key: str,
    default: Path,
    session_dir: Path,
) -> Path:
    raw_value = paths.get(key)
    field = f"session.paths.{key}"
    if raw_value in {None, ""}:
        return _resolve_session_path(default, session_dir=session_dir, field=field, require_inside_session=True)
    return _resolve_session_path(raw_value, session_dir=session_dir, field=field, require_inside_session=True)


def _local_gate_reasons(package: _ApprovedSessionPackage) -> list[str]:
    reasons: list[str] = []
    audit_summary = _mapping_or_empty(package.audit_report.get("summary"))
    session_summary = _mapping_or_empty(package.session.get("summary"))
    signal_report = package.signal_report
    selected_signal = _mapping_or_none(signal_report.get("selected_signal"))
    order_intent = _mapping_or_none(signal_report.get("order_intent"))
    order_result = _mapping_or_none(signal_report.get("order_result"))

    if package.session.get("ready_for_paper_review") is not True:
        reasons.append("session_not_ready_for_paper_review")
    if package.audit_report.get("ready_for_paper_review") is not True:
        reasons.append("audit_not_ready_for_paper_review")
    if _int_value(audit_summary.get("fail_count")) != 0:
        reasons.append("audit_fail_count_nonzero")
    if _int_value(session_summary.get("fail_count")) not in {None, 0}:
        reasons.append("session_fail_count_nonzero")
    if package.freshness_report.get("allowed") is not True:
        reasons.append("freshness_not_allowed")
    if signal_report.get("freshness_allowed") is not True:
        reasons.append("signal_freshness_not_allowed")
    if selected_signal is None:
        reasons.append("missing_selected_signal")
    elif str(selected_signal.get("action", "")).lower() != "buy":
        reasons.append("no_buy_signal")
    if order_intent is None:
        reasons.append("missing_order_intent")
    if order_result is None:
        reasons.append("missing_order_result")
    elif order_result.get("dry_run") is not True:
        reasons.append("order_result_not_dry_run")
    elif order_result.get("accepted") is not True:
        reasons.append("dry_run_order_not_accepted")
    return reasons


def _paper_graduation_gate_reasons(package: _ApprovedSessionPackage, session_dir: Path, risk_limits) -> list[str]:
    inputs = _mapping_or_empty(package.session.get("inputs"))
    campaign_path = _optional_existing_session_path(
        inputs.get("campaign_report"),
        session_dir,
        "session.inputs.campaign_report",
    )
    phase_path = _optional_existing_session_path(inputs.get("phase_review"), session_dir, "session.inputs.phase_review")
    expected = evaluate_paper_graduation(
        risk_limits=risk_limits,
        campaign_report=load_optional_json_report(campaign_path),
        phase_review=load_optional_json_report(phase_path),
        campaign_report_path=campaign_path,
        phase_review_path=phase_path,
    )
    current = package.session.get("paper_graduation")
    if not isinstance(current, Mapping):
        current = package.signal_report.get("paper_graduation")
    current_mapping = current if isinstance(current, Mapping) else None
    return graduation_reasons(current=current_mapping, expected=expected, signal_report=package.signal_report)


def _optional_existing_session_path(value: object, session_dir: Path, field: str) -> Path | None:
    if value in {None, ""}:
        return None
    return _resolve_session_path(value, session_dir, field)


def _approved_order_reasons(
    *,
    signal_report: Mapping[str, object],
    allowlist: tuple[str, ...],
    approved_notional: float,
) -> list[str]:
    reasons: list[str] = []
    selected_signal = _mapping_required(signal_report.get("selected_signal"), "selected_signal")
    order_intent = _mapping_required(signal_report.get("order_intent"), "order_intent")
    symbol = str(order_intent.get("symbol", "")).upper()
    selected_symbol = str(selected_signal.get("symbol", "")).upper()
    allowlisted = {item.upper() for item in allowlist}
    notional = _optional_float(order_intent.get("notional"))

    if symbol not in allowlisted:
        reasons.append("symbol_not_allowlisted")
    if selected_symbol and selected_symbol != symbol:
        reasons.append("selected_signal_order_symbol_mismatch")
    if str(order_intent.get("side", "")).lower() != "buy":
        reasons.append("unsupported_order_side")
    if str(order_intent.get("type", "")).lower() != "market":
        reasons.append("unsupported_order_type")
    if str(order_intent.get("time_in_force", "")).lower() != "day":
        reasons.append("unsupported_time_in_force")
    if "quantity" in order_intent or "qty" in order_intent:
        reasons.append("quantity_not_allowed")
    if notional is None:
        reasons.append("missing_notional")
    elif abs(notional - approved_notional) > 1e-9:
        reasons.append("notional_exceeds_limit" if notional > approved_notional else "notional_below_approved")
    if not str(order_intent.get("client_order_id", "")).strip():
        reasons.append("missing_client_order_id")
    return reasons


def _approved_order_from_signal_report(signal_report: Mapping[str, object]) -> PaperOrder:
    order_intent = _mapping_required(signal_report.get("order_intent"), "order_intent")
    notional = _optional_float(order_intent.get("notional"))
    if notional is None:
        raise PaperExecuteOperationalError("order_intent.notional is required")
    return PaperOrder(
        symbol=str(order_intent["symbol"]).upper(),
        side=str(order_intent["side"]).lower(),
        notional=notional,
        client_order_id=str(order_intent["client_order_id"]),
    )


def _order_from_close_action(action: Mapping[str, object], *, as_of_date: str) -> PaperOrder:
    quantity = _optional_float(action.get("quantity"))
    if quantity is None:
        raise PaperExecuteOperationalError("dynamic close action missing quantity")
    symbol = str(action.get("symbol") or "").upper()
    if not symbol:
        raise PaperExecuteOperationalError("dynamic close action missing symbol")
    return PaperOrder(
        symbol=symbol,
        side="sell",
        quantity=quantity,
        client_order_id=dynamic_client_order_id(prefix="dynamic-close", symbol=symbol, as_of_date=as_of_date),
        notional=None,
    )


def _signal_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _plan_trailing_highs(position_plan: Mapping[str, object]) -> dict[str, float]:
    summary = _mapping_or_empty(position_plan.get("summary"))
    highs = summary.get("trailing_highs")
    result: dict[str, float] = {}
    if isinstance(highs, Mapping):
        for symbol, value in highs.items():
            number = _optional_float(value)
            if number is not None:
                result[str(symbol).upper()] = number
    return result


def _dynamic_close_blockers(position_order_results: list[dict[str, object]]) -> list[str]:
    blockers: list[str] = []
    for item in position_order_results:
        result = _mapping_or_empty(item.get("broker_result"))
        if result.get("accepted") is not True:
            blockers.append(str(result.get("status") or "dynamic_close_not_accepted"))
    return blockers


def _load_universe_from_session(session: Mapping[str, object], session_dir: Path):
    inputs = _mapping_required(session.get("inputs"), "session.inputs")
    config_path = _resolve_session_path(inputs.get("config"), session_dir, "session.inputs.config")
    return load_universe_config(config_path)


def _load_risk_from_session(session: Mapping[str, object], session_dir: Path):
    inputs = _mapping_required(session.get("inputs"), "session.inputs")
    risk_path = _resolve_session_path(inputs.get("risk"), session_dir, "session.inputs.risk")
    return load_risk_config(risk_path)


def _resolve_session_path(
    value: object,
    session_dir: Path,
    field: str,
    *,
    require_inside_session: bool = False,
) -> Path:
    if value in {None, ""}:
        raise PaperExecuteOperationalError(f"{field} is required")
    raw_path = Path(str(value)).expanduser()
    candidates = [raw_path] if raw_path.is_absolute() else [session_dir / raw_path, Path.cwd() / raw_path]
    resolved_root = session_dir.resolve()
    found_outside_session = False
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved_candidate = candidate.resolve()
        if require_inside_session and not _is_relative_to(resolved_candidate, resolved_root):
            found_outside_session = True
            continue
        return resolved_candidate
    if found_outside_session:
        raise PaperExecuteOperationalError(f"{field} must be inside session directory: {raw_path}")
    searched = ", ".join(str(path) for path in candidates)
    raise PaperExecuteOperationalError(f"{field} does not exist: {searched}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except AttributeError:
        # Python < 3.9 compatibility
        root_text = str(root)
        candidate_text = str(path)
        return candidate_text == root_text or candidate_text.startswith(root_text + "/")


def _execution_payload(
    *,
    status: str,
    session_dir: Path,
    output_dir: Path,
    confirm_paper: bool,
    confirm_submit: bool,
    confirm_dynamic_position_actions: bool,
    as_of_date: date,
    max_feature_age_days: int,
    package: _ApprovedSessionPackage,
    preflight: PaperPreflightDecision | None,
    order: PaperOrder,
    account: Any | None,
    positions: tuple[PaperPosition, ...],
    open_orders: tuple[PaperOrderSnapshot, ...],
    broker_result: PaperOrderResult | None,
    final_order: PaperOrderSnapshot | None,
    position_plan: Mapping[str, object] | None = None,
    position_order_results: list[dict[str, object]] | None = None,
    operational_error: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "real-paper",
        "broker": "alpaca",
        "session": {
            "session_dir": str(session_dir),
            "session_json": str(session_dir / "session.json"),
            "ready_for_paper_review": package.session.get("ready_for_paper_review") is True,
            "as_of_date": package.session.get("as_of_date"),
        },
        "paper_graduation": _mapping_or_empty(package.session.get("paper_graduation"))
        or _mapping_or_empty(package.signal_report.get("paper_graduation")),
        "output_dir": str(output_dir),
        "confirmations": {
            "confirm_paper": confirm_paper,
            "confirm_submit": confirm_submit,
            "confirm_dynamic_position_actions": confirm_dynamic_position_actions,
        },
        "execution": {
            "as_of_date": as_of_date.isoformat(),
            "max_feature_age_days": max_feature_age_days,
        },
        "preflight": _paper_preflight_to_dict(preflight),
        "open_orders": [_paper_order_snapshot_to_dict(order_snapshot) for order_snapshot in open_orders],
        "positions": [_paper_position_to_dict(position) for position in positions],
        "position_plan": dict(position_plan or {}),
        "position_order_results": position_order_results or [],
        "account": _paper_account_to_dict(account),
        "order_sent": _paper_order_intent_to_dict(order),
        "broker_result": _paper_order_result_to_dict(broker_result) if broker_result is not None else None,
        "final_order": _paper_order_snapshot_to_dict(final_order) if final_order is not None else None,
        "fill_reconciliation": _fill_reconciliation_summary(
            _paper_order_snapshot_to_dict(final_order) if final_order is not None else None,
            position_order_results or [],
        ),
        "operational_error": operational_error,
    }


def _record_error_day(
    risk_state: RiskState,
    *,
    risk_state_path: str | Path,
    risk_limits: RiskLimits,
) -> RiskState:
    """Count an operational error / unreconciled-fill day toward the kill-switch streak.

    Increments ``consecutive_error_days`` and re-evaluates the latching kill-switch so
    that, once the streak reaches ``max_consecutive_error_days``, safe mode trips and
    the next ``paper-daily`` run is blocked before any new submit.
    """

    updated = replace(risk_state, consecutive_error_days=risk_state.consecutive_error_days + 1)
    updated = evaluate_kill_switch(
        updated,
        max_drawdown_pct=float(risk_limits.max_drawdown_pct),
        max_consecutive_error_days=int(risk_limits.max_consecutive_error_days),
    )
    save_risk_state(updated, risk_state_path)
    return updated


def _record_clean_day(risk_state: RiskState, *, risk_state_path: str | Path) -> RiskState:
    """Reset the consecutive-error streak after a clean, reconciled run."""

    if risk_state.consecutive_error_days == 0:
        return risk_state
    updated = replace(risk_state, consecutive_error_days=0)
    save_risk_state(updated, risk_state_path)
    return updated


def _fill_reconciliation_summary(
    open_final_order: Mapping[str, object] | None,
    position_order_results: list[dict[str, object]],
) -> dict[str, object]:
    """Report partial/unfilled orders after submission.

    Market orders usually fill immediately, but a real broker can leave an order
    pending or partially filled. ``issues`` namespaces each problem as ``open:`` or
    ``close:SYMBOL:``. ``requires_attention`` is True only when a protective/de-risking
    **close** did not fully fill — that leaves residual exposure and counts as an error
    day toward the kill-switch (see ``_record_error_day``). A merely pending *open*
    (e.g. an EOD market order accepted while the market is closed) is reported but is
    not, on its own, an error.
    """

    issues: list[str] = []
    if open_final_order is not None:
        issues.extend(f"open:{code}" for code in _fill_issues(open_final_order, expected_quantity=None))
    for item in position_order_results:
        final_order = item.get("final_order")
        action = item.get("action") if isinstance(item.get("action"), Mapping) else {}
        symbol = str(action.get("symbol") or "") if isinstance(action, Mapping) else ""
        expected_quantity = _optional_float(action.get("quantity")) if isinstance(action, Mapping) else None
        if isinstance(final_order, Mapping):
            issues.extend(
                f"close:{symbol}:{code}" for code in _fill_issues(final_order, expected_quantity=expected_quantity)
            )
    requires_attention = any(issue.startswith("close:") for issue in issues)
    return {"reconciled": not issues, "issues": issues, "requires_attention": requires_attention}


def _fill_issues(order: Mapping[str, object], *, expected_quantity: float | None) -> list[str]:
    filled = _optional_float(order.get("filled_quantity")) or 0.0
    status = str(order.get("status") or "").lower()
    issues: list[str] = []
    if filled <= 0 and status not in {"filled", "partially_filled"}:
        issues.append("unfilled_or_pending")
    if expected_quantity is not None and expected_quantity > 0 and filled + 1e-9 < expected_quantity:
        issues.append("partial_fill")
    return issues


def _write_execution_artifacts(payload: Mapping[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paper_execution.json"
    markdown_path = output_dir / "paper_execution.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_paper_execution_markdown(payload), encoding="utf-8")
    return json_path, markdown_path


def render_paper_execution_markdown(payload: Mapping[str, object]) -> str:
    order_sent = _mapping_or_empty(payload.get("order_sent"))
    broker_result = _mapping_or_empty(payload.get("broker_result"))
    preflight = _mapping_or_empty(payload.get("preflight"))
    final_order = _mapping_or_empty(payload.get("final_order"))
    status = str(payload.get("status", "BLOCKED"))
    reasons = reason_codes(preflight.get("reasons")) or reason_codes(broker_result.get("reasons"))
    lines = [
        "# Paper Session Execution",
        "",
        f"Status: **{status}**",
        "",
        f"Mode: `{payload.get('mode') or ''}`",
        f"Broker: `{payload.get('broker') or ''}`",
        f"Symbol: `{order_sent.get('symbol') or ''}`",
        f"Side: `{order_sent.get('side') or ''}`",
        f"Notional: `{order_sent.get('notional') or ''}`",
        f"Client order ID: `{order_sent.get('client_order_id') or ''}`",
        f"Preflight allowed: `{preflight.get('allowed')}`",
        f"Reasons: `{', '.join(reasons)}`",
        f"Broker result: `{broker_result.get('status') or ''}`",
        f"Final order status: `{final_order.get('status') or ''}`",
        "",
    ]
    return "\n".join(lines)


def _paper_order_intent_to_dict(order: PaperOrder) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": order.symbol.upper(),
        "side": order.side.lower(),
        "client_order_id": order.client_order_id,
        "type": "market",
        "time_in_force": "day",
    }
    if order.quantity is not None:
        payload["quantity"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional
    payload["risk_inputs"] = {
        "daily_pnl_pct": order.daily_pnl_pct,
        "current_drawdown_pct": order.current_drawdown_pct,
        "projected_gross_exposure": order.projected_gross_exposure,
        "estimated_position_weight": order.estimated_position_weight,
    }
    return payload


def _paper_order_result_to_dict(result: PaperOrderResult) -> dict[str, object]:
    return {
        "accepted": result.accepted,
        "status": result.status,
        "reasons": list(result.reasons),
        "dry_run": result.dry_run,
        "broker_response": _broker_response_to_dict(result.broker_response),
    }


def _paper_order_snapshot_to_dict(order: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "status": order.status,
        "notional": order.notional,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "filled_avg_price": order.filled_avg_price,
        "submitted_at": order.submitted_at,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "expires_at": order.expires_at,
    }


def _paper_preflight_to_dict(decision: PaperPreflightDecision | None) -> dict[str, object]:
    if decision is None:
        return {
            "allowed": False,
            "reasons": ["broker_preflight_unavailable"],
            "checked_at": None,
            "max_feature_age_days": None,
        }
    return {
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "max_feature_age_days": decision.max_feature_age_days,
    }


def _paper_account_to_dict(account: Any | None) -> dict[str, object] | None:
    if account is None:
        return None
    return {
        "account_id": account.account_id,
        "status": account.status,
        "cash": account.cash,
        "equity": account.equity,
        "buying_power": account.buying_power,
        "dry_run": account.status == "DRY_RUN",
    }


def _paper_position_to_dict(position: PaperPosition) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "market_value": position.market_value,
        "avg_entry_price": position.avg_entry_price,
        "current_price": position.current_price,
    }


def _broker_response_to_dict(response: Any) -> object:
    if response is None:
        return None
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return {"repr": repr(response)}


def _mapping_required(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PaperExecuteOperationalError(f"{field} must be a JSON object")
    return value


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _optional_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def execution_report_schema_errors(payload: Mapping[str, object]) -> list[str]:
    """
    Validate execution report shape before matching against expected order.

    This is intentionally defensive and keeps required closeout checks cheap and safe.
    """
    errors: list[str] = []

    if str(payload.get("status") or "").strip() == "":
        errors.append("execution_status_missing")
    if not isinstance(payload.get("session"), Mapping):
        errors.append("execution_session_missing")

    order_sent = payload.get("order_sent")
    if not isinstance(order_sent, Mapping):
        errors.append("execution_order_sent_missing")
    else:
        for field in ("client_order_id", "symbol", "side", "notional"):
            if str(order_sent.get(field) or "").strip() == "":
                errors.append(f"execution_order_sent_{field}_missing")
        if str(order_sent.get("type") or "").strip() == "":
            errors.append("execution_order_sent_type_missing")
        if str(order_sent.get("time_in_force") or "").strip() == "":
            errors.append("execution_order_sent_time_in_force_missing")

    broker_result = payload.get("broker_result")
    if broker_result is not None and not isinstance(broker_result, Mapping):
        errors.append("execution_broker_result_not_object")

    return reason_codes(errors)
