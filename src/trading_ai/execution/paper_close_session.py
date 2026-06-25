"""Close out evidence for a submitted Alpaca paper session order."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from trading_ai.execution.alpaca_connection import build_alpaca_paper_client
from trading_ai.execution.alpaca_paper import AlpacaPaperBroker, PaperOrder, PaperOrderSnapshot, PaperPosition
from trading_ai.execution.paper_common import redact_secrets
from trading_ai.execution.paper_execute_session import (
    SCHEMA_VERSION,
    PaperExecuteOperationalError,
    _approved_order_from_signal_report,
    _approved_order_reasons,
    _load_approved_session_package,
    _load_risk_from_session,
    _load_universe_from_session,
    _local_gate_reasons,
    _mapping_or_empty,
    _paper_account_to_dict,
    _paper_graduation_gate_reasons,
    _paper_order_intent_to_dict,
    _paper_order_snapshot_to_dict,
    _paper_position_to_dict,
    _read_json_object,
    execution_report_schema_errors,
    reason_codes,
)

FILLED_STATUSES = {"filled", "partially_filled", "partially filled"}
TERMINAL_UNMATCHED_STATUSES = {"canceled", "cancelled", "rejected", "expired"}


class PaperCloseOperationalError(RuntimeError):
    """Raised for malformed closeout inputs or missing paper prerequisites."""


class _OrderIdentity(TypedDict):
    client_order_id: str | None
    symbol: str | None
    side: str | None
    notional: float | None


@dataclass(frozen=True)
class PaperCloseSessionResult:
    exit_code: int
    status: str
    output_dir: Path | None
    json_path: Path | None
    markdown_path: Path | None
    reasons: tuple[str, ...]
    session_dir: Path
    client_order_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    notional: float | None = None


def run_paper_close_session(
    *,
    session_dir: str | Path,
    confirm_paper: bool,
    execution_report: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> PaperCloseSessionResult:
    if not confirm_paper:
        raise PaperCloseOperationalError("paper closeout requires --confirm-paper")

    root = Path(session_dir)
    if not root.exists() or not root.is_dir():
        raise PaperCloseOperationalError(f"session directory does not exist: {root}")
    resolved_execution_report = (
        Path(execution_report) if execution_report is not None else root / "execution" / "paper_execution.json"
    )
    resolved_output_dir = Path(output_dir) if output_dir is not None else root / "closeout"

    package = _load_package(root)
    risk_limits = _load_risk(root, package.session)
    universe = None
    local_reasons = _local_gate_reasons(package)
    local_reasons.extend(_paper_graduation_gate_reasons(package, root, risk_limits))
    if not local_reasons:
        universe = _load_universe(root, package.session)
        local_reasons = _approved_order_reasons(
            signal_report=package.signal_report,
            allowlist=universe.symbols,
            risk_limits=risk_limits,
        )
    if local_reasons:
        return PaperCloseSessionResult(
            exit_code=1,
            status="BLOCKED",
            output_dir=None,
            json_path=None,
            markdown_path=None,
            reasons=tuple(local_reasons),
            session_dir=root,
            **_order_identity_from_signal_report(package.signal_report),
        )

    order = _approved_order_from_signal_report(package.signal_report)
    order_identity = _order_identity(order)
    execution = _load_execution_report(resolved_execution_report)
    execution_schema = execution_report_schema_errors(execution)
    if execution_schema:
        blocker_reasons = ("execution_schema_invalid", *tuple(execution_schema))
        payload = _closeout_payload(
            status="UNMATCHED",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            execution_report=resolved_execution_report,
            execution=execution,
            package=package,
            expected_order=order,
            account=None,
            positions=(),
            open_orders=(),
            broker_order=None,
            reasons=list(blocker_reasons),
        )
        json_path, markdown_path = _write_closeout_artifacts(payload, resolved_output_dir)
        return PaperCloseSessionResult(
            exit_code=1,
            status="UNMATCHED",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=blocker_reasons,
            session_dir=root,
            **order_identity,
        )

    execution_reasons = _execution_report_reasons(execution, expected_order=order)
    if _execution_status(execution) != "SUBMITTED":
        return PaperCloseSessionResult(
            exit_code=1,
            status="BLOCKED",
            output_dir=None,
            json_path=None,
            markdown_path=None,
            reasons=("execution_not_submitted", *tuple(execution_reasons)),
            session_dir=root,
            **order_identity,
        )
    if execution_reasons:
        payload = _closeout_payload(
            status="UNMATCHED",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            execution_report=resolved_execution_report,
            execution=execution,
            package=package,
            expected_order=order,
            account=None,
            positions=(),
            open_orders=(),
            broker_order=None,
            reasons=execution_reasons,
        )
        json_path, markdown_path = _write_closeout_artifacts(payload, resolved_output_dir)
        return PaperCloseSessionResult(
            exit_code=1,
            status="UNMATCHED",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=tuple(execution_reasons),
            session_dir=root,
            **order_identity,
        )

    if universe is None:
        universe = _load_universe(root, package.session)
    risk_limits = _load_risk(root, package.session)
    try:
        client = build_alpaca_paper_client()
        broker = AlpacaPaperBroker(client=client, allowlist=universe.symbols, risk_limits=risk_limits, dry_run=False)
        account = broker.read_account()
        positions = broker.read_positions()
        open_orders = broker.list_orders(status="open")
    except Exception as exc:  # pragma: no cover - broker/client dependent
        reason = redact_secrets(str(exc))
        payload = _closeout_payload(
            status="ERROR",
            session_dir=root,
            output_dir=resolved_output_dir,
            confirm_paper=confirm_paper,
            execution_report=resolved_execution_report,
            execution=execution,
            package=package,
            expected_order=order,
            account=None,
            positions=(),
            open_orders=(),
            broker_order=None,
            reasons=[reason],
        )
        json_path, markdown_path = _write_closeout_artifacts(payload, resolved_output_dir)
        return PaperCloseSessionResult(
            exit_code=2,
            status="ERROR",
            output_dir=resolved_output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            reasons=(reason,),
            session_dir=root,
            **order_identity,
        )

    broker_order, order_missing_reason = _get_broker_order(broker, order.client_order_id)
    status, reasons = _closeout_status_and_reasons(
        expected_order=order,
        broker_order=broker_order,
        positions=positions,
        order_missing_reason=order_missing_reason,
    )
    payload = _closeout_payload(
        status=status,
        session_dir=root,
        output_dir=resolved_output_dir,
        confirm_paper=confirm_paper,
        execution_report=resolved_execution_report,
        execution=execution,
        package=package,
        expected_order=order,
        account=account,
        positions=positions,
        open_orders=open_orders,
        broker_order=broker_order,
        reasons=reasons,
    )
    json_path, markdown_path = _write_closeout_artifacts(payload, resolved_output_dir)
    return PaperCloseSessionResult(
        exit_code=0 if status == "CLOSED" else 1,
        status=status,
        output_dir=resolved_output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        reasons=tuple(reasons),
        session_dir=root,
        **order_identity,
    )


def _load_package(session_dir: Path):
    try:
        return _load_approved_session_package(session_dir)
    except PaperExecuteOperationalError as exc:
        raise PaperCloseOperationalError(str(exc)) from exc


def _load_universe(session_dir: Path, session: Mapping[str, object]):
    try:
        return _load_universe_from_session(session, session_dir)
    except PaperExecuteOperationalError as exc:
        raise PaperCloseOperationalError(str(exc)) from exc


def _load_risk(session_dir: Path, session: Mapping[str, object]):
    try:
        return _load_risk_from_session(session, session_dir)
    except PaperExecuteOperationalError as exc:
        raise PaperCloseOperationalError(str(exc)) from exc


def _load_execution_report(path: Path) -> Mapping[str, object]:
    try:
        return _read_json_object(path)
    except PaperExecuteOperationalError as exc:
        raise PaperCloseOperationalError(str(exc)) from exc


def _execution_status(execution: Mapping[str, object]) -> str:
    return str(execution.get("status") or "").upper()


def _execution_report_reasons(execution: Mapping[str, object], *, expected_order: PaperOrder) -> list[str]:
    reasons: list[str] = []
    order_sent = _mapping_or_empty(execution.get("order_sent"))
    if not order_sent:
        return ["missing_execution_order_sent"]
    reasons.extend(_order_mapping_mismatch_reasons(order_sent, expected_order, prefix="execution"))
    broker_result = _mapping_or_empty(execution.get("broker_result"))
    if broker_result and broker_result.get("accepted") is not True:
        reasons.append("execution_broker_result_not_accepted")
    return reasons


def _order_mapping_mismatch_reasons(
    order_mapping: Mapping[str, object],
    expected_order: PaperOrder,
    *,
    prefix: str,
) -> list[str]:
    reasons: list[str] = []
    if str(order_mapping.get("client_order_id", "")) != expected_order.client_order_id:
        reasons.append(f"{prefix}_client_order_id_mismatch")
    if str(order_mapping.get("symbol", "")).upper() != expected_order.symbol.upper():
        reasons.append(f"{prefix}_symbol_mismatch")
    if str(order_mapping.get("side", "")).lower() != expected_order.side.lower():
        reasons.append(f"{prefix}_side_mismatch")
    notional = _optional_float(order_mapping.get("notional"))
    expected_notional = float(expected_order.notional or 0.0)
    if notional is None or abs(notional - expected_notional) > 1e-9:
        reasons.append(f"{prefix}_notional_mismatch")
    return reasons


def _get_broker_order(
    broker: AlpacaPaperBroker,
    client_order_id: str,
) -> tuple[PaperOrderSnapshot | None, str | None]:
    try:
        return broker.get_order_by_client_id(client_order_id), None
    except Exception:
        return None, "order_missing"


def _closeout_status_and_reasons(
    *,
    expected_order: PaperOrder,
    broker_order: PaperOrderSnapshot | None,
    positions: tuple[PaperPosition, ...],
    order_missing_reason: str | None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if order_missing_reason is not None:
        return "UNMATCHED", [order_missing_reason]
    if broker_order is None:
        return "UNMATCHED", ["order_missing"]

    reasons.extend(_broker_order_mismatch_reasons(broker_order, expected_order))
    broker_status = broker_order.status.lower()
    if broker_status in TERMINAL_UNMATCHED_STATUSES:
        reasons.append(f"order_{broker_status}")
    if reasons:
        return "UNMATCHED", reasons

    matching_position = _matching_position(positions, expected_order.symbol)
    if broker_status in FILLED_STATUSES and broker_order.filled_quantity > 0 and matching_position is not None:
        return "CLOSED", []

    pending_reasons: list[str] = []
    if broker_order.filled_quantity <= 0:
        pending_reasons.append("not_filled_yet")
    if broker_status not in FILLED_STATUSES:
        pending_reasons.append(f"order_status_{broker_status or 'unknown'}")
    if matching_position is None:
        pending_reasons.append("position_missing")
    return "PENDING", _dedupe(pending_reasons)


def _broker_order_mismatch_reasons(order: PaperOrderSnapshot, expected_order: PaperOrder) -> list[str]:
    reasons: list[str] = []
    if order.client_order_id != expected_order.client_order_id:
        reasons.append("broker_client_order_id_mismatch")
    if order.symbol.upper() != expected_order.symbol.upper():
        reasons.append("broker_symbol_mismatch")
    if order.side.lower() != expected_order.side.lower():
        reasons.append("broker_side_mismatch")
    if order.notional is None or abs(order.notional - float(expected_order.notional or 0.0)) > 1e-9:
        reasons.append("broker_notional_mismatch")
    return reasons


def _matching_position(positions: tuple[PaperPosition, ...], symbol: str) -> PaperPosition | None:
    expected_symbol = symbol.upper()
    for position in positions:
        if position.symbol.upper() == expected_symbol and position.quantity > 0:
            return position
    return None


def _closeout_payload(
    *,
    status: str,
    session_dir: Path,
    output_dir: Path,
    confirm_paper: bool,
    execution_report: Path,
    execution: Mapping[str, object],
    package: Any,
    expected_order: PaperOrder,
    account: Any | None,
    positions: tuple[PaperPosition, ...],
    open_orders: tuple[PaperOrderSnapshot, ...],
    broker_order: PaperOrderSnapshot | None,
    reasons: list[str],
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
        "confirmations": {"confirm_paper": confirm_paper},
        "execution_report": {
            "path": str(execution_report),
            "status": execution.get("status"),
            "client_order_id": _mapping_or_empty(execution.get("order_sent")).get("client_order_id"),
        },
        "expected_order": _paper_order_intent_to_dict(expected_order),
        "execution_order": dict(_mapping_or_empty(execution.get("order_sent"))),
        "broker_order": _paper_order_snapshot_to_dict(broker_order) if broker_order is not None else None,
        "open_orders": [_paper_order_snapshot_to_dict(order_snapshot) for order_snapshot in open_orders],
        "positions": [_paper_position_to_dict(position) for position in positions],
        "account": _paper_account_to_dict(account) if account is not None else None,
        "reasons": list(reasons),
    }


def _write_closeout_artifacts(payload: Mapping[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paper_closeout.json"
    markdown_path = output_dir / "paper_closeout.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_paper_closeout_markdown(payload), encoding="utf-8")
    return json_path, markdown_path


def render_paper_closeout_markdown(payload: Mapping[str, object]) -> str:
    expected_order = _mapping_or_empty(payload.get("expected_order"))
    broker_order = _mapping_or_empty(payload.get("broker_order"))
    reasons = reason_codes(payload.get("reasons"))
    lines = [
        "# Paper Session Closeout",
        "",
        f"Status: **{payload.get('status') or ''}**",
        "",
        f"Mode: `{payload.get('mode') or ''}`",
        f"Broker: `{payload.get('broker') or ''}`",
        f"Symbol: `{expected_order.get('symbol') or ''}`",
        f"Side: `{expected_order.get('side') or ''}`",
        f"Notional: `{expected_order.get('notional') or ''}`",
        f"Client order ID: `{expected_order.get('client_order_id') or ''}`",
        f"Broker order status: `{broker_order.get('status') or ''}`",
        f"Filled quantity: `{broker_order.get('filled_quantity') or ''}`",
        f"Reasons: `{', '.join(reasons)}`",
        "",
    ]
    return "\n".join(lines)


def _order_identity(order: PaperOrder) -> _OrderIdentity:
    return {
        "client_order_id": order.client_order_id,
        "symbol": order.symbol.upper(),
        "side": order.side.lower(),
        "notional": float(order.notional) if order.notional is not None else None,
    }


def _order_identity_from_signal_report(signal_report: Mapping[str, object]) -> _OrderIdentity:
    order_intent = _mapping_or_empty(signal_report.get("order_intent"))
    notional = _optional_float(order_intent.get("notional"))
    return {
        "client_order_id": str(order_intent.get("client_order_id")) if order_intent.get("client_order_id") else None,
        "symbol": str(order_intent.get("symbol")).upper() if order_intent.get("symbol") else None,
        "side": str(order_intent.get("side")).lower() if order_intent.get("side") else None,
        "notional": notional,
    }


def _optional_float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
