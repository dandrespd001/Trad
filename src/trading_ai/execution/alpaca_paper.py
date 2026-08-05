"""Alpaca paper adapter with dry-run and risk-gate defaults."""

from __future__ import annotations

import inspect
import math
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trading_ai.data.market_calendar import is_trading_day
from trading_ai.execution.account_supervisor import AccountLeaseBusyError
from trading_ai.execution.order_journal import (
    BrokerOrderIdCollisionError,
    DurableOrderJournal,
    InvalidOrderTransitionError,
    JournalState,
    OrderIntentCollisionError,
    OrderJournalError,
    OrderJournalReconciliationAttestation,
    OrderJournalStorageError,
)
from trading_ai.execution.paper_account_executor import (
    PaperAccountDispatchNotStartedError,
    paper_account_dispatch_not_started,
    require_audited_alpaca_http_transport,
)
from trading_ai.risk.policy import RiskLimits, evaluate_risk_state

CRYPTO_MIN_NOTIONAL_USD = 10.0
ORDER_LIST_MAX_LIMIT = 500
ACTIVITY_PAGE_SIZE = 100
ACTIVITY_MAX_PAGES = 100
POSITION_INTENTS = frozenset({"open", "increase", "reduce", "close"})
REDUCING_POSITION_INTENTS = frozenset({"reduce", "close"})
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
NONTERMINAL_ORDER_STATUSES = frozenset(
    {
        "accepted",
        "new",
        "pending_new",
        "partially_filled",
        "pending_cancel",
        "pending_replace",
        "done_for_day",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "calculated",
    }
)


@dataclass(frozen=True)
class _PortableGetOrdersRequest:
    """Minimal request shape for injected clients when alpaca-py is absent."""

    status: str
    limit: int


class IncompleteOrderSnapshotError(RuntimeError):
    """Raised when an order-list response may have hit the API result cap."""


class BrokerOrderLookupUnavailableError(RuntimeError):
    """Raised when broker-first recovery cannot determine order existence."""


class UnknownBrokerOrderStatusError(RuntimeError):
    """Raised when a broker status cannot be mapped without guessing."""


class InvalidPositionSnapshotError(RuntimeError):
    """Raised when a broker position cannot be trusted for reconciliation."""


class InvalidOrderSnapshotError(RuntimeError):
    """Raised when a broker order snapshot is incomplete or non-finite."""


class InvalidAccountSnapshotError(RuntimeError):
    """Raised when broker account risk fields are non-finite."""


class InvalidFillActivitySnapshotError(RuntimeError):
    """Raised when an account-activity fill cannot be trusted."""


class IncompleteFillActivitySnapshotError(RuntimeError):
    """Raised when pagination cannot prove the fill-activity set is complete."""


class _SubmitClaimError(RuntimeError):
    """Raised when the durable submit claim fails before broker dispatch."""


class _SubmitPreflightRejected(RuntimeError):
    """Carry a fresh under-lease preflight rejection without dispatching."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _CancelPreflightResolved(RuntimeError):
    """Carry a fresh order snapshot that blocks another cancel DELETE."""

    def __init__(self, snapshot: Any) -> None:
        super().__init__("cancel preflight resolved without dispatch")
        self.snapshot = snapshot


class _ExecutionGuardRejected(RuntimeError):
    """A server-side deadline/lifecycle guard rejected broker dispatch."""


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca crypto pairs use slash notation (e.g. ``BTC/USD``)."""
    return "/" in symbol


_TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_MESSAGE_FRAGMENTS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
    "connection reset",
    "connection aborted",
)


def _is_transient_error(exc: BaseException) -> bool:
    """Return True for broker errors worth retrying (timeouts, 429, 5xx, conn drops)."""

    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    status_code = _exception_status_code(exc)
    if status_code in _TRANSIENT_STATUS_CODES:
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in _TRANSIENT_MESSAGE_FRAGMENTS)


def _exception_status_code(exc: BaseException) -> int | None:
    candidates = (
        getattr(exc, "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(getattr(exc, "http_error", None), "status_code", None),
        getattr(getattr(getattr(exc, "http_error", None), "response", None), "status_code", None),
    )
    for value in candidates:
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_definitive_submit_rejection(exc: BaseException) -> bool:
    """Return True only when the broker explicitly rejected before acceptance."""

    status_code = _exception_status_code(exc)
    return status_code is not None and 400 <= status_code < 500 and status_code not in {
        404,
        408,
        409,
        429,
    }


@dataclass(frozen=True, init=False)
class PaperOrder:
    symbol: str
    side: str
    client_order_id: str
    quantity: float | None
    notional: float | None
    estimated_position_weight: float = 0.0
    projected_gross_exposure: float = 0.0
    daily_pnl_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    reference_price: float | None = None
    order_type: str = "market"
    limit_price: float | None = None
    position_intent: str = "open"

    def __init__(
        self,
        symbol: str,
        side: str,
        quantity: float | None = None,
        client_order_id: str = "",
        *,
        notional: float | None = None,
        estimated_position_weight: float = 0.0,
        projected_gross_exposure: float = 0.0,
        daily_pnl_pct: float = 0.0,
        current_drawdown_pct: float = 0.0,
        reference_price: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
        position_intent: str = "open",
    ) -> None:
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "client_order_id", client_order_id)
        object.__setattr__(self, "notional", notional)
        object.__setattr__(self, "estimated_position_weight", estimated_position_weight)
        object.__setattr__(self, "projected_gross_exposure", projected_gross_exposure)
        object.__setattr__(self, "daily_pnl_pct", daily_pnl_pct)
        object.__setattr__(self, "current_drawdown_pct", current_drawdown_pct)
        object.__setattr__(self, "reference_price", reference_price)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "position_intent", position_intent)


@dataclass(frozen=True)
class PaperOrderResult:
    accepted: bool
    status: str
    reasons: tuple[str, ...]
    dry_run: bool
    broker_response: Any | None = None

    def proves_not_dispatched(self, operation: str) -> bool:
        """Consume a trusted, operation-bound proof that no mutation was entered."""

        return _consume_not_dispatched_result_proof(self, operation=operation)


_NOT_DISPATCHED_RESULT_PROOF_LOCK = threading.Lock()
_NOT_DISPATCHED_RESULT_PROOFS: dict[
    int,
    tuple[weakref.ReferenceType[PaperOrderResult], str],
] = {}


def _register_not_dispatched_result_proof(
    result: PaperOrderResult,
    *,
    operation: str,
) -> None:
    result_id = id(result)

    def discard(reference: weakref.ReferenceType[PaperOrderResult]) -> None:
        with _NOT_DISPATCHED_RESULT_PROOF_LOCK:
            current = _NOT_DISPATCHED_RESULT_PROOFS.get(result_id)
            if current is not None and current[0] is reference:
                _NOT_DISPATCHED_RESULT_PROOFS.pop(result_id, None)

    reference = weakref.ref(result, discard)
    with _NOT_DISPATCHED_RESULT_PROOF_LOCK:
        _NOT_DISPATCHED_RESULT_PROOFS[result_id] = (reference, operation)


def _consume_not_dispatched_result_proof(
    result: PaperOrderResult,
    *,
    operation: str,
) -> bool:
    with _NOT_DISPATCHED_RESULT_PROOF_LOCK:
        proof = _NOT_DISPATCHED_RESULT_PROOFS.pop(id(result), None)
    return proof is not None and proof[0]() is result and proof[1] == operation


def _not_dispatched_result(
    *,
    operation: str,
    status: str,
    reason: str,
) -> PaperOrderResult:
    """Create a one-shot in-process capability for audited pre-dispatch paths."""

    result = PaperOrderResult(False, status, (reason,), False)
    _register_not_dispatched_result_proof(
        result,
        operation=operation,
    )
    return result


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    status: str
    cash: float
    equity: float
    buying_power: float
    last_equity: float = 0.0


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    quantity: float
    market_value: float
    avg_entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None


@dataclass(frozen=True)
class PaperOrderSnapshot:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    notional: float | None
    quantity: float | None
    filled_quantity: float
    filled_avg_price: float | None
    submitted_at: str
    created_at: str
    updated_at: str
    expires_at: str
    stop_price: float | None = None
    limit_price: float | None = None
    filled_at: str = ""
    realized_pnl: float | None = None


@dataclass(frozen=True)
class PaperFillActivity:
    activity_id: str
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    transaction_time: str
    cumulative_quantity: float
    leaves_quantity: float
    activity_type: str
    order_status: str


@dataclass(frozen=True)
class PaperPreflightContext:
    signal: Any | None
    client_order_id: str | None
    open_orders: tuple[PaperOrderSnapshot, ...]
    positions: tuple[PaperPosition, ...]
    as_of_date: date
    max_feature_age_days: int


