"""Credential-free high-level client for the Alpaca paper executor socket."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading_ai.execution.alpaca_paper import (
    PaperAccount,
    PaperFillActivity,
    PaperOrder,
    PaperOrderResult,
    PaperOrderSnapshot,
    PaperPosition,
    ReconciliationReport,
)
from trading_ai.execution.paper_executor_ipc import (
    ExecutorTarget,
    PaperExecutorClient,
    PaperExecutorProtocolError,
)
from trading_ai.execution.paper_executor_service import deterministic_command_id


@dataclass(frozen=True)
class PaperExecutorMutationReceipt:
    """Correlates one completed executor mutation with its fenced target."""

    request_id: str
    operation: str
    target: ExecutorTarget
    outcome: str
    result: PaperOrderResult


@dataclass(frozen=True)
class PaperSafeFlattenOutcomeUnknown:
    request_id: str
    operation: str
    phase: str
    retry_allowed: bool


@dataclass(frozen=True)
class PaperSafeFlattenStatus:
    schema_version: int
    operation_id: str
    account_scope_sha256: str
    state: str
    state_version: int
    terminal: bool
    reconciled: bool
    kill_switch_active: bool
    retry_allowed: bool
    failure_code: str | None
    outcome_unknown: PaperSafeFlattenOutcomeUnknown | None
    started_at: str
    updated_at: str


@dataclass(frozen=True)
class PaperSafeFlattenStartReceipt:
    request_id: str
    operation_id: str
    target: ExecutorTarget
    status: PaperSafeFlattenStatus


class PaperExecutorBrokerClient:
    """Paper-broker facade that exposes no SDK, credentials, or arbitrary HTTP."""

    def __init__(self, transport: PaperExecutorClient | None = None) -> None:
        self._transport = transport or PaperExecutorClient()
        self._expected_target: ExecutorTarget | None = None

    def health(self) -> dict[str, object]:
        payload = self._transport.request("health")
        _exact(payload, _HEALTH_FIELDS, label="executor health")
        status = _text(payload["status"], label="status")
        mutations_allowed = _boolean(
            payload["mutations_allowed"],
            label="mutations_allowed",
        )
        opening_orders_allowed = _boolean(
            payload["opening_orders_allowed"],
            label="opening_orders_allowed",
        )
        capability_mode = _text(payload["capability_mode"], label="capability_mode")
        account_scope = _hex_digest(
            payload["account_scope_sha256"],
            label="account_scope_sha256",
        )
        policy = _hex_digest(payload["policy_sha256"], label="policy_sha256")
        authz_policy = _hex_digest(
            payload["authz_policy_sha256"],
            label="authz_policy_sha256",
        )
        run_id = _canonical_request_id(payload["run_id"], label="run_id")
        fence_epoch = _positive_integer(payload["fence_epoch"], label="fence_epoch")
        pending_recovery = _nonnegative_integer(
            payload["pending_recovery"],
            label="pending_recovery",
        )
        kill_switch_active = _boolean(
            payload["kill_switch_active"],
            label="kill_switch_active",
        )
        if status not in {
            "starting",
            "recovering",
            "ready",
            "flattening",
            "blocked",
            "draining",
            "stopped",
            "crashed",
            "failed",
        }:
            raise PaperExecutorProtocolError("executor health status is unknown")
        if mutations_allowed != (status == "ready" and pending_recovery == 0):
            raise PaperExecutorProtocolError("executor health mutation state is inconsistent")
        expected_mode = "full" if opening_orders_allowed else "reduce_only" if mutations_allowed else "blocked"
        if capability_mode != expected_mode or (opening_orders_allowed and not mutations_allowed):
            raise PaperExecutorProtocolError("executor health capability mode is inconsistent")
        if opening_orders_allowed and kill_switch_active:
            raise PaperExecutorProtocolError("executor health permits opening while the kill switch is active")
        if status == "flattening" and not kill_switch_active:
            raise PaperExecutorProtocolError("executor flattening health lacks its kill switch")
        return {
            "status": status,
            "mutations_allowed": mutations_allowed,
            "opening_orders_allowed": opening_orders_allowed,
            "capability_mode": capability_mode,
            "account_scope_sha256": account_scope,
            "fence_epoch": fence_epoch,
            "policy_sha256": policy,
            "authz_policy_sha256": authz_policy,
            "run_id": run_id,
            "pending_recovery": pending_recovery,
            "kill_switch_active": kill_switch_active,
        }

    def pin_target(self, health: Mapping[str, object]) -> ExecutorTarget:
        """Bind every later mutation to one previously validated health target.

        Reads remain observational, but a daemon restart, policy replacement,
        authorization-policy replacement, account change, or fence advance
        between preflight and dispatch makes the next mutation fail before its
        request frame is sent.
        """

        target = ExecutorTarget(
            account_scope_sha256=_hex_digest(
                health.get("account_scope_sha256"),
                label="account_scope_sha256",
            ),
            policy_sha256=_hex_digest(
                health.get("policy_sha256"),
                label="policy_sha256",
            ),
            authz_policy_sha256=_hex_digest(
                health.get("authz_policy_sha256"),
                label="authz_policy_sha256",
            ),
            run_id=_canonical_request_id(health.get("run_id"), label="run_id"),
            fence_epoch=_positive_integer(
                health.get("fence_epoch"),
                label="fence_epoch",
            ),
        )
        if self._expected_target is not None and self._expected_target != target:
            raise PaperExecutorProtocolError("executor target is already pinned")
        self._expected_target = target
        return target

    def read_account(self) -> PaperAccount:
        payload = self._transport.request("read_account")
        raw = _object_field(payload, "account")
        _exact(raw, _ACCOUNT_FIELDS, label="account")
        return PaperAccount(
            account_id=_text(raw["account_id"], label="account_id"),
            status=_text(raw["status"], label="status"),
            cash=_finite(raw["cash"], label="cash"),
            equity=_finite(raw["equity"], label="equity"),
            buying_power=_finite(raw["buying_power"], label="buying_power"),
            last_equity=_finite(raw["last_equity"], label="last_equity"),
        )

    def read_positions(self) -> tuple[PaperPosition, ...]:
        payload = self._transport.request("read_positions")
        raw_positions = _list_field(payload, "positions")
        return tuple(_position(item) for item in raw_positions)

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        payload = self._transport.request("list_orders", {"status": status})
        return tuple(_order(item) for item in _list_field(payload, "orders"))

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        payload = self._transport.request(
            "get_order",
            {"order_id": order_id, "client_order_id": None},
        )
        order = _order(_object_field(payload, "order"))
        if order.order_id != order_id:
            raise PaperExecutorProtocolError("executor returned a different order_id")
        return order

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        payload = self._transport.request(
            "get_order",
            {"order_id": None, "client_order_id": client_order_id},
        )
        order = _order(_object_field(payload, "order"))
        if order.client_order_id != client_order_id:
            raise PaperExecutorProtocolError("executor returned a different client_order_id")
        return order

    def list_fill_activities(
        self,
        *,
        after: datetime,
        until: datetime,
    ) -> tuple[PaperFillActivity, ...]:
        payload = self._transport.request(
            "list_fill_activities",
            {"after": after.isoformat(), "until": until.isoformat()},
        )
        return tuple(_fill(item) for item in _list_field(payload, "fills"))

    def latest_trade_price(self, symbol: str) -> float | None:
        payload = self._transport.request("latest_trade_price", {"symbol": symbol})
        _exact(payload, frozenset({"symbol", "price"}), label="latest trade")
        expected_symbol = _text(symbol, label="symbol").upper()
        if _text(payload["symbol"], label="symbol") != expected_symbol:
            raise PaperExecutorProtocolError("executor returned a different trade symbol")
        price = payload["price"]
        return None if price is None else _finite(price, label="price")

    def submit_order(self, order: PaperOrder) -> PaperOrderResult:
        return self.submit_order_with_receipt(order).result

    def submit_order_with_receipt(
        self,
        order: PaperOrder,
    ) -> PaperExecutorMutationReceipt:
        request_id = deterministic_command_id(
            "submit_order",
            f"client_order_id:{order.client_order_id}",
        )
        target = self._mutation_target()
        payload = {
            "order": {
                "symbol": order.symbol,
                "side": order.side,
                "client_order_id": order.client_order_id,
                "quantity": order.quantity,
                "notional": order.notional,
                "reference_price": order.reference_price,
                "order_type": order.order_type,
                "limit_price": order.limit_price,
                "position_intent": order.position_intent,
            }
        }
        result = self._transport.request(
            "submit_order",
            payload,
            request_id=request_id,
            target=target,
        )
        order_result = _order_result(result)
        return PaperExecutorMutationReceipt(
            request_id=request_id,
            operation="submit_order",
            target=target,
            outcome="completed" if order_result.accepted else "rejected",
            result=order_result,
        )

    def cancel_order(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
    ) -> PaperOrderResult:
        return self.cancel_order_with_receipt(
            client_order_id,
            order_id=order_id,
        ).result

    def cancel_order_with_receipt(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
    ) -> PaperExecutorMutationReceipt:
        if (client_order_id is None) == (order_id is None):
            raise ValueError("exactly one order identifier is required")
        business_key = f"order_id:{order_id}" if order_id is not None else f"client_order_id:{client_order_id}"
        request_id = deterministic_command_id("cancel_order", business_key)
        target = self._mutation_target()
        result = self._transport.request(
            "cancel_order",
            {"order_id": order_id, "client_order_id": client_order_id},
            request_id=request_id,
            target=target,
        )
        order_result = _order_result(result)
        return PaperExecutorMutationReceipt(
            request_id=request_id,
            operation="cancel_order",
            target=target,
            outcome="completed" if order_result.accepted else "rejected",
            result=order_result,
        )

    def activate_kill_switch(self, reason: str) -> None:
        result = self._transport.request(
            "latch_kill_switch",
            {"reason_code": reason},
            request_id=deterministic_command_id(
                "latch_kill_switch",
                f"reason_code:{reason}",
            ),
            target=self._mutation_target(),
        )
        _exact(result, _KILL_SWITCH_FIELDS, label="kill switch result")
        if not _boolean(result["kill_switch_active"], label="kill_switch_active"):
            raise PaperExecutorProtocolError("executor kill switch did not latch")
        _positive_integer(result["generation"], label="generation")
        if _text(result["reason_code"], label="reason_code") != reason:
            raise PaperExecutorProtocolError("executor kill switch acknowledgement has a different reason")

    def start_safe_flatten(
        self,
        operation_id: str,
    ) -> PaperSafeFlattenStartReceipt:
        clean_id = _canonical_request_id(operation_id, label="operation_id")
        target = self._mutation_target()
        payload = self._transport.request(
            "start_safe_flatten",
            {"operation_id": clean_id},
            request_id=clean_id,
            target=target,
        )
        status = _safe_flatten_response(payload, expected_operation_id=clean_id)
        if status.account_scope_sha256 != target.account_scope_sha256:
            raise PaperExecutorProtocolError(
                "safe-flatten response belongs to another account scope"
            )
        return PaperSafeFlattenStartReceipt(
            request_id=clean_id,
            operation_id=clean_id,
            target=target,
            status=status,
        )

    def get_safe_flatten_status(
        self,
        operation_id: str,
    ) -> PaperSafeFlattenStatus:
        clean_id = _canonical_request_id(operation_id, label="operation_id")
        payload = self._transport.request(
            "get_safe_flatten_status",
            {"operation_id": clean_id},
        )
        return _safe_flatten_response(payload, expected_operation_id=clean_id)

    def get_active_safe_flatten(self) -> PaperSafeFlattenStatus | None:
        payload = self._transport.request("get_active_safe_flatten")
        _exact(payload, frozenset({"operation"}), label="safe-flatten response")
        raw = payload["operation"]
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise PaperExecutorProtocolError(
                "executor safe-flatten response is invalid"
            )
        return _safe_flatten_status(raw, expected_operation_id=None)

    def reset_kill_switch(self) -> None:
        raise RuntimeError("executor kill switch cannot be reset over the online RPC")

    def mark_order_reconciled(self, _client_order_id: str) -> None:
        raise RuntimeError("terminal reconciliation is not exposed until broker evidence is verified")

    def reconcile_positions(
        self,
        expected_positions: tuple[PaperPosition, ...],
    ) -> ReconciliationReport:
        broker_positions = self.read_positions()
        broker_by_symbol = {position.symbol: position for position in broker_positions}
        expected_by_symbol = {position.symbol.upper(): position for position in expected_positions}
        differences: list[str] = []
        for symbol, expected in sorted(expected_by_symbol.items()):
            broker = broker_by_symbol.get(symbol)
            if broker is None:
                differences.append(f"missing_broker_position: {symbol}")
            elif abs(broker.quantity - expected.quantity) > 1e-9:
                differences.append(f"quantity_mismatch: {symbol} expected={expected.quantity} broker={broker.quantity}")
        for symbol in sorted(set(broker_by_symbol) - set(expected_by_symbol)):
            differences.append(f"unexpected_broker_position: {symbol}")
        return ReconciliationReport(
            matched=not differences,
            differences=tuple(differences),
            broker_positions=broker_positions,
            expected_positions=expected_positions,
        )

    def _mutation_target(self) -> ExecutorTarget:
        health = self.health()
        current = ExecutorTarget(
            account_scope_sha256=str(health["account_scope_sha256"]),
            policy_sha256=str(health["policy_sha256"]),
            authz_policy_sha256=str(health["authz_policy_sha256"]),
            run_id=str(health["run_id"]),
            fence_epoch=int(health["fence_epoch"]),
        )
        if self._expected_target is not None and current != self._expected_target:
            raise PaperExecutorProtocolError(
                "executor target changed after caller preflight"
            )
        return current


_ACCOUNT_FIELDS = frozenset({"account_id", "status", "cash", "equity", "buying_power", "last_equity"})
_HEALTH_FIELDS = frozenset(
    {
        "status",
        "mutations_allowed",
        "opening_orders_allowed",
        "capability_mode",
        "account_scope_sha256",
        "fence_epoch",
        "policy_sha256",
        "authz_policy_sha256",
        "run_id",
        "pending_recovery",
        "kill_switch_active",
    }
)
_KILL_SWITCH_FIELDS = frozenset({"kill_switch_active", "generation", "reason_code"})
_POSITION_FIELDS = frozenset(
    {
        "symbol",
        "quantity",
        "market_value",
        "avg_entry_price",
        "current_price",
        "unrealized_pl",
        "unrealized_plpc",
    }
)
_ORDER_FIELDS = frozenset(
    {
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "status",
        "notional",
        "quantity",
        "filled_quantity",
        "filled_avg_price",
        "submitted_at",
        "created_at",
        "updated_at",
        "expires_at",
        "stop_price",
        "limit_price",
        "filled_at",
        "realized_pnl",
    }
)
_FILL_FIELDS = frozenset(
    {
        "activity_id",
        "order_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "transaction_time",
        "cumulative_quantity",
        "leaves_quantity",
        "activity_type",
        "order_status",
    }
)
_RESULT_FIELDS = frozenset({"accepted", "status", "reasons", "dry_run"})
_SAFE_FLATTEN_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "account_scope_sha256",
        "state",
        "state_version",
        "terminal",
        "reconciled",
        "kill_switch_active",
        "retry_allowed",
        "failure_code",
        "outcome_unknown",
        "started_at",
        "updated_at",
    }
)
_SAFE_FLATTEN_STATES = frozenset(
    {
        "latched",
        "canceling",
        "cancel_confirmed",
        "closing",
        "fills_confirmed",
        "reconciling",
        "blocked_outcome_unknown",
        "failed_latched",
        "flat_latched",
    }
)


def _position(value: Any) -> PaperPosition:
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError("executor position response is invalid")
    _exact(value, _POSITION_FIELDS, label="position")
    return PaperPosition(
        symbol=_text(value["symbol"], label="symbol"),
        quantity=_finite(value["quantity"], label="quantity"),
        market_value=_finite(value["market_value"], label="market_value"),
        avg_entry_price=_finite(value["avg_entry_price"], label="avg_entry_price"),
        current_price=_finite(value["current_price"], label="current_price"),
        unrealized_pl=_optional_finite(value["unrealized_pl"], label="unrealized_pl"),
        unrealized_plpc=_optional_finite(value["unrealized_plpc"], label="unrealized_plpc"),
    )


def _order(value: Any) -> PaperOrderSnapshot:
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError("executor order response is invalid")
    _exact(value, _ORDER_FIELDS, label="order")
    return PaperOrderSnapshot(
        order_id=_text(value["order_id"], label="order_id"),
        client_order_id=_text(value["client_order_id"], label="client_order_id", allow_empty=True),
        symbol=_text(value["symbol"], label="symbol"),
        side=_text(value["side"], label="side"),
        order_type=_text(value["order_type"], label="order_type"),
        time_in_force=_text(value["time_in_force"], label="time_in_force"),
        status=_text(value["status"], label="status"),
        notional=_optional_finite(value["notional"], label="notional"),
        quantity=_optional_finite(value["quantity"], label="quantity"),
        filled_quantity=_finite(value["filled_quantity"], label="filled_quantity"),
        filled_avg_price=_optional_finite(value["filled_avg_price"], label="filled_avg_price"),
        submitted_at=_text(value["submitted_at"], label="submitted_at", allow_empty=True),
        created_at=_text(value["created_at"], label="created_at", allow_empty=True),
        updated_at=_text(value["updated_at"], label="updated_at", allow_empty=True),
        expires_at=_text(value["expires_at"], label="expires_at", allow_empty=True),
        stop_price=_optional_finite(value["stop_price"], label="stop_price"),
        limit_price=_optional_finite(value["limit_price"], label="limit_price"),
        filled_at=_text(value["filled_at"], label="filled_at", allow_empty=True),
        realized_pnl=_optional_finite(value["realized_pnl"], label="realized_pnl"),
    )


def _fill(value: Any) -> PaperFillActivity:
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError("executor fill response is invalid")
    _exact(value, _FILL_FIELDS, label="fill")
    return PaperFillActivity(
        activity_id=_text(value["activity_id"], label="activity_id"),
        order_id=_text(value["order_id"], label="order_id"),
        symbol=_text(value["symbol"], label="symbol"),
        side=_text(value["side"], label="side"),
        quantity=_finite(value["quantity"], label="quantity"),
        price=_finite(value["price"], label="price"),
        transaction_time=_text(value["transaction_time"], label="transaction_time"),
        cumulative_quantity=_finite(
            value["cumulative_quantity"],
            label="cumulative_quantity",
        ),
        leaves_quantity=_finite(value["leaves_quantity"], label="leaves_quantity"),
        activity_type=_text(value["activity_type"], label="activity_type"),
        order_status=_text(value["order_status"], label="order_status"),
    )


def _order_result(value: dict[str, Any]) -> PaperOrderResult:
    _exact(value, _RESULT_FIELDS, label="order result")
    accepted = value["accepted"]
    dry_run = value["dry_run"]
    reasons = value["reasons"]
    if type(accepted) is not bool or type(dry_run) is not bool or not isinstance(reasons, list):
        raise PaperExecutorProtocolError("executor order result is invalid")
    return PaperOrderResult(
        accepted=accepted,
        status=_text(value["status"], label="status"),
        reasons=tuple(_text(reason, label="reason") for reason in reasons),
        dry_run=dry_run,
    )


def _safe_flatten_response(
    payload: dict[str, Any],
    *,
    expected_operation_id: str,
) -> PaperSafeFlattenStatus:
    raw = _object_field(payload, "operation")
    return _safe_flatten_status(raw, expected_operation_id=expected_operation_id)


def _safe_flatten_status(
    raw: dict[str, Any],
    *,
    expected_operation_id: str | None,
) -> PaperSafeFlattenStatus:
    _exact(raw, _SAFE_FLATTEN_FIELDS, label="safe-flatten status")
    schema_version = _positive_integer(
        raw["schema_version"],
        label="schema_version",
    )
    if schema_version != 1:
        raise PaperExecutorProtocolError(
            "executor safe-flatten schema is unsupported"
        )
    operation_id = _canonical_request_id(
        raw["operation_id"],
        label="operation_id",
    )
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise PaperExecutorProtocolError(
            "executor returned a different safe-flatten operation"
        )
    state = _text(raw["state"], label="state")
    if state not in _SAFE_FLATTEN_STATES:
        raise PaperExecutorProtocolError(
            "executor safe-flatten state is unknown"
        )
    terminal = _boolean(raw["terminal"], label="terminal")
    reconciled = _boolean(raw["reconciled"], label="reconciled")
    kill_switch_active = _boolean(
        raw["kill_switch_active"],
        label="kill_switch_active",
    )
    retry_allowed = _boolean(raw["retry_allowed"], label="retry_allowed")
    if not kill_switch_active or retry_allowed:
        raise PaperExecutorProtocolError(
            "executor safe-flatten safety flags are inconsistent"
        )
    expected_terminal = state in {"failed_latched", "flat_latched"}
    if terminal is not expected_terminal or reconciled is not (state == "flat_latched"):
        raise PaperExecutorProtocolError(
            "executor safe-flatten terminal state is inconsistent"
        )
    failure_raw = raw["failure_code"]
    failure_code = (
        None if failure_raw is None else _text(failure_raw, label="failure_code")
    )
    if (state == "failed_latched") != (failure_code is not None):
        raise PaperExecutorProtocolError(
            "executor safe-flatten failure state is inconsistent"
        )
    outcome_raw = raw["outcome_unknown"]
    outcome_unknown: PaperSafeFlattenOutcomeUnknown | None = None
    if state == "blocked_outcome_unknown":
        if not isinstance(outcome_raw, dict):
            raise PaperExecutorProtocolError(
                "executor safe-flatten ambiguity context is missing"
            )
        _exact(
            outcome_raw,
            frozenset({"request_id", "operation", "phase", "retry_allowed"}),
            label="safe-flatten ambiguity",
        )
        unknown_retry = _boolean(
            outcome_raw["retry_allowed"],
            label="retry_allowed",
        )
        if unknown_retry:
            raise PaperExecutorProtocolError(
                "executor safe-flatten ambiguity permits an unsafe retry"
            )
        phase = _text(outcome_raw["phase"], label="phase")
        if phase not in {"canceling", "closing"}:
            raise PaperExecutorProtocolError(
                "executor safe-flatten recovery phase is invalid"
            )
        outcome_unknown = PaperSafeFlattenOutcomeUnknown(
            request_id=_canonical_request_id(
                outcome_raw["request_id"],
                label="request_id",
            ),
            operation=_text(outcome_raw["operation"], label="operation"),
            phase=phase,
            retry_allowed=False,
        )
    elif outcome_raw is not None:
        raise PaperExecutorProtocolError(
            "executor safe-flatten has unexpected ambiguity context"
        )
    started_at = _timestamp(raw["started_at"], label="started_at")
    updated_at = _timestamp(raw["updated_at"], label="updated_at")
    if datetime.fromisoformat(updated_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ):
        raise PaperExecutorProtocolError(
            "executor safe-flatten timestamps are inconsistent"
        )
    return PaperSafeFlattenStatus(
        schema_version=schema_version,
        operation_id=operation_id,
        account_scope_sha256=_hex_digest(
            raw["account_scope_sha256"],
            label="account_scope_sha256",
        ),
        state=state,
        state_version=_positive_integer(
            raw["state_version"],
            label="state_version",
        ),
        terminal=terminal,
        reconciled=reconciled,
        kill_switch_active=True,
        retry_allowed=False,
        failure_code=failure_code,
        outcome_unknown=outcome_unknown,
        started_at=started_at,
        updated_at=updated_at,
    )


def _object_field(payload: dict[str, Any], name: str) -> dict[str, Any]:
    _exact(payload, frozenset({name}), label="response")
    value = payload[name]
    if not isinstance(value, dict):
        raise PaperExecutorProtocolError(f"executor {name} response is invalid")
    return value


def _list_field(payload: dict[str, Any], name: str) -> list[Any]:
    _exact(payload, frozenset({name}), label="response")
    value = payload[name]
    if not isinstance(value, list):
        raise PaperExecutorProtocolError(f"executor {name} response is invalid")
    return value


def _exact(value: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    if set(value) != expected:
        raise PaperExecutorProtocolError(f"executor {label} fields are invalid")


def _text(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > 512 or any(ord(item) < 32 for item in value):
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    if not allow_empty and not value:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    return value


def _boolean(value: Any, *, label: str) -> bool:
    if type(value) is not bool:
        raise PaperExecutorProtocolError(f"executor {label} is not boolean")
    return value


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PaperExecutorProtocolError(f"executor {label} is not a nonnegative integer")
    return value


def _positive_integer(value: Any, *, label: str) -> int:
    result = _nonnegative_integer(value, label=label)
    if result < 1:
        raise PaperExecutorProtocolError(f"executor {label} is not positive")
    return result


def _hex_digest(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if len(text) != 64:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise PaperExecutorProtocolError(f"executor {label} is invalid") from exc
    if text.lower() != text:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    return text


def _canonical_request_id(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    if len(text) != 32:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    try:
        parsed = bytes.fromhex(text)
    except ValueError as exc:
        raise PaperExecutorProtocolError(f"executor {label} is invalid") from exc
    if len(parsed) != 16 or text.lower() != text or text == "0" * 32:
        raise PaperExecutorProtocolError(f"executor {label} is invalid")
    return text


def _timestamp(value: Any, *, label: str) -> str:
    text = _text(value, label=label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperExecutorProtocolError(f"executor {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperExecutorProtocolError(f"executor {label} is not timezone-aware")
    return text


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperExecutorProtocolError(f"executor {label} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise PaperExecutorProtocolError(f"executor {label} is non-finite")
    return number


def _optional_finite(value: Any, *, label: str) -> float | None:
    return None if value is None else _finite(value, label=label)
