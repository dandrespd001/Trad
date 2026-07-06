"""Offline Telegram control inbox for audited trading control intents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact
from trading_ai.execution.paper_risk_state import (
    DEFAULT_RISK_STATE_PATH,
    load_risk_state,
    reset_kill_switch,
    save_risk_state,
    trip_kill_switch,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/telegram_control/latest.json"
DEFAULT_APPLY_OUTPUT = "reports/tmp/telegram_control/apply.json"
DEFAULT_PLAN_OUTPUT = "reports/tmp/telegram_control/plan.json"
DEFAULT_DISPATCH_OUTPUT = "reports/tmp/telegram_control/dispatch.json"
SUPPORTED_ENVIRONMENTS = ("paper", "live")
ALLOWED_DISPATCH_GATES = frozenset({"paper_signal_arbitration", "paper_safe_flatten", "paper_auto_cycle"})


class TelegramControlOperationalError(RuntimeError):
    """Raised when Telegram control updates cannot be processed."""


@dataclass(frozen=True)
class TelegramControlResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class TelegramControlApplyResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class TelegramControlPlanResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class TelegramControlDispatchResult:
    exit_code: int
    status: str
    output_path: Path
    payload: dict[str, object]


def run_telegram_control_inbox(
    *,
    as_of_date: str,
    updates: str | Path,
    allowed_chat_ids: Iterable[str],
    allowed_user_ids: Iterable[str],
    output: str | Path = DEFAULT_OUTPUT,
    state: str | Path | None = None,
    ledger_output: str | Path | None = None,
    environment: str = "paper",
    generated_at: str | None = None,
) -> TelegramControlResult:
    if environment not in SUPPORTED_ENVIRONMENTS:
        raise TelegramControlOperationalError(f"unsupported environment: {environment}")
    allowed_chats = _clean_set(allowed_chat_ids)
    allowed_users = _clean_set(allowed_user_ids)
    if not allowed_chats:
        raise TelegramControlOperationalError("allowed_chat_ids is required")
    if not allowed_users:
        raise TelegramControlOperationalError("allowed_user_ids is required")

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    previous_state = _load_state(state)
    payload = _load_updates(updates)
    records = _update_records(payload)
    processed_ids = {int(value) for value in previous_state.get("processed_update_ids", []) if _int_or_none(value) is not None}
    last_update_id = _int_or_none(previous_state.get("last_update_id"))

    intents: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    max_seen = last_update_id if last_update_id is not None else -1
    seen_in_batch: set[int] = set()
    for record in records:
        update_id = _int_or_none(record.get("update_id"))
        if update_id is None:
            rejected.append(_rejected(record, reason_codes=["missing_update_id"]))
            continue
        max_seen = max(max_seen, update_id)
        message = _mapping(record.get("message") or record.get("edited_message"))
        chat_id = str(_mapping(message.get("chat")).get("id") or "")
        user_id = str(_mapping(message.get("from")).get("id") or "")
        text = str(message.get("text") or "").strip()
        reasons: list[str] = []
        if chat_id not in allowed_chats:
            reasons.append("unauthorized_chat")
        if user_id not in allowed_users:
            reasons.append("unauthorized_user")
        if update_id in processed_ids or update_id in seen_in_batch or (last_update_id is not None and update_id <= last_update_id):
            reasons.append("stale_or_duplicate_update")
        if not text:
            reasons.append("missing_text")
        if reasons:
            rejected.append(_rejected(record, reason_codes=reasons))
            seen_in_batch.add(update_id)
            processed_ids.add(update_id)
            continue
        intent, parse_reasons = _parse_intent(
            as_of_date=as_of_date,
            environment=environment,
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            generated_at=generated,
        )
        seen_in_batch.add(update_id)
        processed_ids.add(update_id)
        if parse_reasons:
            rejected.append(_rejected(record, reason_codes=parse_reasons))
            continue
        intents.append(intent)

    next_state = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": generated,
        "last_update_id": max_seen if max_seen >= 0 else None,
        "processed_update_ids": sorted(processed_ids)[-500:],
    }
    if state is not None:
        write_json_artifact(next_state, state)

    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "environment": environment,
        "status": "OK",
        "source": {"updates_path": str(Path(updates)), "state_path": str(Path(state)) if state is not None else None},
        "intents": intents,
        "rejected_updates": rejected,
        "next_state": next_state,
        "authority": _authority(),
        "safety": _safety(),
    }
    write_json_artifact(payload_out, output_path)
    if ledger_output is not None:
        _append_intent_ledger(ledger_output, intents)
    return TelegramControlResult(0, "OK", output_path, payload_out)


def run_telegram_control_apply(
    *,
    as_of_date: str,
    inbox: str | Path,
    output: str | Path = DEFAULT_APPLY_OUTPUT,
    state: str | Path | None = None,
    ledger_output: str | Path | None = None,
    risk_state_path: str | Path = DEFAULT_RISK_STATE_PATH,
    status_report: str | Path | None = None,
    history_report: str | Path | None = None,
    confirm_telegram_control: bool = False,
    generated_at: str | None = None,
) -> TelegramControlApplyResult:
    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    inbox_payload = _load_inbox(inbox)
    apply_state = _load_apply_state(state)
    applied_ids = {
        str(value)
        for value in apply_state.get("applied_intent_ids", [])
        if str(value).strip()
    }

    decisions: list[dict[str, object]] = []
    risk_changed = False
    blocked_by_inbox = _inbox_blockers(inbox_payload, as_of_date=as_of_date)
    for intent in _intent_records(inbox_payload):
        intent_id = str(intent.get("intent_id") or "").strip()
        if not intent_id:
            decisions.append(_decision(intent, decision="BLOCKED", action="REJECT_INTENT", reason_codes=["missing_intent_id"]))
            continue
        if intent_id in applied_ids:
            decisions.append(
                _decision(
                    intent,
                    decision="SKIPPED",
                    action="DUPLICATE_INTENT",
                    reason_codes=["intent_already_applied"],
                )
            )
            continue
        blockers = list(blocked_by_inbox)
        if str(intent.get("environment") or inbox_payload.get("environment") or "paper") != "paper":
            blockers.append("paper_only_apply_gate")
        if str(intent.get("as_of_date") or inbox_payload.get("as_of_date") or "") != as_of_date:
            blockers.append("intent_as_of_date_mismatch")
        if bool(intent.get("orders_submitted")) or bool(intent.get("broker_client_built")):
            blockers.append("unsafe_intent_execution_marker")
        if bool(intent.get("requires_confirmation")) and not confirm_telegram_control:
            blockers.append("confirmation_required")
        if blockers:
            decisions.append(
                _decision(
                    intent,
                    decision="BLOCKED",
                    action="REJECT_INTENT",
                    reason_codes=_dedupe(blockers),
                )
            )
            continue

        decision = _apply_confirmed_intent(
            intent,
            as_of_date=as_of_date,
            risk_state_path=risk_state_path,
            status_report=status_report,
            history_report=history_report,
        )
        decisions.append(decision)
        if decision.get("risk_changed"):
            risk_changed = True
        if decision.get("decision") in {"ACKNOWLEDGED", "APPLIED", "ROUTED"}:
            applied_ids.add(intent_id)

    blocked = any(decision.get("decision") == "BLOCKED" for decision in decisions)
    status = "BLOCKED" if blocked else "OK"
    next_state = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": generated,
        "applied_intent_ids": sorted(applied_ids)[-500:],
    }
    if state is not None:
        write_json_artifact(next_state, state)

    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "environment": "paper",
        "status": status,
        "source": {
            "inbox_path": str(Path(inbox)),
            "state_path": str(Path(state)) if state is not None else None,
            "risk_state_path": str(Path(risk_state_path)),
            "status_report_path": str(Path(status_report)) if status_report is not None else None,
            "history_report_path": str(Path(history_report)) if history_report is not None else None,
        },
        "decisions": decisions,
        "next_state": next_state,
        "authority": _authority(risk_changed=risk_changed),
        "safety": _safety(),
    }
    write_json_artifact(payload_out, output_path)
    if ledger_output is not None:
        _append_apply_ledger(ledger_output, decisions)
    return TelegramControlApplyResult(1 if blocked else 0, status, output_path, payload_out)


def run_telegram_control_plan(
    *,
    as_of_date: str,
    apply_report: str | Path,
    output: str | Path = DEFAULT_PLAN_OUTPUT,
    ledger_output: str | Path | None = None,
    generated_at: str | None = None,
) -> TelegramControlPlanResult:
    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    apply_payload = _load_apply_report(apply_report)
    blockers = _apply_report_plan_blockers(apply_payload, as_of_date=as_of_date)
    candidate_steps: list[dict[str, object]] = []
    skipped_decisions: list[dict[str, object]] = []

    for index, decision in enumerate(_decision_records(apply_payload), start=1):
        decision_state = str(decision.get("decision") or "").upper()
        intent_id = str(decision.get("intent_id") or "")
        if decision_state != "ROUTED":
            skipped_decisions.append(
                {
                    "intent_id": intent_id,
                    "decision": decision_state or "UNKNOWN",
                    "reason": "not_routed",
                }
            )
            continue
        route = _mapping(decision.get("route"))
        route_blockers = _route_plan_blockers(route, intent_id=intent_id)
        blockers.extend(route_blockers)
        if route_blockers:
            continue
        candidate_steps.append(_route_plan_step(index=index, decision=decision, route=route))

    blockers = _dedupe(blockers)
    steps = [] if blockers else candidate_steps
    status = "BLOCKED" if blockers else "OK"
    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "environment": "paper",
        "status": status,
        "source": {
            "apply_path": str(Path(apply_report)),
            "apply_status": str(apply_payload.get("status") or "UNKNOWN"),
            "apply_as_of_date": apply_payload.get("as_of_date"),
        },
        "summary": {
            "route_count": len(steps),
            "skipped_decision_count": len(skipped_decisions),
            "blocker_count": len(blockers),
        },
        "steps": steps,
        "skipped_decisions": skipped_decisions,
        "blockers": blockers,
        "authority": _authority(),
        "safety": _safety(),
    }
    write_json_artifact(payload_out, output_path)
    if ledger_output is not None and steps:
        _append_plan_ledger(ledger_output, steps)
    return TelegramControlPlanResult(1 if blockers else 0, status, output_path, payload_out)


def run_telegram_control_dispatch(
    *,
    as_of_date: str,
    plan: str | Path,
    output: str | Path = DEFAULT_DISPATCH_OUTPUT,
    ledger_output: str | Path | None = None,
    dry_run: bool = True,
    generated_at: str | None = None,
) -> TelegramControlDispatchResult:
    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    plan_payload = _load_plan_report(plan)
    plan_blockers = _plan_dispatch_blockers(plan_payload, as_of_date=as_of_date)
    decisions = [
        _dispatch_step_decision(step, plan_blockers=plan_blockers, dry_run=dry_run)
        for step in _step_records(plan_payload)
    ]
    blockers = _dedupe([*plan_blockers, *(code for decision in decisions for code in _reason_list(decision.get("reason_codes")))])
    blocked_count = sum(1 for decision in decisions if decision.get("status") == "BLOCKED")
    ready_count = sum(1 for decision in decisions if decision.get("status") == "READY_FOR_OPERATOR")
    status = "BLOCKED" if blockers else "OK"
    safety = {**_safety(), "subprocess_started": False}
    payload_out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of_date": as_of_date,
        "environment": "paper",
        "status": status,
        "source": {
            "plan_path": str(Path(plan)),
            "plan_status": str(plan_payload.get("status") or "UNKNOWN"),
            "plan_as_of_date": plan_payload.get("as_of_date"),
        },
        "summary": {
            "decision_count": len(decisions),
            "ready_count": ready_count,
            "blocked_count": blocked_count,
            "blocker_count": len(blockers),
        },
        "decisions": decisions,
        "blockers": blockers,
        "authority": _authority(),
        "safety": safety,
    }
    write_json_artifact(payload_out, output_path)
    if ledger_output is not None and decisions:
        _append_dispatch_ledger(ledger_output, decisions)
    return TelegramControlDispatchResult(1 if blockers else 0, status, output_path, payload_out)


def _load_inbox(path: str | Path) -> dict[str, object]:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram control inbox: {exc}") from exc


def _load_apply_report(path: str | Path) -> dict[str, object]:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram control apply report: {exc}") from exc


def _load_plan_report(path: str | Path) -> dict[str, object]:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram control plan: {exc}") from exc


def _load_apply_state(path: str | Path | None) -> dict[str, object]:
    if path is None or not Path(path).exists():
        return {}
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram control apply state: {exc}") from exc


def _intent_records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("intents")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TelegramControlOperationalError("telegram control inbox intents must be a JSON array")
    return [row for row in raw if isinstance(row, Mapping)]


def _decision_records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("decisions")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TelegramControlOperationalError("telegram control apply decisions must be a JSON array")
    return [row for row in raw if isinstance(row, Mapping)]


def _step_records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = payload.get("steps")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TelegramControlOperationalError("telegram control plan steps must be a JSON array")
    return [row for row in raw if isinstance(row, Mapping)]


def _inbox_blockers(payload: Mapping[str, object], *, as_of_date: str) -> list[str]:
    blockers: list[str] = []
    if str(payload.get("environment") or "paper") != "paper":
        blockers.append("paper_only_apply_gate")
    if str(payload.get("as_of_date") or "") != as_of_date:
        blockers.append("inbox_as_of_date_mismatch")
    safety = _mapping(payload.get("safety"))
    authority = _mapping(payload.get("authority"))
    if bool(safety.get("orders_submitted")) or bool(authority.get("orders_submitted")):
        blockers.append("unsafe_inbox_orders_marker")
    if bool(safety.get("broker_client_built")):
        blockers.append("unsafe_inbox_broker_marker")
    return _dedupe(blockers)


def _apply_report_plan_blockers(payload: Mapping[str, object], *, as_of_date: str) -> list[str]:
    blockers: list[str] = []
    if str(payload.get("environment") or "paper") != "paper":
        blockers.append("paper_only_plan_gate")
    if str(payload.get("as_of_date") or "") != as_of_date:
        blockers.append("apply_as_of_date_mismatch")
    if str(payload.get("status") or "").upper() not in {"OK", ""}:
        blockers.append(f"apply_status_{str(payload.get('status') or 'unknown').lower()}")
    safety = _mapping(payload.get("safety"))
    authority = _mapping(payload.get("authority"))
    if safety.get("paper_only") is False:
        blockers.append("apply_not_paper_only")
    if safety.get("broker_client_built") is True:
        blockers.append("apply_broker_client_built")
    if safety.get("orders_submitted") is True or authority.get("orders_submitted") is True:
        blockers.append("apply_orders_submitted")
    if (
        safety.get("live_trading_authorized") is True
        or safety.get("live_trading_allowed") is True
        or safety.get("live_execution_enabled") is True
        or authority.get("live_trading_authorized") is True
    ):
        blockers.append("apply_live_trading_flag")
    return blockers


def _route_plan_blockers(route: Mapping[str, object], *, intent_id: str) -> list[str]:
    suffix = f":{intent_id}" if intent_id else ""
    blockers: list[str] = []
    if not route:
        return [f"route_missing{suffix}"]
    if str(route.get("command") or "").strip() == "":
        blockers.append(f"route_command_missing{suffix}")
    args = route.get("args")
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        blockers.append(f"route_args_invalid{suffix}")
    if route.get("paper_only") is not True:
        blockers.append(f"route_not_paper_only{suffix}")
    if route.get("broker_client_built") is True:
        blockers.append(f"route_broker_client_built{suffix}")
    if route.get("orders_submitted") is True:
        blockers.append(f"route_orders_submitted{suffix}")
    if route.get("eligible_for_auto_execution") is True:
        blockers.append(f"route_auto_execution_enabled{suffix}")
    return blockers


def _route_plan_step(
    *,
    index: int,
    decision: Mapping[str, object],
    route: Mapping[str, object],
) -> dict[str, object]:
    command = str(route.get("command") or "")
    args = [str(value) for value in route.get("args", []) if isinstance(value, str)]
    symbol = str(route.get("symbol") or decision.get("symbol") or "").upper() or None
    side = str(route.get("side") or decision.get("side") or "").lower() or None
    notional = _float_or_none(route.get("notional") if route.get("notional") is not None else decision.get("notional"))
    return {
        "step_id": f"telegram-control-route-{index:03d}",
        "status": "PENDING_OPERATOR_GATE",
        "intent_id": str(decision.get("intent_id") or route.get("intent_id") or ""),
        "intent_type": str(decision.get("intent_type") or ""),
        "gate": str(route.get("gate") or ""),
        "command": command,
        "args": args,
        "argv": [command, *args],
        "suggested_next_command": str(decision.get("suggested_next_command") or ""),
        "symbol": symbol,
        "side": side,
        "notional": notional,
        "requires_operator_confirmation": route.get("requires_operator_confirmation") is True,
        "paper_only": True,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }


def _plan_dispatch_blockers(payload: Mapping[str, object], *, as_of_date: str) -> list[str]:
    blockers: list[str] = []
    if str(payload.get("environment") or "paper") != "paper":
        blockers.append("paper_only_dispatch_gate")
    if str(payload.get("as_of_date") or "") != as_of_date:
        blockers.append("plan_as_of_date_mismatch")
    if str(payload.get("status") or "").upper() not in {"OK", ""}:
        blockers.append(f"plan_status_{str(payload.get('status') or 'unknown').lower()}")
    blockers.extend(f"plan_{code}" for code in _reason_list(payload.get("blockers")))
    safety = _mapping(payload.get("safety"))
    authority = _mapping(payload.get("authority"))
    if safety.get("paper_only") is False:
        blockers.append("plan_not_paper_only")
    if safety.get("broker_client_built") is True:
        blockers.append("plan_broker_client_built")
    if safety.get("orders_submitted") is True or authority.get("orders_submitted") is True:
        blockers.append("plan_orders_submitted")
    if (
        safety.get("live_trading_authorized") is True
        or safety.get("live_trading_allowed") is True
        or safety.get("live_execution_enabled") is True
        or authority.get("live_trading_authorized") is True
    ):
        blockers.append("plan_live_trading_flag")
    return blockers


def _dispatch_step_decision(
    step: Mapping[str, object],
    *,
    plan_blockers: list[str],
    dry_run: bool,
) -> dict[str, object]:
    reason_codes = [*_step_dispatch_blockers(step), *plan_blockers]
    status = "BLOCKED" if reason_codes else "READY_FOR_OPERATOR"
    argv = _argv_from_step(step)
    return {
        "step_id": str(step.get("step_id") or ""),
        "intent_id": str(step.get("intent_id") or ""),
        "intent_type": str(step.get("intent_type") or ""),
        "gate": str(step.get("gate") or ""),
        "status": status,
        "reason_codes": _dedupe(reason_codes),
        "command": str(step.get("command") or ""),
        "args": [str(value) for value in step.get("args", []) if isinstance(value, str)],
        "argv": argv,
        "suggested_next_command": str(step.get("suggested_next_command") or ""),
        "symbol": str(step.get("symbol") or "").upper() or None,
        "side": str(step.get("side") or "").lower() or None,
        "notional": _float_or_none(step.get("notional")),
        "dry_run": dry_run,
        "subprocess_started": False,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }


def _step_dispatch_blockers(step: Mapping[str, object]) -> list[str]:
    blockers: list[str] = []
    gate = str(step.get("gate") or "")
    if gate not in ALLOWED_DISPATCH_GATES:
        blockers.append("gate_not_allowed")
    if str(step.get("status") or "") != "PENDING_OPERATOR_GATE":
        blockers.append("step_not_pending_operator_gate")
    if step.get("paper_only") is not True:
        blockers.append("step_not_paper_only")
    if step.get("broker_client_built") is True:
        blockers.append("step_broker_client_built")
    if step.get("orders_submitted") is True:
        blockers.append("step_orders_submitted")
    if step.get("eligible_for_auto_execution") is True:
        blockers.append("step_auto_execution_enabled")
    if not str(step.get("command") or "").strip():
        blockers.append("step_command_missing")
    args = step.get("args")
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        blockers.append("step_args_invalid")
    return blockers


def _argv_from_step(step: Mapping[str, object]) -> list[str]:
    command = str(step.get("command") or "")
    args = [str(value) for value in step.get("args", []) if isinstance(value, str)]
    argv = step.get("argv")
    if isinstance(argv, list) and all(isinstance(value, str) for value in argv):
        return [str(value) for value in argv]
    return [command, *args]


def _apply_confirmed_intent(
    intent: Mapping[str, object],
    *,
    as_of_date: str,
    risk_state_path: str | Path,
    status_report: str | Path | None = None,
    history_report: str | Path | None = None,
) -> dict[str, object]:
    intent_type = str(intent.get("intent_type") or "").upper()
    if intent_type == "STATUS_REQUESTED":
        return _telegram_response_decision(
            intent,
            as_of_date=as_of_date,
            report_path=status_report,
            response_kind="status",
        )
    if intent_type == "HISTORY_REQUESTED":
        return _telegram_response_decision(
            intent,
            as_of_date=as_of_date,
            report_path=history_report,
            response_kind="history",
        )
    if intent_type == "PAUSE_REQUESTED":
        state = load_risk_state(risk_state_path)
        reason = str(intent.get("reason") or "operator requested control action")
        save_risk_state(trip_kill_switch(state, reason=f"telegram_pause:{reason}"), risk_state_path)
        return _decision(intent, decision="APPLIED", action="KILL_SWITCH_TRIPPED", risk_changed=True)
    if intent_type == "RESUME_REQUESTED":
        state = load_risk_state(risk_state_path)
        save_risk_state(reset_kill_switch(state), risk_state_path)
        return _decision(intent, decision="APPLIED", action="KILL_SWITCH_RESET", risk_changed=True)
    if intent_type in {"OPEN_SIGNAL_REQUESTED", "SIGNAL_REQUESTED"}:
        return _decision(
            intent,
            decision="ROUTED",
            action="ROUTE_TO_SIGNAL_GATE",
            suggested_next_command=_signal_gate_command(intent, as_of_date=as_of_date),
            route=_signal_gate_route(intent, as_of_date=as_of_date),
        )
    if intent_type == "FLATTEN_REQUESTED":
        return _decision(
            intent,
            decision="ROUTED",
            action="ROUTE_TO_FLATTEN_GATE",
            suggested_next_command=_flatten_gate_command(intent, as_of_date=as_of_date),
            route=_flatten_gate_route(intent, as_of_date=as_of_date),
        )
    if intent_type == "RESTART_REQUESTED":
        return _decision(
            intent,
            decision="ROUTED",
            action="ROUTE_TO_OPERATOR_RESTART",
            suggested_next_command=f"trading-ai paper-auto-cycle --as-of-date {as_of_date} --confirm-paper-auto",
            route=_restart_gate_route(intent, as_of_date=as_of_date),
        )
    return _decision(intent, decision="BLOCKED", action="REJECT_INTENT", reason_codes=["unsupported_intent_type"])


def _decision(
    intent: Mapping[str, object],
    *,
    decision: str,
    action: str,
    reason_codes: list[str] | None = None,
    suggested_next_command: str | None = None,
    telegram_response: Mapping[str, object] | None = None,
    route: Mapping[str, object] | None = None,
    risk_changed: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent_id": str(intent.get("intent_id") or ""),
        "intent_type": str(intent.get("intent_type") or ""),
        "decision": decision,
        "action": action,
        "reason_codes": reason_codes or [],
        "requires_confirmation": bool(intent.get("requires_confirmation")),
        "symbol": str(intent.get("symbol") or "").upper() or None,
        "side": str(intent.get("side") or "").lower() or None,
        "notional": _float_or_none(intent.get("notional")),
        "risk_changed": risk_changed,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }
    if suggested_next_command:
        payload["suggested_next_command"] = suggested_next_command
    if telegram_response is not None:
        payload["telegram_response"] = dict(telegram_response)
    if route is not None:
        payload["route"] = dict(route)
    return payload


def _telegram_response_decision(
    intent: Mapping[str, object],
    *,
    as_of_date: str,
    report_path: str | Path | None,
    response_kind: str,
) -> dict[str, object]:
    if report_path is None:
        command = (
            "trading-ai paper-telegram-status"
            if response_kind == "status"
            else "trading-ai paper-telegram-history"
        )
        return _decision(
            intent,
            decision="ACKNOWLEDGED",
            action=f"{response_kind.upper()}_RESPONSE",
            reason_codes=[f"{response_kind}_report_missing"],
            suggested_next_command=f"{command} --as-of-date {as_of_date}",
        )
    response, blockers = _load_local_telegram_response(
        report_path,
        as_of_date=as_of_date,
        response_kind=response_kind,
    )
    if blockers:
        return _decision(
            intent,
            decision="BLOCKED",
            action="REJECT_INTENT",
            reason_codes=blockers,
        )
    return _decision(
        intent,
        decision="ACKNOWLEDGED",
        action=f"{response_kind.upper()}_RESPONSE",
        telegram_response=response,
    )


def _load_local_telegram_response(
    report_path: str | Path,
    *,
    as_of_date: str,
    response_kind: str,
) -> tuple[dict[str, object] | None, list[str]]:
    try:
        payload = read_json_artifact(report_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None, [f"{response_kind}_report_invalid"]
    blockers: list[str] = []
    if str(payload.get("as_of_date") or "") not in {"", as_of_date}:
        blockers.append(f"{response_kind}_report_stale")
    message = str(payload.get("message") or "").strip()
    if not message:
        blockers.append(f"{response_kind}_message_missing")
    safety = _mapping(payload.get("safety"))
    if safety.get("live_trading_authorized") is True or safety.get("live_trading_allowed") is True:
        blockers.append(f"{response_kind}_live_trading_flag")
    if safety.get("broker_client_built") is True:
        blockers.append(f"{response_kind}_broker_client_built")
    if safety.get("credentials_read") is True:
        blockers.append(f"{response_kind}_credentials_read")
    if safety.get("orders_submitted") is True:
        blockers.append(f"{response_kind}_orders_submitted")
    telegram = _mapping(payload.get("telegram"))
    return (
        {
            "kind": response_kind,
            "source_path": str(Path(report_path)),
            "status": str(payload.get("status") or "UNKNOWN"),
            "message": message,
            "message_length": len(message),
            "parse_mode": str(telegram.get("parse_mode") or "plain_text"),
            "sent": telegram.get("sent") is True,
        },
        _dedupe(blockers),
    )


def _signal_gate_command(intent: Mapping[str, object], *, as_of_date: str) -> str:
    symbol = str(intent.get("symbol") or "<symbol>").upper()
    side = str(intent.get("side") or "<side>").lower()
    return (
        "trading-ai paper-signal-arbitration "
        f"--as-of-date {as_of_date} "
        "--model-signals reports/tmp/paper/model_signals.json "
        "--llm-proposals reports/tmp/llm_signal_proposals/latest.json "
        "--readiness reports/tmp/paper/readiness.json "
        f"# telegram_intent symbol={symbol} side={side}"
    )


def _flatten_gate_command(intent: Mapping[str, object], *, as_of_date: str) -> str:
    symbol = str(intent.get("symbol") or "<symbol>").upper()
    return (
        "trading-ai paper-safe-flatten "
        f"--as-of-date {as_of_date} "
        "--confirm-paper --confirm-flatten "
        f"# telegram_intent symbol={symbol}"
    )


def _signal_gate_route(intent: Mapping[str, object], *, as_of_date: str) -> dict[str, object]:
    route = _base_route(
        intent,
        gate="paper_signal_arbitration",
        command="trading-ai paper-signal-arbitration",
        args=[
            "--as-of-date",
            as_of_date,
            "--model-signals",
            "reports/tmp/paper/model_signals.json",
            "--llm-proposals",
            "reports/tmp/llm_signal_proposals/latest.json",
            "--readiness",
            "reports/tmp/paper/readiness.json",
        ],
    )
    route["symbol"] = str(intent.get("symbol") or "").upper() or None
    route["side"] = str(intent.get("side") or "").lower() or None
    route["notional"] = _float_or_none(intent.get("notional"))
    return route


def _flatten_gate_route(intent: Mapping[str, object], *, as_of_date: str) -> dict[str, object]:
    route = _base_route(
        intent,
        gate="paper_safe_flatten",
        command="trading-ai paper-safe-flatten",
        args=["--as-of-date", as_of_date, "--confirm-paper", "--confirm-flatten"],
    )
    route["symbol"] = str(intent.get("symbol") or "").upper() or None
    return route


def _restart_gate_route(intent: Mapping[str, object], *, as_of_date: str) -> dict[str, object]:
    return _base_route(
        intent,
        gate="paper_auto_cycle",
        command="trading-ai paper-auto-cycle",
        args=["--as-of-date", as_of_date, "--confirm-paper-auto"],
    )


def _base_route(
    intent: Mapping[str, object],
    *,
    gate: str,
    command: str,
    args: list[str],
) -> dict[str, object]:
    return {
        "gate": gate,
        "command": command,
        "args": args,
        "intent_id": str(intent.get("intent_id") or ""),
        "requires_operator_confirmation": bool(intent.get("requires_confirmation")),
        "paper_only": True,
        "broker_client_built": False,
        "orders_submitted": False,
        "eligible_for_auto_execution": False,
    }


def _load_updates(path: str | Path) -> object:
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram updates: {exc}") from exc


def _update_records(payload: object) -> list[Mapping[str, object]]:
    raw = _mapping(payload).get("result") if isinstance(payload, Mapping) else payload
    if not isinstance(raw, list):
        raise TelegramControlOperationalError("telegram updates must be a JSON array or Bot API response object")
    return [row for row in raw if isinstance(row, Mapping)]


def _load_state(path: str | Path | None) -> dict[str, object]:
    if path is None or not Path(path).exists():
        return {}
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TelegramControlOperationalError(f"cannot read telegram control state: {exc}") from exc


def _parse_intent(
    *,
    as_of_date: str,
    environment: str,
    update_id: int,
    chat_id: str,
    user_id: str,
    text: str,
    generated_at: str,
) -> tuple[dict[str, object], list[str]]:
    parts = text.split()
    if not parts or not parts[0].startswith("/"):
        return {}, ["unsupported_command"]
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]
    base = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "environment": environment,
        "source": "telegram",
        "source_update_id": update_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "command": command,
        "eligible_for_auto_execution": False,
        "broker_client_built": False,
        "orders_submitted": False,
    }
    if command == "/status":
        return {
            **base,
            "intent_id": _intent_id(environment, as_of_date, update_id, text),
            "intent_type": "STATUS_REQUESTED",
            "requires_confirmation": False,
            "reason": "status request",
        }, []
    if command == "/history":
        return {
            **base,
            "intent_id": _intent_id(environment, as_of_date, update_id, text),
            "intent_type": "HISTORY_REQUESTED",
            "requires_confirmation": False,
            "reason": "history request",
        }, []
    if command == "/pause":
        return _control_intent(base, "PAUSE_REQUESTED", args=args, text=text, as_of_date=as_of_date, update_id=update_id), []
    if command == "/resume":
        return _control_intent(base, "RESUME_REQUESTED", args=args, text=text, as_of_date=as_of_date, update_id=update_id), []
    if command == "/restart":
        return _control_intent(base, "RESTART_REQUESTED", args=args, text=text, as_of_date=as_of_date, update_id=update_id), []
    if command in {"/close", "/flatten"}:
        if not args:
            return {}, ["malformed_command"]
        return {
            **_control_intent(base, "FLATTEN_REQUESTED", args=args[1:], text=text, as_of_date=as_of_date, update_id=update_id),
            "symbol": args[0].upper(),
        }, []
    if command == "/open":
        if len(args) < 3:
            return {}, ["malformed_command"]
        side = args[1].lower()
        if side not in {"buy", "sell"}:
            return {}, ["invalid_side"]
        notional = _float_or_none(args[2])
        if notional is None or notional <= 0:
            return {}, ["invalid_notional"]
        return {
            **_control_intent(base, "OPEN_SIGNAL_REQUESTED", args=args[3:], text=text, as_of_date=as_of_date, update_id=update_id),
            "symbol": args[0].upper(),
            "side": side,
            "notional": notional,
        }, []
    if command == "/signal":
        if len(args) < 2:
            return {}, ["malformed_command"]
        side = args[1].lower()
        if side not in {"buy", "sell", "hold", "close"}:
            return {}, ["invalid_side"]
        return {
            **_control_intent(base, "SIGNAL_REQUESTED", args=args[2:], text=text, as_of_date=as_of_date, update_id=update_id),
            "symbol": args[0].upper(),
            "side": side,
        }, []
    return {}, ["unsupported_command"]


def _control_intent(
    base: Mapping[str, object],
    intent_type: str,
    *,
    args: list[str],
    text: str,
    as_of_date: str,
    update_id: int,
) -> dict[str, object]:
    environment = str(base.get("environment") or "paper")
    return {
        **dict(base),
        "intent_id": _intent_id(environment, as_of_date, update_id, text),
        "intent_type": intent_type,
        "requires_confirmation": True,
        "reason": " ".join(args).strip() or "operator requested control action",
    }


def _rejected(record: Mapping[str, object], *, reason_codes: list[str]) -> dict[str, object]:
    message = _mapping(record.get("message") or record.get("edited_message"))
    return {
        "update_id": _int_or_none(record.get("update_id")),
        "chat_id": str(_mapping(message.get("chat")).get("id") or ""),
        "user_id": str(_mapping(message.get("from")).get("id") or ""),
        "reason_codes": _dedupe(reason_codes),
    }


def _append_intent_ledger(path: str | Path, intents: list[Mapping[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for intent in intents:
            handle.write(json.dumps({"record_type": "telegram_control_intent", **dict(intent)}, sort_keys=True) + "\n")


def _append_apply_ledger(path: str | Path, decisions: list[Mapping[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(
                json.dumps({"record_type": "telegram_control_apply_decision", **dict(decision)}, sort_keys=True)
                + "\n"
            )


def _append_plan_ledger(path: str | Path, steps: list[Mapping[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for step in steps:
            handle.write(json.dumps({"record_type": "telegram_control_plan_step", **dict(step)}, sort_keys=True) + "\n")


def _append_dispatch_ledger(path: str | Path, decisions: list[Mapping[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(
                json.dumps({"record_type": "telegram_control_dispatch_decision", **dict(decision)}, sort_keys=True)
                + "\n"
            )


def _intent_id(environment: str, as_of_date: str, update_id: int, text: str) -> str:
    material = f"{environment}|{as_of_date}|{update_id}|{text.strip()}"
    return "tgctl-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _clean_set(values: Iterable[str]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _reason_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _authority(*, risk_changed: bool = False) -> dict[str, object]:
    return {
        "control_plane": "telegram_intents_only",
        "llm_authority": "none",
        "orders_submitted": False,
        "risk_changed": risk_changed,
        "live_trading_authorized": False,
    }


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_execution_enabled": False,
        "live_trading_allowed": False,
    }