@dataclass(frozen=True)
class PaperPreflightDecision:
    allowed: bool
    reasons: tuple[str, ...]
    checked_at: str
    max_feature_age_days: int


@dataclass(frozen=True)
class ReconciliationReport:
    matched: bool
    differences: tuple[str, ...]
    broker_positions: tuple[PaperPosition, ...]
    expected_positions: tuple[PaperPosition, ...]


class AlpacaPaperBroker:
    """Small paper-only broker boundary.

    The adapter defaults to dry-run. Real paper submission requires an injected
    client and still passes through allowlist and risk checks first.
    """

    def __init__(
        self,
        *,
        client: Any | None,
        allowlist: tuple[str, ...],
        risk_limits: RiskLimits,
        dry_run: bool = True,
        max_retries: int = 2,
        retry_base_delay: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        market_data: Any | None = None,
        crypto_market_data: Any | None = None,
        order_journal_path: str | Path | None = None,
    ) -> None:
        self._client = client
        self._allowlist = {symbol.upper() for symbol in allowlist}
        self._risk_limits = risk_limits
        self._dry_run = dry_run
        self._dry_run_order_ids: set[str] = set()
        self._dry_run_cancelled_ids: set[str] = set()
        self._kill_switch_active = False
        self._kill_switch_reason: str | None = None
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = max(0.0, retry_base_delay)
        self._sleep = sleep
        self._today = today
        self._market_data = market_data
        self._crypto_market_data = crypto_market_data
        supervised_journal_path = getattr(client, "order_journal_path", None)
        if not dry_run and isinstance(supervised_journal_path, (str, Path)):
            self._order_journal_path = Path(supervised_journal_path)
        else:
            self._order_journal_path = (
                Path(order_journal_path) if order_journal_path is not None else None
            )
        self._order_journal: Any | None = None

    @property
    def executor_authority(self) -> Any | None:
        """Return the exact supervised client which owns broker authority.

        The single-account executor uses object identity on this capability so
        a broker assembled with another account client cannot be paired with a
        foreign authority ledger.
        """

        return None if self._dry_run else self._client

    @property
    def order_journal_path(self) -> Path | None:
        return self._order_journal_path

    def _call_with_retry(
        self,
        func: Callable[[], Any],
        *,
        idempotency_check: Callable[[], Any | None] | None = None,
    ) -> Any:
        """Call ``func`` with exponential backoff on transient broker errors.

        ``idempotency_check`` is consulted before each retry: if it resolves a
        result (e.g. the order already exists at the broker), that result is
        returned instead of re-issuing the request — preventing duplicate orders
        when a submit times out after the broker accepted it.
        """

        attempt = 0
        while True:
            try:
                return func()
            except Exception as exc:
                if not _is_transient_error(exc) or attempt >= self._max_retries:
                    raise
                attempt += 1
                if idempotency_check is not None:
                    resolved = idempotency_check()
                    if resolved is not None:
                        return resolved
                if self._retry_base_delay > 0:
                    self._sleep(self._retry_base_delay * (2 ** (attempt - 1)))

    def read_account(self) -> PaperAccount:
        if self._dry_run or self._client is None:
            return PaperAccount(
                account_id="dry-run",
                status="DRY_RUN",
                cash=0.0,
                equity=0.0,
                buying_power=0.0,
            )
        account = self._call_with_retry(self._client.get_account)
        raw_account_id = _get_attr(account, "id", None)
        raw_status = _get_attr(account, "status", None)
        if raw_account_id is None or isinstance(raw_account_id, bool):
            raise InvalidAccountSnapshotError("broker account identity/status is missing")
        if raw_status is None or isinstance(raw_status, bool):
            raise InvalidAccountSnapshotError("broker account identity/status is missing")
        account_id = str(raw_account_id).strip()
        status = _enum_text(raw_status).strip()
        if not account_id or not status:
            raise InvalidAccountSnapshotError("broker account identity/status is missing")
        return PaperAccount(
            account_id=account_id,
            status=status,
            cash=_account_finite_float(_get_attr(account, "cash", 0.0), field="cash"),
            equity=_account_finite_float(_get_attr(account, "equity", 0.0), field="equity"),
            buying_power=_account_finite_float(
                _get_attr(account, "buying_power", 0.0),
                field="buying_power",
            ),
            last_equity=_account_finite_float(
                _get_attr(account, "last_equity", 0.0) or 0.0,
                field="last_equity",
            ),
        )

    def read_positions(self) -> tuple[PaperPosition, ...]:
        if self._dry_run or self._client is None:
            return ()
        positions: list[PaperPosition] = []
        if hasattr(self._client, "list_positions"):
            raw_positions = self._call_with_retry(self._client.list_positions)
        elif hasattr(self._client, "get_all_positions"):
            raw_positions = self._call_with_retry(self._client.get_all_positions)
        else:
            raise AttributeError("broker client must expose list_positions or get_all_positions")
        seen_assets: set[str] = set()
        for position in raw_positions:
            symbol = str(_get_attr(position, "symbol", "")).upper()
            if not symbol:
                raise InvalidPositionSnapshotError("broker position is missing symbol")
            asset_key = _asset_key(symbol)
            if asset_key in seen_assets:
                raise InvalidPositionSnapshotError(f"duplicate broker position for {symbol}")
            seen_assets.add(asset_key)
            quantity = _required_finite_float(
                _get_attr(position, "qty", _get_attr(position, "quantity", None)),
                field="quantity",
            )
            market_value = _required_finite_float(
                _get_attr(position, "market_value", None),
                field="market_value",
            )
            if abs(quantity) <= 1e-12:
                raise InvalidPositionSnapshotError(f"broker position {symbol} has zero quantity")
            if abs(market_value) <= 1e-12 or quantity * market_value <= 0:
                raise InvalidPositionSnapshotError(
                    f"broker position {symbol} has inconsistent quantity and market value"
                )
            # Account reconciliation must retain every broker position. Filtering
            # unknown symbols here can make a flatten or exposure check report a
            # false zero while capital is still at risk.
            positions.append(
                PaperPosition(
                    symbol=symbol,
                    quantity=quantity,
                    market_value=market_value,
                    avg_entry_price=_finite_float_or_default(
                        _get_attr(position, "avg_entry_price", None),
                        field="avg_entry_price",
                    ),
                    current_price=_finite_float_or_default(
                        _get_attr(position, "current_price", None),
                        field="current_price",
                    ),
                    unrealized_pl=_optional_finite_float(
                        _get_attr(position, "unrealized_pl", None),
                        field="unrealized_pl",
                    ),
                    unrealized_plpc=_optional_finite_float(
                        _get_attr(position, "unrealized_plpc", None),
                        field="unrealized_plpc",
                    ),
                )
            )
        return tuple(positions)

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        if self._dry_run or self._client is None:
            return ()
        client = self._client
        assert client is not None  # narrowed by the dry-run/None guard above
        raw_orders = self._call_with_retry(lambda: client.get_orders(filter=_build_get_orders_request(status)))
        orders = tuple(raw_orders)
        if len(orders) >= ORDER_LIST_MAX_LIMIT:
            raise IncompleteOrderSnapshotError(
                "order list reached the 500-order API limit; snapshot completeness is unknown"
            )
        return tuple(_paper_order_snapshot_from_raw(order) for order in orders)

    def list_fill_activities(
        self,
        *,
        after: datetime,
        until: datetime,
    ) -> tuple[PaperFillActivity, ...]:
        """Read every broker FILL activity in an inclusive evidence window.

        Alpaca order snapshots expose only aggregate filled quantity and VWAP.
        This paginated activity read supplies the individual, venue-side fill
        identifiers needed for deduplication and reconciliation.  Any
        ambiguous page boundary blocks rather than returning a partial set.
        """

        if self._dry_run or self._client is None:
            return ()
        if after.tzinfo is None or after.utcoffset() is None:
            raise ValueError("after must include a timezone")
        if until.tzinfo is None or until.utcoffset() is None:
            raise ValueError("until must include a timezone")
        if after > until:
            raise ValueError("after must not be later than until")
        if not hasattr(self._client, "get"):
            raise IncompleteFillActivitySnapshotError(
                "broker client does not expose account activities"
            )

        by_id: dict[str, PaperFillActivity] = {}
        page_token: str | None = None
        for _page_number in range(ACTIVITY_MAX_PAGES):
            params = {
                "after": after.isoformat(),
                "until": until.isoformat(),
                "direction": "asc",
                "page_size": ACTIVITY_PAGE_SIZE,
            }
            if page_token is not None:
                params["page_token"] = page_token
            raw_page = self._call_with_retry(
                lambda params=params: self._client.get("/account/activities/FILL", params)
            )
            if not isinstance(raw_page, list):
                raise IncompleteFillActivitySnapshotError(
                    "broker fill-activity page is not a list"
                )
            if not raw_page:
                return tuple(by_id[key] for key in sorted(by_id))

            for raw_activity in raw_page:
                activity = _paper_fill_activity_from_raw(raw_activity)
                previous = by_id.get(activity.activity_id)
                if previous is not None and previous != activity:
                    raise InvalidFillActivitySnapshotError(
                        f"broker fill activity {activity.activity_id!r} changed within one snapshot"
                    )
                by_id[activity.activity_id] = activity

            if len(raw_page) < ACTIVITY_PAGE_SIZE:
                return tuple(by_id[key] for key in sorted(by_id))
            next_token = str(_get_attr(raw_page[-1], "id", ""))
            if not next_token or next_token == page_token:
                raise IncompleteFillActivitySnapshotError(
                    "broker fill-activity pagination token is missing or repeated"
                )
            page_token = next_token

        raise IncompleteFillActivitySnapshotError(
            "broker fill-activity pagination reached the configured page cap"
        )

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        if self._dry_run or self._client is None:
            raise ValueError("dry-run broker does not have remote orders")
        snapshot = _paper_order_snapshot_from_raw(
            self._call_with_retry(lambda: self._client.get_order_by_id(order_id))
        )
        self._sync_journal_snapshot(snapshot)
        return snapshot

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        if self._dry_run or self._client is None:
            raise ValueError("dry-run broker does not have remote orders")
        snapshot = _paper_order_snapshot_from_raw(
            self._call_with_retry(lambda: self._client.get_order_by_client_id(client_order_id))
        )
        self._sync_journal_snapshot(snapshot)
        return snapshot

    def _sync_journal_snapshot(self, snapshot: PaperOrderSnapshot) -> None:
        if self._order_journal is None or not snapshot.client_order_id:
            return
        record = self._order_journal.get(snapshot.client_order_id)
        if record is None:
            return
        if (
            record.broker_order_id is not None
            and snapshot.order_id
            and record.broker_order_id != snapshot.order_id
        ):
            raise BrokerOrderIdCollisionError(
                f"client_order_id {snapshot.client_order_id!r} is already bound to broker order "
                f"{record.broker_order_id!r}"
            )
        target = _journal_state_from_broker_status(snapshot.status)
        current = _journal_state_text(record.state)
        if current == "reconciled" or current == _journal_state_text(target):
            return
        if current in {"cancel_requested", "cancel_unresolved"} and target not in {
            JournalState.REJECTED,
            JournalState.CANCELED,
            JournalState.FILLED,
            JournalState.EXPIRED,
        }:
            # An observed non-terminal state does not prove that a prior
            # ambiguous DELETE failed. Preserve the cancel-intent latch; a
            # later cancel path may retry only after its own fresh broker read
            # explicitly confirms that the order is still working.
            return
        self._order_journal.transition(
            snapshot.client_order_id,
            target,
            broker_order_id=snapshot.order_id or None,
            metadata={"broker_status": snapshot.status, "operation": "read_reconciliation"},
        )

    def activate_kill_switch(self, reason: str) -> None:
        self._kill_switch_active = True
        self._kill_switch_reason = reason

    def reset_kill_switch(self) -> None:
        self._kill_switch_active = False
        self._kill_switch_reason = None

    def mark_order_reconciled(self, client_order_id: str) -> None:
        """Close a terminal journal record after broker/account reconciliation."""

        journal = self._ensure_order_journal()
        record = journal.get(client_order_id)
        if record is None:
            raise InvalidOrderTransitionError(f"order intent is missing from journal: {client_order_id}")
        state = _journal_state_text(record.state)
        if state == "reconciled":
            return
        if state not in {"filled", "canceled", "expired", "rejected"}:
            raise InvalidOrderTransitionError(
                f"order cannot be reconciled from non-terminal state: {state}"
            )
        journal.transition(
            client_order_id,
            JournalState.RECONCILED,
            metadata={"operation": "account_reconciliation"},
            expected_state=record.state,
        )

    def reconcile_flat_account_order_journal(
        self,
    ) -> OrderJournalReconciliationAttestation | None:
        """Reconcile every durable intent only while broker evidence is flat.

        The method is deliberately broker-first.  Nonterminal journal entries
        are refreshed by their durable client order ID and remain blocking when
        the lookup is unavailable or the broker still reports a nonterminal
        state.  It never interprets a missing order as proof that a prior
        mutation did not occur.
        """

        journal = self._ensure_order_journal()
        identity = journal.storage_identity()
        if self.list_orders(status="open") or self.read_positions():
            return None

        for initial in journal.records():
            state = _journal_state_text(initial.state)
            if state == JournalState.RECONCILED.value:
                continue
            if state not in {
                JournalState.FILLED.value,
                JournalState.CANCELED.value,
                JournalState.EXPIRED.value,
                JournalState.REJECTED.value,
            }:
                try:
                    self.get_order_by_client_id(initial.client_order_id)
                except OrderJournalError:
                    raise
                except Exception:
                    return None
                refreshed = journal.get(initial.client_order_id)
                if refreshed is None:
                    raise InvalidOrderTransitionError(
                        "order intent disappeared during account reconciliation"
                    )
                state = _journal_state_text(refreshed.state)
                if state not in {
                    JournalState.FILLED.value,
                    JournalState.CANCELED.value,
                    JournalState.EXPIRED.value,
                    JournalState.REJECTED.value,
                    JournalState.RECONCILED.value,
                }:
                    return None

        try:
            attestation = journal.reconcile_terminal_records(
                metadata={"operation": "paper_executor_safe_flatten"},
            )
        except InvalidOrderTransitionError:
            return None

        if journal.storage_identity() != identity:
            raise OrderJournalStorageError(
                "order journal identity changed during account reconciliation"
            )
        if self.list_orders(status="open") or self.read_positions():
            return None
        if journal.reconciliation_attestation() != attestation:
            raise OrderJournalStorageError(
                "order journal changed after account reconciliation"
            )
        return attestation

    def latest_trade_price(self, symbol: str) -> float | None:
        return self._read_latest_trade_price(symbol.upper())

    def submit_order(
        self,
        order: PaperOrder,
        *,
        execution_guard: Callable[[], None] | None = None,
    ) -> PaperOrderResult:
        symbol = order.symbol.upper()
        position_intent = order.position_intent.strip().lower()
        is_reducing = position_intent in REDUCING_POSITION_INTENTS
        submit_method = getattr(self._client, "submit_order", None)
        price_sanity_runs_inside_client_authority = callable(
            submit_method
        ) and _accepts_explicit_keyword(submit_method, "before_dispatch")
        price_sanity_checked = False
        if self._kill_switch_active and not is_reducing:
            return PaperOrderResult(False, "risk_rejected", ("kill_switch_active",), self._dry_run)
        if position_intent not in POSITION_INTENTS:
            return PaperOrderResult(False, "rejected", ("invalid_position_intent",), self._dry_run)
        if (
            order.side.lower() == "buy"
            and not is_crypto_symbol(symbol)
            and not is_reducing
            and not is_trading_day(self._today())
        ):
            return PaperOrderResult(False, "rejected", ("market_closed_not_a_trading_day",), self._dry_run)
        if (
            order.side.lower() == "buy"
            and not self._dry_run
            and not is_reducing
            and not price_sanity_runs_inside_client_authority
        ):
            price_sanity_reason = self._price_sanity_rejection_reason(order)
            if price_sanity_reason is not None:
                return PaperOrderResult(False, "rejected", (price_sanity_reason,), self._dry_run)
            price_sanity_checked = True
        if symbol not in self._allowlist and not is_reducing:
            return PaperOrderResult(False, "rejected", ("symbol_not_allowlisted",), self._dry_run)
        if order.side.lower() not in {"buy", "sell"}:
            return PaperOrderResult(False, "rejected", ("invalid_side",), self._dry_run)
        if order.side.lower() == "sell" and not is_reducing:
            return PaperOrderResult(False, "rejected", ("sell_requires_reducing_intent",), self._dry_run)
        if not order.client_order_id:
            return PaperOrderResult(False, "rejected", ("missing_client_order_id",), self._dry_run)
        if (order.quantity is None) == (order.notional is None):
            return PaperOrderResult(False, "rejected", ("quantity_or_notional_required",), self._dry_run)
        if order.quantity is not None and order.quantity <= 0:
            return PaperOrderResult(False, "rejected", ("invalid_quantity",), self._dry_run)
        if order.notional is not None and order.notional <= 0:
            return PaperOrderResult(False, "rejected", ("invalid_notional",), self._dry_run)
        if order.order_type not in {"market", "limit"}:
            return PaperOrderResult(False, "rejected", ("invalid_order_type",), self._dry_run)
        if order.order_type == "limit" and (order.limit_price is None or order.limit_price <= 0):
            return PaperOrderResult(False, "rejected", ("limit_price_required",), self._dry_run)
        if is_reducing and (order.quantity is None or order.notional is not None):
            return PaperOrderResult(False, "rejected", ("reducing_order_requires_quantity",), self._dry_run)
        if (
            is_crypto_symbol(symbol)
            and order.notional is not None
            and order.notional < CRYPTO_MIN_NOTIONAL_USD
        ):
            return PaperOrderResult(
                False,
                "rejected",
                ("crypto_notional_below_minimum",),
                self._dry_run,
            )

        if not is_reducing:
            risk = evaluate_risk_state(
                daily_pnl_pct=order.daily_pnl_pct,
                current_drawdown_pct=order.current_drawdown_pct,
                gross_exposure=order.projected_gross_exposure,
                largest_position_weight=order.estimated_position_weight,
                mode="paper",
                limits=self._risk_limits,
            )
            if not risk.allowed:
                return PaperOrderResult(False, "risk_rejected", tuple(risk.reasons), self._dry_run)

        if self._dry_run:
            if order.client_order_id in self._dry_run_order_ids:
                return PaperOrderResult(True, "duplicate_accepted", (), True)
            self._dry_run_order_ids.add(order.client_order_id)
            return PaperOrderResult(True, "dry_run_accepted", (), True)
        if self._client is None:
            return PaperOrderResult(False, "rejected", ("broker_client_missing",), False)

        try:
            journal = self._ensure_order_journal()
            record, _created = journal.record_intent(
                order.client_order_id,
                _journal_intent_from_order(symbol=symbol, order=order),
            )
        except OrderIntentCollisionError:
            return PaperOrderResult(False, "rejected", ("client_order_id_intent_mismatch",), False)
        except (OrderJournalError, OSError, ValueError):
            return PaperOrderResult(False, "rejected", ("durable_order_journal_unavailable",), False)

        record_state = _journal_state_text(record.state)
        if record_state in {"filled", "canceled", "expired", "rejected", "reconciled"}:
            return _result_from_terminal_journal_state(record_state)

        # Broker-first is mandatory for both restart recovery and a newly
        # recorded intent. An unavailable lookup is not equivalent to 404.
        try:
            existing = self._lookup_existing_order(order.client_order_id)
        except Exception as exc:
            if not _is_order_not_found_error(exc):
                if record_state == "intent_recorded":
                    if self._record_not_dispatched(
                        journal,
                        client_order_id=order.client_order_id,
                        operation="submit",
                        expected_state=JournalState.INTENT_RECORDED,
                        reason="broker_lookup_unavailable",
                        error_type=type(exc).__name__,
                    ):
                        return _not_dispatched_result(
                            operation="submit_order",
                            status="submit_deferred",
                            reason="broker_lookup_unavailable",
                        )
                    return PaperOrderResult(
                        False,
                        "submit_unresolved",
                        ("submit_not_dispatched_marker_failed",),
                        False,
                    )
                else:
                    self._transition_best_effort(
                        order.client_order_id,
                        "submit_unresolved",
                        metadata={"reason": "broker_lookup_unavailable", "error_type": type(exc).__name__},
                    )
                return PaperOrderResult(False, "submit_unresolved", ("broker_lookup_unavailable",), False)
            existing = None

        if existing is not None:
            return self._adopt_existing_order(order=order, raw_order=existing, journal=journal)
        if record_state not in {"intent_recorded"}:
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "broker_order_missing_after_submit_state"},
            )
            return PaperOrderResult(
                False,
                "submit_unresolved",
                ("broker_order_missing_after_submit_state",),
                False,
            )

        if is_reducing:
            reduction_reason = self._reducing_order_rejection_reason(order)
            if reduction_reason is not None:
                return PaperOrderResult(False, "risk_rejected", (reduction_reason,), False)

        submit_claimed = False

        def claim_immediately_before_dispatch() -> None:
            nonlocal submit_claimed
            if self._kill_switch_active and not is_reducing:
                raise _SubmitPreflightRejected("risk_rejected", "kill_switch_active")
            if is_reducing:
                fresh_reduction_reason = self._reducing_order_rejection_reason(order)
                if fresh_reduction_reason is not None:
                    raise _SubmitPreflightRejected("risk_rejected", fresh_reduction_reason)
            elif order.side.lower() == "buy" and not price_sanity_checked:
                fresh_price_reason = self._price_sanity_rejection_reason(order)
                if fresh_price_reason is not None:
                    raise _SubmitPreflightRejected("rejected", fresh_price_reason)
            if execution_guard is not None:
                try:
                    execution_guard()
                except Exception as exc:
                    raise _ExecutionGuardRejected("executor dispatch guard rejected submit") from exc
            try:
                _claimed_record, claimed = journal.transition(
                    order.client_order_id,
                    JournalState.SUBMIT_ATTEMPTED,
                    expected_state=JournalState.INTENT_RECORDED,
                    metadata={"operation": "submit", "inside_account_lease": True},
                )
            except OrderJournalError as exc:
                raise _SubmitClaimError("durable submit claim failed") from exc
            if not claimed:
                raise _SubmitClaimError("durable submit claim was not acquired")
            submit_claimed = True

        try:
            response = _submit_broker_order(
                self._client,
                symbol=symbol,
                order=order,
                before_dispatch=claim_immediately_before_dispatch,
            )
        except _SubmitPreflightRejected as exc:
            return PaperOrderResult(False, exc.status, (exc.reason,), False)
        except _ExecutionGuardRejected:
            if not self._record_not_dispatched(
                journal,
                client_order_id=order.client_order_id,
                operation="submit",
                expected_state=JournalState.INTENT_RECORDED,
                reason="execution_guard_rejected",
                error_type="_ExecutionGuardRejected",
            ):
                return PaperOrderResult(
                    False,
                    "submit_unresolved",
                    ("submit_not_dispatched_marker_failed",),
                    False,
                )
            return _not_dispatched_result(
                operation="submit_order",
                status="submit_deferred",
                reason="execution_guard_rejected",
            )
        except _SubmitClaimError:
            return PaperOrderResult(
                False,
                "submit_unresolved",
                ("concurrent_submit_or_journal_failure",),
                False,
            )
        except Exception as exc:
            if paper_account_dispatch_not_started(exc, operation="submit"):
                cause = (
                    exc.__cause__
                    if isinstance(exc, PaperAccountDispatchNotStartedError) and exc.__cause__ is not None
                    else exc
                )
                reason = (
                    "account_mutation_busy"
                    if isinstance(cause, AccountLeaseBusyError)
                    else "paper_account_pre_dispatch_failed"
                )
                if not self._record_not_dispatched(
                    journal,
                    client_order_id=order.client_order_id,
                    operation="submit",
                    expected_state=JournalState.INTENT_RECORDED,
                    reason=reason,
                    error_type=type(cause).__name__,
                ):
                    return PaperOrderResult(
                        False,
                        "submit_unresolved",
                        ("submit_not_dispatched_marker_failed",),
                        False,
                    )
                return _not_dispatched_result(
                    operation="submit_order",
                    status="submit_deferred",
                    reason=reason,
                )
            if isinstance(exc, AccountLeaseBusyError) and not submit_claimed:
                # Only the supervised client can prove that a false claim flag
                # means its raw SDK mutation was never entered.  Untrusted
                # injected clients remain ambiguous here.
                return PaperOrderResult(
                    False,
                    "submit_unresolved",
                    ("pre_dispatch_outcome_unproven",),
                    False,
                )
            if _is_definitive_submit_rejection(exc):
                self._transition_best_effort(
                    order.client_order_id,
                    "rejected",
                    metadata={"reason": "broker_submit_rejected", "error_type": type(exc).__name__},
                )
                return PaperOrderResult(False, "rejected", ("broker_submit_rejected",), False)
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "ambiguous_submit_error", "error_type": type(exc).__name__},
            )
            try:
                existing = self._lookup_existing_order(order.client_order_id)
            except Exception:
                existing = None
            if existing is not None:
                return self._adopt_existing_order(order=order, raw_order=existing, journal=journal)
            return PaperOrderResult(False, "submit_unresolved", ("ambiguous_submit_error",), False)

        if not _raw_order_matches_intent(response, order=order, symbol=symbol):
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "broker_order_intent_mismatch"},
            )
            return PaperOrderResult(False, "submit_unresolved", ("broker_order_intent_mismatch",), False)

        broker_status = _enum_text(_get_attr(response, "status", ""))
        try:
            journal_state = _journal_state_from_broker_status(broker_status)
        except UnknownBrokerOrderStatusError:
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "broker_order_status_unknown"},
            )
            return PaperOrderResult(False, "submit_unresolved", ("broker_order_status_unknown",), False)
        broker_order_id = str(_get_attr(response, "id", "")) or None
        try:
            journal.transition(
                order.client_order_id,
                journal_state,
                broker_order_id=broker_order_id,
                expected_state=JournalState.SUBMIT_ATTEMPTED,
                metadata={"broker_status": broker_status},
            )
        except OrderJournalError:
            return PaperOrderResult(False, "submit_unresolved", ("journal_acknowledgement_failed",), False)
        accepted = broker_status not in {"rejected", "canceled", "expired"}
        status = "submitted" if accepted else f"broker_{broker_status}"
        reasons = () if accepted else (f"broker_terminal_{broker_status}",)
        return PaperOrderResult(accepted, status, reasons, False, response)

    def _ensure_order_journal(self) -> DurableOrderJournal:
        if self._order_journal_path is None:
            raise OrderJournalStorageError("durable paper order journal path is required")
        if self._order_journal is None:
            self._order_journal = DurableOrderJournal(self._order_journal_path)
        return self._order_journal

    def _adopt_existing_order(
        self,
        *,
        order: PaperOrder,
        raw_order: Any,
        journal: DurableOrderJournal,
    ) -> PaperOrderResult:
        symbol = order.symbol.upper()
        if not _raw_order_matches_intent(raw_order, order=order, symbol=symbol):
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "broker_order_intent_mismatch"},
            )
            return PaperOrderResult(False, "submit_unresolved", ("broker_order_intent_mismatch",), False)

        broker_status = _enum_text(_get_attr(raw_order, "status", ""))
        try:
            journal_state = _journal_state_from_broker_status(broker_status)
        except UnknownBrokerOrderStatusError:
            self._transition_best_effort(
                order.client_order_id,
                "submit_unresolved",
                metadata={"reason": "broker_order_status_unknown"},
            )
            return PaperOrderResult(False, "submit_unresolved", ("broker_order_status_unknown",), False)
        broker_order_id = str(_get_attr(raw_order, "id", "")) or None
        try:
            journal.transition(
                order.client_order_id,
                journal_state,
                broker_order_id=broker_order_id,
                metadata={"broker_status": broker_status, "operation": "broker_first_recovery"},
            )
        except InvalidOrderTransitionError:
            current = journal.get(order.client_order_id)
            if current is None or _journal_state_text(current.state) != _journal_state_text(journal_state):
                return PaperOrderResult(False, "submit_unresolved", ("journal_recovery_transition_failed",), False)
        except OrderJournalError:
            return PaperOrderResult(False, "submit_unresolved", ("durable_order_journal_unavailable",), False)

        accepted = broker_status not in {"rejected", "canceled", "expired"}
        status = f"recovered_{broker_status or 'unknown'}"
        reasons = () if accepted else (f"broker_terminal_{broker_status}",)
        return PaperOrderResult(accepted, status, reasons, False, raw_order)

    def _transition_best_effort(
        self,
        client_order_id: str,
        state: str,
        *,
        metadata: dict[str, object],
    ) -> None:
        """Persist a conservative state without ever hiding the primary result.

        Callers already return a blocked/unresolved result. A failed secondary
        transition must not trigger another broker mutation.
        """

        if self._order_journal is None:
            return
        try:
            self._order_journal.transition(client_order_id, state, metadata=metadata)
        except OrderJournalError:
            return

    @staticmethod
    def _record_not_dispatched(
        journal: DurableOrderJournal,
        *,
        client_order_id: str,
        operation: str,
        expected_state: JournalState,
        reason: str,
        error_type: str,
    ) -> bool:
        """Durably prove that an SDK mutation was not authorized or entered."""

        try:
            journal.record_marker(
                client_order_id,
                f"{operation}_not_dispatched",
                expected_state=expected_state,
                metadata={
                    "classification": "not_dispatched_retryable",
                    "error_type": error_type,
                    "reason": reason,
                },
            )
        except OrderJournalError:
            return False
        return True

    def _reducing_order_rejection_reason(self, order: PaperOrder) -> str | None:
        """Enforce reduce-only locally using fresh, complete broker snapshots."""

        try:
            journal = self._ensure_order_journal()
            for prior in journal.records():
                if prior.client_order_id == order.client_order_id:
                    continue
                intent = prior.intent
                if (
                    _asset_key(str(intent.get("symbol") or "")) == _asset_key(order.symbol)
                    and str(intent.get("position_intent") or "").strip().lower()
                    in REDUCING_POSITION_INTENTS
                    and prior.state
                    not in {JournalState.RECONCILED, JournalState.REJECTED}
                ):
                    # A stale broker position after a prior close can otherwise
                    # make a new client-order ID cross zero and create reverse
                    # exposure.  Only a durable account reconciliation (or a
                    # definitive no-effect rejection) releases this symbol.
                    return "unreconciled_reducing_order_exists"
        except (OrderJournalError, OSError, ValueError):
            return "durable_order_journal_unavailable"

        try:
            positions = self.read_positions()
            open_orders = self.list_orders(status="open")
        except Exception:
            return "reducing_snapshot_unavailable"

        symbol_key = _asset_key(order.symbol)
        matching_positions = [position for position in positions if _asset_key(position.symbol) == symbol_key]
        if len(matching_positions) != 1:
            return "reducing_position_missing" if not matching_positions else "reducing_position_ambiguous"
        position_quantity = matching_positions[0].quantity
        if abs(position_quantity) <= 1e-12:
            return "reducing_position_zero"

        expected_side = "sell" if position_quantity > 0 else "buy"
        if order.side.lower() != expected_side:
            return "reducing_side_would_increase_position"
        if order.quantity is None:
            return "reducing_order_requires_quantity"

        pending_reduction = 0.0
        for open_order in open_orders:
            if _asset_key(open_order.symbol) != symbol_key or open_order.side.lower() != expected_side:
                continue
            if open_order.client_order_id == order.client_order_id:
                continue
            if open_order.quantity is None:
                return "reducing_open_order_quantity_unknown"
            quantity = open_order.quantity
            pending_reduction += max(0.0, quantity - open_order.filled_quantity)
        available = max(0.0, abs(position_quantity) - pending_reduction)
        if order.quantity > available + 1e-9:
            return "reducing_quantity_exceeds_position"
        if order.position_intent.strip().lower() == "close" and abs(order.quantity - available) > 1e-9:
            return "close_quantity_must_match_position"
        return None

    def _price_sanity_rejection_reason(self, order: PaperOrder) -> str | None:
        if is_crypto_symbol(order.symbol):
            if self._crypto_market_data is None:
                return "market_data_unavailable"
        else:
            if self._market_data is None:
                return "market_data_unavailable"
        reference_price = _positive_finite_number(order.reference_price)
        if reference_price is None:
            return "price_sanity_reference_missing"
        max_deviation = _bounded_fraction(self._risk_limits.max_price_deviation_pct)
        if max_deviation is None:
            return "price_sanity_policy_invalid"
        live_price = self._read_latest_trade_price(order.symbol.upper())
        if live_price is None:
            return "price_sanity_unavailable"
        deviation = abs(live_price - reference_price) / reference_price
        if not math.isfinite(deviation) or deviation > max_deviation:
            return "price_sanity_band_exceeded"
        return None

    def _read_latest_trade_price(self, symbol: str) -> float | None:
        market_data = self._crypto_market_data if is_crypto_symbol(symbol) else self._market_data
        if market_data is None:
            return None
        if is_crypto_symbol(symbol):
            request = _build_crypto_latest_trade_request(symbol)
            try:
                require_audited_alpaca_http_transport(market_data)
                response = market_data.get_crypto_latest_trade(request)
            except Exception:
                return None
        else:
            request = _build_latest_trade_request(symbol)
            try:
                require_audited_alpaca_http_transport(market_data)
                response = market_data.get_stock_latest_trade(request)
            except Exception:
                return None
        if not hasattr(response, "values"):
            return None
        trade = next(iter(response.values()), None)
        if trade is None:
            return None
        price = _get_attr(trade, "price", None)
        return _positive_finite_number(price)

    def _lookup_existing_order(self, client_order_id: str) -> Any:
        """Return a broker order or propagate a distinguishable lookup error.

        In particular, timeout/unavailability must never collapse into "not
        found" because that would authorize a second POST after an ambiguous
        first submission.
        """

        if not client_order_id or self._client is None or not hasattr(self._client, "get_order_by_client_id"):
            raise BrokerOrderLookupUnavailableError("broker client lookup by client_order_id is unavailable")
        return self._call_with_retry(lambda: self._client.get_order_by_client_id(client_order_id))

    def cancel_order(
        self,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
        execution_guard: Callable[[], None] | None = None,
    ) -> PaperOrderResult:
        if (client_order_id is None) == (order_id is None):
            return PaperOrderResult(False, "rejected", ("order_id_or_client_order_id_required",), self._dry_run)
        cancel_key = order_id if order_id is not None else client_order_id
        if self._dry_run:
            if str(cancel_key) in self._dry_run_cancelled_ids:
                return PaperOrderResult(True, "duplicate_dry_run_cancel_requested", (), True)
            self._dry_run_cancelled_ids.add(str(cancel_key))
            return PaperOrderResult(True, "dry_run_cancel_requested", (), True)
        if self._client is None:
            return PaperOrderResult(False, "rejected", ("broker_client_missing",), False)
        try:
            journal = self._ensure_order_journal()
        except (OrderJournalError, OSError, ValueError):
            return PaperOrderResult(False, "rejected", ("durable_order_journal_unavailable",), False)

        try:
            snapshot = (
                self.get_order(order_id=str(order_id))
                if order_id is not None
                else self.get_order_by_client_id(str(client_order_id))
            )
        except Exception as exc:
            return PaperOrderResult(
                False,
                "cancel_unresolved",
                ("broker_order_lookup_unavailable", type(exc).__name__),
                False,
            )

        broker_status = snapshot.status.lower()
        journal_key = snapshot.client_order_id or f"broker-{snapshot.order_id}"
        try:
            record = journal.get(journal_key)
            if record is None:
                record, _ = journal.record_intent(journal_key, _journal_intent_from_snapshot(snapshot))
            state = _journal_state_text(record.state)
            if state == "intent_recorded":
                observed_state = _journal_state_from_broker_status(broker_status)
                record, _ = journal.transition(
                    journal_key,
                    observed_state,
                    broker_order_id=snapshot.order_id or None,
                    expected_state=JournalState.INTENT_RECORDED,
                    metadata={"broker_status": broker_status, "source": "cancel_recovery"},
                )
                state = _journal_state_text(record.state)
        except (OrderJournalError, UnknownBrokerOrderStatusError):
            return PaperOrderResult(False, "cancel_unresolved", ("cancel_journal_transition_failed",), False)

        if broker_status in TERMINAL_ORDER_STATUSES:
            accepted = broker_status == "canceled"
            return PaperOrderResult(
                accepted,
                f"cancel_terminal_{broker_status}",
                () if accepted else (f"broker_terminal_{broker_status}",),
                False,
            )
        if broker_status == "pending_cancel":
            return PaperOrderResult(False, "cancel_pending", ("cancel_already_pending",), False)

        if state in {"cancel_requested", "cancel_unresolved"}:
            return PaperOrderResult(
                False,
                "cancel_pending" if state == "cancel_requested" else "cancel_unresolved",
                (
                    "cancel_already_requested"
                    if state == "cancel_requested"
                    else "cancel_outcome_requires_reconciliation"
                ,),
                False,
            )

        dispatch_claimed = False

        def claim_immediately_before_cancel() -> None:
            nonlocal dispatch_claimed
            fresh = self.get_order(order_id=snapshot.order_id)
            fresh_status = fresh.status.lower()
            if fresh_status in TERMINAL_ORDER_STATUSES or fresh_status == "pending_cancel":
                raise _CancelPreflightResolved(fresh)
            current = journal.get(journal_key)
            if current is None:
                raise InvalidOrderTransitionError("cancel journal intent disappeared")
            current_state = _journal_state_text(current.state)
            if current_state in {"cancel_requested", "cancel_unresolved"}:
                raise _CancelPreflightResolved(fresh)
            if execution_guard is not None:
                try:
                    execution_guard()
                except Exception as exc:
                    raise _ExecutionGuardRejected(
                        "executor dispatch guard rejected cancel"
                    ) from exc
            transitioned, _changed = journal.transition(
                journal_key,
                JournalState.CANCEL_REQUESTED,
                broker_order_id=fresh.order_id or None,
                expected_state=current.state,
                metadata={
                    "broker_status": fresh_status,
                    "inside_account_lease": _accepts_explicit_keyword(
                        self._client.cancel_order_by_id,
                        "before_dispatch",
                    ),
                },
            )
            journal.record_marker(
                journal_key,
                "cancel_dispatch_attempted",
                expected_state=transitioned.state,
                metadata={"broker_status_before_delete": fresh_status},
            )
            dispatch_claimed = True

        try:
            cancel_method = self._client.cancel_order_by_id
            if _accepts_explicit_keyword(cancel_method, "before_dispatch"):
                response = cancel_method(
                    snapshot.order_id,
                    before_dispatch=claim_immediately_before_cancel,
                )
            else:
                claim_immediately_before_cancel()
                response = cancel_method(snapshot.order_id)
        except _CancelPreflightResolved as exc:
            fresh = exc.snapshot
            fresh_status = str(fresh.status).lower()
            if fresh_status in TERMINAL_ORDER_STATUSES:
                accepted = fresh_status == "canceled"
                return PaperOrderResult(
                    accepted,
                    f"cancel_terminal_{fresh_status}",
                    () if accepted else (f"broker_terminal_{fresh_status}",),
                    False,
                )
            return PaperOrderResult(
                False,
                "cancel_pending" if fresh_status == "pending_cancel" else "cancel_unresolved",
                (
                    "cancel_already_pending"
                    if fresh_status == "pending_cancel"
                    else "cancel_outcome_requires_reconciliation"
                ,),
                False,
            )
        except _ExecutionGuardRejected:
            if not self._record_not_dispatched(
                journal,
                client_order_id=journal_key,
                operation="cancel",
                expected_state=record.state,
                reason="execution_guard_rejected",
                error_type="_ExecutionGuardRejected",
            ):
                return PaperOrderResult(
                    False,
                    "cancel_unresolved",
                    ("cancel_not_dispatched_marker_failed",),
                    False,
                )
            return _not_dispatched_result(
                operation="cancel_order",
                status="cancel_deferred",
                reason="execution_guard_rejected",
            )
        except AccountLeaseBusyError as exc:
            if paper_account_dispatch_not_started(exc, operation="cancel"):
                if not self._record_not_dispatched(
                    journal,
                    client_order_id=journal_key,
                    operation="cancel",
                    expected_state=record.state,
                    reason="account_mutation_busy",
                    error_type=type(exc).__name__,
                ):
                    return PaperOrderResult(
                        False,
                        "cancel_unresolved",
                        ("cancel_not_dispatched_marker_failed",),
                        False,
                    )
                return _not_dispatched_result(
                    operation="cancel_order",
                    status="cancel_deferred",
                    reason="account_mutation_busy",
                )
            if not dispatch_claimed:
                return PaperOrderResult(
                    False,
                    "cancel_unresolved",
                    ("pre_dispatch_outcome_unproven",),
                    False,
                )
            self._transition_best_effort(
                journal_key,
                "cancel_unresolved",
                metadata={"reason": "account_lease_lost_after_cancel_claim"},
            )
            return PaperOrderResult(False, "cancel_unresolved", ("cancel_request_ambiguous",), False)
        except Exception as exc:
            if paper_account_dispatch_not_started(exc, operation="cancel"):
                cause = (
                    exc.__cause__
                    if isinstance(exc, PaperAccountDispatchNotStartedError) and exc.__cause__ is not None
                    else exc
                )
                if not self._record_not_dispatched(
                    journal,
                    client_order_id=journal_key,
                    operation="cancel",
                    expected_state=record.state,
                    reason="paper_account_pre_dispatch_failed",
                    error_type=type(cause).__name__,
                ):
                    return PaperOrderResult(
                        False,
                        "cancel_unresolved",
                        ("cancel_not_dispatched_marker_failed",),
                        False,
                    )
                return _not_dispatched_result(
                    operation="cancel_order",
                    status="cancel_deferred",
                    reason="paper_account_pre_dispatch_failed",
                )
            if dispatch_claimed:
                self._transition_best_effort(
                    journal_key,
                    "cancel_unresolved",
                    metadata={"reason": "cancel_request_ambiguous", "error_type": type(exc).__name__},
                )
                return PaperOrderResult(False, "cancel_unresolved", ("cancel_request_ambiguous",), False)
            return PaperOrderResult(False, "cancel_unresolved", ("cancel_preflight_failed",), False)
        try:
            journal.record_marker(
                journal_key,
                "cancel_request_accepted",
                expected_state=JournalState.CANCEL_REQUESTED,
                metadata={"broker_status_before_delete": broker_status},
            )
        except OrderJournalError:
            return PaperOrderResult(False, "cancel_unresolved", ("cancel_journal_transition_failed",), False)
        return PaperOrderResult(True, "cancel_requested", (), False, response)

    def reconcile_positions(self, expected_positions: tuple[PaperPosition, ...]) -> ReconciliationReport:
        broker_positions = self.read_positions()
        broker_by_symbol = {position.symbol: position for position in broker_positions}
        expected_by_symbol = {position.symbol.upper(): position for position in expected_positions}
        differences: list[str] = []

        for symbol, expected in sorted(expected_by_symbol.items()):
            broker_position = broker_by_symbol.get(symbol)
            if broker_position is None:
                differences.append(f"missing_broker_position: {symbol}")
                continue
            if abs(broker_position.quantity - expected.quantity) > 1e-9:
                differences.append(
                    f"quantity_mismatch: {symbol} expected={expected.quantity} broker={broker_position.quantity}"
                )

        for symbol in sorted(set(broker_by_symbol) - set(expected_by_symbol)):
            differences.append(f"unexpected_broker_position: {symbol}")

        return ReconciliationReport(
            matched=not differences,
            differences=tuple(differences),
            broker_positions=broker_positions,
            expected_positions=tuple(expected_positions),
        )


