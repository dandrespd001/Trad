"""High-level supervised-executor doubles for paper workflow tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    PaperAccount,
    PaperOrder,
    PaperOrderSnapshot,
    PaperPosition,
)
from trading_ai.execution.paper_execute_session import (
    _load_approved_session_package,
    _load_risk_from_session,
    _load_universe_from_session,
)
from trading_ai.execution.paper_executor_client import (
    PaperExecutorMutationReceipt,
)
from trading_ai.execution.paper_executor_ipc import ExecutorTarget
from trading_ai.execution.paper_executor_service import deterministic_command_id

_DIGEST = "a" * 64
_RUN_ID = "1" * 32


class ExecutorBrokerTestAdapter:
    """Wrap an SDK-shaped fake behind the production high-level contract."""

    def __init__(
        self,
        broker: AlpacaPaperBroker,
        *,
        mutations_allowed: bool = True,
        opening_orders_allowed: bool = True,
    ) -> None:
        self._broker = broker
        self.mutations_allowed = mutations_allowed
        self.opening_orders_allowed = opening_orders_allowed
        self.receipts: list[PaperExecutorMutationReceipt] = []

    def health(self) -> dict[str, object]:
        mutations_allowed = self.mutations_allowed
        opening_orders_allowed = mutations_allowed and self.opening_orders_allowed
        return {
            "status": "ready" if mutations_allowed else "blocked",
            "mutations_allowed": mutations_allowed,
            "opening_orders_allowed": opening_orders_allowed,
            "capability_mode": (
                "full"
                if opening_orders_allowed
                else "reduce_only"
                if mutations_allowed
                else "blocked"
            ),
            "account_scope_sha256": _DIGEST,
            "policy_sha256": "b" * 64,
            "authz_policy_sha256": "c" * 64,
            "run_id": _RUN_ID,
            "fence_epoch": 1,
            "pending_recovery": 0,
            "kill_switch_active": False,
        }

    def pin_target(self, health: dict[str, object]) -> ExecutorTarget:
        expected = self.health()
        target_fields = (
            "account_scope_sha256",
            "policy_sha256",
            "authz_policy_sha256",
            "run_id",
            "fence_epoch",
        )
        if any(health.get(field) != expected[field] for field in target_fields):
            raise AssertionError("test executor target changed")
        return ExecutorTarget(
            account_scope_sha256=str(health["account_scope_sha256"]),
            policy_sha256=str(health["policy_sha256"]),
            authz_policy_sha256=str(health["authz_policy_sha256"]),
            run_id=str(health["run_id"]),
            fence_epoch=int(health["fence_epoch"]),
        )

    def read_account(self) -> PaperAccount:
        return self._broker.read_account()

    def read_positions(self) -> tuple[PaperPosition, ...]:
        return self._broker.read_positions()

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        return self._broker.list_orders(status=status)

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        return self._broker.get_order_by_client_id(client_order_id)

    def submit_order_with_receipt(self, order: PaperOrder) -> PaperExecutorMutationReceipt:
        target = ExecutorTarget(
            account_scope_sha256=_DIGEST,
            policy_sha256="b" * 64,
            authz_policy_sha256="c" * 64,
            run_id=_RUN_ID,
            fence_epoch=1,
        )
        result = self._broker.submit_order(order)
        receipt = PaperExecutorMutationReceipt(
            request_id=deterministic_command_id(
                "submit_order",
                f"client_order_id:{order.client_order_id}",
            ),
            operation="submit_order",
            target=target,
            outcome="completed" if result.accepted else "rejected",
            result=result,
        )
        self.receipts.append(receipt)
        return receipt

    def activate_kill_switch(self, reason: str) -> None:
        self._broker.activate_kill_switch(reason)


def build_executor_adapter(
    raw_client: Any,
    session_dir: str | Path,
    *,
    market_data: object | None = None,
    as_of_date: date = date(2026, 6, 16),
    mutations_allowed: bool = True,
    opening_orders_allowed: bool = True,
) -> ExecutorBrokerTestAdapter:
    root = Path(session_dir)
    package = _load_approved_session_package(root)
    risk_limits = _load_risk_from_session(package.session, root)
    universe = _load_universe_from_session(package.session, root)
    broker = AlpacaPaperBroker(
        client=raw_client,
        allowlist=universe.symbols,
        risk_limits=risk_limits,
        dry_run=False,
        today=lambda: as_of_date,
        market_data=market_data,
        order_journal_path=root.parent / ".test-executor-order-journal.sqlite3",
    )
    return ExecutorBrokerTestAdapter(
        broker,
        mutations_allowed=mutations_allowed,
        opening_orders_allowed=opening_orders_allowed,
    )
