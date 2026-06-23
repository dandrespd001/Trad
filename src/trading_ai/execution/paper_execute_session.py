"""Execute an approved offline paper-session package against Alpaca paper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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
    output_dir: str | Path | None = None,
    as_of_date: str | date = "today",
    max_feature_age_days: int = 5,
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

    try:
        client = build_alpaca_paper_client()
        broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk_limits, dry_run=False)
        account = broker.read_account()
        open_orders = broker.list_orders(status="open")
        positions = broker.read_positions()
    except Exception as exc:
        reason = redact_secrets(str(exc))
        payload = _execution_payload(
            status="ERROR",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
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
    if not preflight.allowed:
        payload = _execution_payload(
            status="BLOCKED",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
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
        broker_result = broker.submit_order(order)
        final_order = None
        if broker_result.accepted:
            final_order = broker.get_order_by_client_id(order.client_order_id)
    except Exception as exc:
        reason = redact_secrets(str(exc))
        payload = _execution_payload(
            status="ERROR",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_submit,
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
    status = "SUBMITTED" if broker_result.accepted else "BLOCKED"
    payload = _execution_payload(
        status=status,
        session_dir=root,
        output_dir=resolved_output_dir,
        confirm_paper=confirm_paper,
        confirm_submit=confirm_submit,
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
    )
    json_path, markdown_path = _write_execution_artifacts(payload, resolved_output_dir)
    return PaperSessionExecutionResult(
        exit_code=0 if broker_result.accepted else 1,
        status=status,
        output_dir=resolved_output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        reasons=tuple(broker_result.reasons),
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
    signal_path = _session_artifact_path(paths, "signal_report", session_dir / "paper" / "paper_signal_order.json", session_dir)
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
    if raw_path.is_absolute():
        candidates = [raw_path]
    else:
        candidates = [session_dir / raw_path, Path.cwd() / raw_path]
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
    operational_error: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "real-paper",
        "broker": "alpaca",
        "session": {
            "session_dir": str(session_dir),
            "session_json": str(session_dir / "session.json"),
            "ready_for_paper_review": package.session.get("ready_for_paper_review") is True,
            "as_of_date": package.session.get("as_of_date"),
        },
        "output_dir": str(output_dir),
        "confirmations": {
            "confirm_paper": confirm_paper,
            "confirm_submit": confirm_submit,
        },
        "execution": {
            "as_of_date": as_of_date.isoformat(),
            "max_feature_age_days": max_feature_age_days,
        },
        "preflight": _paper_preflight_to_dict(preflight),
        "open_orders": [_paper_order_snapshot_to_dict(order_snapshot) for order_snapshot in open_orders],
        "positions": [_paper_position_to_dict(position) for position in positions],
        "account": _paper_account_to_dict(account),
        "order_sent": _paper_order_intent_to_dict(order),
        "broker_result": _paper_order_result_to_dict(broker_result) if broker_result is not None else None,
        "final_order": _paper_order_snapshot_to_dict(final_order) if final_order is not None else None,
        "operational_error": operational_error,
    }


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