def evaluate_paper_preflight(
    *,
    signal: Any | None,
    client_order_id: str | None,
    open_orders: tuple[PaperOrderSnapshot, ...],
    positions: tuple[PaperPosition, ...],
    as_of_date: date,
    max_feature_age_days: int,
) -> PaperPreflightDecision:
    context = PaperPreflightContext(
        signal=signal,
        client_order_id=client_order_id,
        open_orders=open_orders,
        positions=positions,
        as_of_date=as_of_date,
        max_feature_age_days=max_feature_age_days,
    )
    reasons: list[str] = []

    if context.signal is None or str(_get_attr(context.signal, "action", "")).lower() != "buy":
        reasons.append("no_buy_signal")
        return PaperPreflightDecision(
            allowed=False,
            reasons=tuple(reasons),
            checked_at=context.as_of_date.isoformat(),
            max_feature_age_days=context.max_feature_age_days,
        )

    symbol = str(_get_attr(context.signal, "symbol", "")).upper()
    signal_date = _date_from_signal_timestamp(str(_get_attr(context.signal, "timestamp", "")))
    if signal_date is None or (context.as_of_date - signal_date).days > context.max_feature_age_days:
        reasons.append("stale_features")

    if any(order.symbol.upper() == symbol for order in context.open_orders):
        reasons.append("open_order_exists")

    if context.client_order_id and any(
        order.client_order_id == context.client_order_id for order in context.open_orders
    ):
        reasons.append("duplicate_client_order_id")

    if any(position.symbol.upper() == symbol for position in context.positions):
        reasons.append("position_exists")

    return PaperPreflightDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        checked_at=context.as_of_date.isoformat(),
        max_feature_age_days=context.max_feature_age_days,
    )


def _get_attr(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _date_from_signal_timestamp(timestamp: str) -> date | None:
    value = timestamp.strip()
    if len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _paper_order_snapshot_from_raw(order: Any) -> PaperOrderSnapshot:
    order_id = str(_get_attr(order, "id", ""))
    client_order_id = str(_get_attr(order, "client_order_id", ""))
    symbol = str(_get_attr(order, "symbol", "")).upper()
    side = _enum_text(_get_attr(order, "side", ""))
    order_type = _enum_text(_get_attr(order, "order_type", _get_attr(order, "type", "")))
    time_in_force = _enum_text(_get_attr(order, "time_in_force", ""))
    status = _enum_text(_get_attr(order, "status", ""))
    if not order_id or not client_order_id or not symbol:
        raise InvalidOrderSnapshotError("broker order identity is incomplete")
    if side not in {"buy", "sell"} or not order_type or not time_in_force:
        raise InvalidOrderSnapshotError("broker order routing fields are incomplete")
    # A replaced order points at a distinct broker order. Until the adapter
    # validates and follows that relationship, accepting it would lose the
    # identity chain used by the durable journal. Block it explicitly.
    if status not in TERMINAL_ORDER_STATUSES | NONTERMINAL_ORDER_STATUSES:
        raise InvalidOrderSnapshotError("broker order status is unsupported")
    notional = _order_optional_nonnegative_float(_get_attr(order, "notional", None), field="notional")
    quantity = _order_optional_nonnegative_float(_get_attr(order, "qty", None), field="quantity")
    filled_quantity = _order_required_nonnegative_float(
        _get_attr(order, "filled_qty", None),
        field="filled_quantity",
    )
    if quantity is not None and filled_quantity > quantity + 1e-9:
        raise InvalidOrderSnapshotError("broker filled quantity exceeds order quantity")
    filled_avg_price = _order_optional_nonnegative_float(
        _get_attr(order, "filled_avg_price", None),
        field="filled_avg_price",
    )
    stop_price = _order_optional_nonnegative_float(
        _get_attr(order, "stop_price", None),
        field="stop_price",
    )
    limit_price = _order_optional_nonnegative_float(
        _get_attr(order, "limit_price", None),
        field="limit_price",
    )
    realized_pnl = _order_optional_finite_float(
        _get_attr(order, "realized_pnl", _get_attr(order, "realized_pl", None)),
        field="realized_pnl",
    )
    return PaperOrderSnapshot(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        status=status,
        notional=notional,
        quantity=quantity,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
        submitted_at=str(_get_attr(order, "submitted_at", "")),
        created_at=str(_get_attr(order, "created_at", "")),
        updated_at=str(_get_attr(order, "updated_at", "")),
        expires_at=str(_get_attr(order, "expires_at", "")),
        stop_price=stop_price,
        limit_price=limit_price,
        filled_at=_timestamp_text(_get_attr(order, "filled_at", "")),
        realized_pnl=realized_pnl,
    )


def _paper_fill_activity_from_raw(activity: Any) -> PaperFillActivity:
    activity_id = str(_get_attr(activity, "id", ""))
    order_id = str(_get_attr(activity, "order_id", ""))
    symbol = str(_get_attr(activity, "symbol", "")).upper()
    side = _enum_text(_get_attr(activity, "side", ""))
    activity_type = _enum_text(_get_attr(activity, "type", ""))
    order_status = _enum_text(_get_attr(activity, "order_status", ""))
    if not activity_id or not order_id or not symbol:
        raise InvalidFillActivitySnapshotError("broker fill activity identity is incomplete")
    if side not in {"buy", "sell"}:
        raise InvalidFillActivitySnapshotError("broker fill activity side is invalid")
    if activity_type not in {"fill", "partial_fill"}:
        raise InvalidFillActivitySnapshotError("broker fill activity type is invalid")
    transaction_time = _timestamp_text(_get_attr(activity, "transaction_time", ""))
    try:
        parsed_time = datetime.fromisoformat(transaction_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidFillActivitySnapshotError(
            "broker fill activity transaction_time is invalid"
        ) from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise InvalidFillActivitySnapshotError(
            "broker fill activity transaction_time lacks timezone"
        )
    quantity = _fill_activity_positive_float(_get_attr(activity, "qty", None), field="quantity")
    price = _fill_activity_positive_float(_get_attr(activity, "price", None), field="price")
    cumulative_quantity = _fill_activity_positive_float(
        _get_attr(activity, "cum_qty", None),
        field="cumulative_quantity",
    )
    leaves_quantity = _fill_activity_nonnegative_float(
        _get_attr(activity, "leaves_qty", None),
        field="leaves_quantity",
    )
    if cumulative_quantity + 1e-12 < quantity:
        raise InvalidFillActivitySnapshotError(
            "broker fill activity cumulative quantity is below fill quantity"
        )
    return PaperFillActivity(
        activity_id=activity_id,
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        transaction_time=transaction_time,
        cumulative_quantity=cumulative_quantity,
        leaves_quantity=leaves_quantity,
        activity_type=activity_type,
        order_status=order_status,
    )


def _timestamp_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _fill_activity_positive_float(value: Any, *, field: str) -> float:
    result = _fill_activity_nonnegative_float(value, field=field)
    if result <= 0:
        raise InvalidFillActivitySnapshotError(
            f"broker fill activity {field} must be positive"
        )
    return result


def _fill_activity_nonnegative_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidFillActivitySnapshotError(
            f"broker fill activity {field} is missing or invalid"
        ) from None
    if not math.isfinite(result) or result < 0:
        raise InvalidFillActivitySnapshotError(
            f"broker fill activity {field} is invalid"
        )
    return result


def _enum_text(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    text = str(raw_value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("numeric value must be finite")
    return result


def _positive_finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _bounded_fraction(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 1 else None


def _account_finite_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidAccountSnapshotError(f"broker account {field} is invalid") from None
    if not math.isfinite(result):
        raise InvalidAccountSnapshotError(f"broker account {field} is not finite")
    return result


def _order_optional_nonnegative_float(value: Any, *, field: str) -> float | None:
    if value in {None, ""}:
        return None
    return _order_required_nonnegative_float(value, field=field)


def _order_required_nonnegative_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidOrderSnapshotError(f"broker order {field} is missing or invalid") from None
    if not math.isfinite(result) or result < 0:
        raise InvalidOrderSnapshotError(f"broker order {field} is invalid")
    return result


def _order_optional_finite_float(value: Any, *, field: str) -> float | None:
    if value in {None, ""}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidOrderSnapshotError(f"broker order {field} is invalid") from None
    if not math.isfinite(result):
        raise InvalidOrderSnapshotError(f"broker order {field} is not finite")
    return result


def _required_finite_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise InvalidPositionSnapshotError(f"broker position {field} is missing or invalid") from None
    if not math.isfinite(result):
        raise InvalidPositionSnapshotError(f"broker position {field} is not finite")
    return result


def _finite_float_or_default(value: Any, *, field: str, default: float = 0.0) -> float:
    if value in {None, ""}:
        return default
    return _required_finite_float(value, field=field)


def _optional_finite_float(value: Any, *, field: str) -> float | None:
    if value in {None, ""}:
        return None
    return _required_finite_float(value, field=field)


def _journal_state_text(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _journal_state_from_broker_status(status: str) -> JournalState:
    normalized = status.strip().lower()
    states = {
        "filled": JournalState.FILLED,
        "partially_filled": JournalState.PARTIALLY_FILLED,
        "pending_cancel": JournalState.CANCEL_REQUESTED,
        "canceled": JournalState.CANCELED,
        "expired": JournalState.EXPIRED,
        "rejected": JournalState.REJECTED,
    }
    if normalized in states:
        return states[normalized]
    if normalized in NONTERMINAL_ORDER_STATUSES:
        return JournalState.ACKNOWLEDGED
    raise UnknownBrokerOrderStatusError(f"unsupported broker order status: {normalized or 'missing'}")


def _journal_intent_from_order(*, symbol: str, order: PaperOrder) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": order.side.lower(),
        "quantity": order.quantity,
        "notional": order.notional,
        "order_type": order.order_type,
        "time_in_force": "gtc" if is_crypto_symbol(symbol) else "day",
        "limit_price": order.limit_price,
        "position_intent": order.position_intent.strip().lower(),
        "reference_price": order.reference_price,
    }


def _journal_intent_from_snapshot(snapshot: PaperOrderSnapshot) -> dict[str, object]:
    return {
        "symbol": snapshot.symbol,
        "side": snapshot.side,
        "quantity": snapshot.quantity,
        "notional": snapshot.notional,
        "order_type": snapshot.order_type,
        "time_in_force": snapshot.time_in_force,
        "limit_price": snapshot.limit_price,
        "position_intent": "external",
    }


def _result_from_terminal_journal_state(state: str) -> PaperOrderResult:
    accepted = state == "filled"
    return PaperOrderResult(
        accepted,
        f"journal_terminal_{state}",
        () if accepted else (f"order_already_{state}",),
        False,
    )


def _raw_order_matches_intent(raw_order: Any, *, order: PaperOrder, symbol: str) -> bool:
    raw_client_order_id = str(_get_attr(raw_order, "client_order_id", ""))
    raw_symbol = str(_get_attr(raw_order, "symbol", "")).upper()
    raw_side = _enum_text(_get_attr(raw_order, "side", ""))
    raw_type = _enum_text(_get_attr(raw_order, "order_type", _get_attr(raw_order, "type", "")))
    raw_time_in_force = _enum_text(_get_attr(raw_order, "time_in_force", ""))
    expected_time_in_force = "gtc" if is_crypto_symbol(symbol) else "day"
    if raw_client_order_id != order.client_order_id:
        return False
    if _asset_key(raw_symbol) != _asset_key(symbol):
        return False
    if (
        raw_side != order.side.lower()
        or raw_type != order.order_type
        or raw_time_in_force != expected_time_in_force
    ):
        return False
    if order.order_type == "limit":
        try:
            raw_limit_price = _optional_float(_get_attr(raw_order, "limit_price", None))
        except (TypeError, ValueError):
            return False
        if (
            order.limit_price is None
            or raw_limit_price is None
            or abs(raw_limit_price - order.limit_price) > 1e-9
        ):
            return False
    try:
        raw_quantity = _optional_float(_get_attr(raw_order, "qty", None))
        raw_notional = _optional_float(_get_attr(raw_order, "notional", None))
    except (TypeError, ValueError):
        return False
    if order.quantity is not None:
        return raw_quantity is not None and abs(raw_quantity - order.quantity) <= 1e-9
    if order.notional is not None:
        return raw_notional is not None and abs(raw_notional - order.notional) <= 1e-9
    return False


def _asset_key(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("-", "")


def _is_order_not_found_error(exc: BaseException) -> bool:
    return _exception_status_code(exc) == 404


def _build_get_orders_request(status: str) -> Any:
    normalized = status.strip().lower()
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
    except ImportError:
        return _PortableGetOrdersRequest(
            status=normalized if normalized in {"open", "closed", "all"} else "open",
            limit=ORDER_LIST_MAX_LIMIT,
        )
    statuses = {
        "open": QueryOrderStatus.OPEN,
        "closed": QueryOrderStatus.CLOSED,
        "all": QueryOrderStatus.ALL,
    }
    return GetOrdersRequest(
        status=statuses.get(normalized, QueryOrderStatus.OPEN),
        limit=ORDER_LIST_MAX_LIMIT,
    )


def _submit_broker_order(
    client: Any,
    *,
    symbol: str,
    order: PaperOrder,
    before_dispatch: Callable[[], None] | None = None,
) -> Any:
    payload: dict[str, object] = {
        "symbol": symbol,
        "side": order.side.lower(),
        "type": "market",
        "time_in_force": "gtc" if is_crypto_symbol(symbol) else "day",
        "client_order_id": order.client_order_id,
    }
    if order.quantity is not None:
        payload["qty"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional
    if order.order_type == "limit":
        payload["type"] = "limit"
        payload["limit_price"] = order.limit_price

    submit_order = client.submit_order
    if _accepts_explicit_keyword(submit_order, "before_dispatch"):
        request = payload if _accepts_keyword_orders(submit_order) else _build_alpaca_order_request(payload)
        return submit_order(request, before_dispatch=before_dispatch)
    if before_dispatch is not None:
        before_dispatch()
    if _accepts_keyword_orders(submit_order):
        return submit_order(**payload)
    return submit_order(_build_alpaca_order_request(payload))


# Backwards-compatible alias for callers that still import the historical name.
_submit_market_order = _submit_broker_order


def _build_latest_trade_request(symbol: str) -> Any:
    try:
        from alpaca.data.requests import StockLatestTradeRequest
    except ImportError:  # pragma: no cover - depends on optional package
        from types import SimpleNamespace

        return SimpleNamespace(symbol_or_symbols=symbol, feed="iex")
    # Pin the IEX feed: the default (SIP) needs a paid subscription, so on the
    # free paper tier the price-sanity quote fetch would raise and every order
    # would be rejected. IEX matches the governed market-data fetch feed.
    try:
        from alpaca.data.enums import DataFeed

        return StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    except ImportError:  # pragma: no cover - depends on optional package
        return StockLatestTradeRequest(symbol_or_symbols=symbol)


def _build_crypto_latest_trade_request(symbol: str) -> Any:
    """Build a ``CryptoLatestTradeRequest`` (no ``feed`` arg; endpoint is crypto-specific).

    Alpaca's crypto latest-trade endpoint uses ``CryptoLatestTradeRequest``. Unlike
    the stock endpoint, it does NOT take a ``feed`` keyword (the default feed
    is the only public crypto feed and is selected automatically).
    """
    try:
        from alpaca.data.requests import CryptoLatestTradeRequest

        return CryptoLatestTradeRequest(symbol_or_symbols=symbol)
    except ImportError:  # pragma: no cover - depends on optional package
        from types import SimpleNamespace

        return SimpleNamespace(symbol_or_symbols=symbol)


def _accepts_keyword_orders(submit_order: Any) -> bool:
    try:
        signature = inspect.signature(submit_order)
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())


def _accepts_explicit_keyword(callable_object: Any, name: str) -> bool:
    try:
        parameter = inspect.signature(callable_object).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _build_alpaca_order_request(payload: dict[str, object]) -> Any:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    except ImportError:
        return payload

    side = OrderSide.BUY if str(payload["side"]).lower() == "buy" else OrderSide.SELL
    time_in_force = (
        TimeInForce.GTC
        if str(payload.get("time_in_force", "day")).lower() == "gtc"
        else TimeInForce.DAY
    )
    request_kwargs: dict[str, object] = {
        "symbol": str(payload["symbol"]),
        "side": side,
        "time_in_force": time_in_force,
        "client_order_id": str(payload["client_order_id"]),
    }
    if "qty" in payload:
        request_kwargs["qty"] = payload["qty"]
    if "notional" in payload:
        request_kwargs["notional"] = payload["notional"]
    if str(payload.get("type", "market")).lower() == "limit":
        request_kwargs["limit_price"] = float(payload["limit_price"])
        return LimitOrderRequest(**request_kwargs)
    return MarketOrderRequest(**request_kwargs)
