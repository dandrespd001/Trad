"""Alpaca paper adapter with dry-run and risk-gate defaults."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from trading_ai.risk.policy import RiskLimits, evaluate_risk_state

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
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status_code, int) and status_code in _TRANSIENT_STATUS_CODES:
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in _TRANSIENT_MESSAGE_FRAGMENTS)


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


@dataclass(frozen=True)
class PaperOrderResult:
    accepted: bool
    status: str
    reasons: tuple[str, ...]
    dry_run: bool
    broker_response: Any | None = None


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    status: str
    cash: float
    equity: float
    buying_power: float


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    quantity: float
    market_value: float
    avg_entry_price: float = 0.0
    current_price: float = 0.0


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
    ) -> None:
        self._client = client
        self._allowlist = {symbol.upper() for symbol in allowlist}
        self._risk_limits = risk_limits
        self._dry_run = dry_run
        self._accepted_order_ids: set[str] = set()
        self._cancelled_order_ids: set[str] = set()
        self._kill_switch_active = False
        self._kill_switch_reason: str | None = None
        self._max_retries = max(0, max_retries)
        self._retry_base_delay = max(0.0, retry_base_delay)
        self._sleep = sleep

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
        return PaperAccount(
            account_id=str(_get_attr(account, "id", "")),
            status=_enum_text(_get_attr(account, "status", "")),
            cash=float(_get_attr(account, "cash", 0.0)),
            equity=float(_get_attr(account, "equity", 0.0)),
            buying_power=float(_get_attr(account, "buying_power", 0.0)),
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
        for position in raw_positions:
            symbol = str(_get_attr(position, "symbol", "")).upper()
            if symbol not in self._allowlist:
                continue
            positions.append(
                PaperPosition(
                    symbol=symbol,
                    quantity=float(_get_attr(position, "qty", 0.0)),
                    market_value=float(_get_attr(position, "market_value", 0.0)),
                    avg_entry_price=float(_get_attr(position, "avg_entry_price", 0.0) or 0.0),
                    current_price=float(_get_attr(position, "current_price", 0.0) or 0.0),
                )
            )
        return tuple(positions)

    def list_orders(self, *, status: str = "open") -> tuple[PaperOrderSnapshot, ...]:
        if self._dry_run or self._client is None:
            return ()
        client = self._client
        assert client is not None  # narrowed by the dry-run/None guard above
        raw_orders = self._call_with_retry(lambda: client.get_orders(filter=_build_get_orders_request(status)))
        return tuple(_paper_order_snapshot_from_raw(order) for order in raw_orders)

    def get_order(self, *, order_id: str) -> PaperOrderSnapshot:
        if self._dry_run or self._client is None:
            raise ValueError("dry-run broker does not have remote orders")
        return _paper_order_snapshot_from_raw(self._client.get_order_by_id(order_id))

    def get_order_by_client_id(self, client_order_id: str) -> PaperOrderSnapshot:
        if self._dry_run or self._client is None:
            raise ValueError("dry-run broker does not have remote orders")
        return _paper_order_snapshot_from_raw(self._client.get_order_by_client_id(client_order_id))

    def activate_kill_switch(self, reason: str) -> None:
        self._kill_switch_active = True
        self._kill_switch_reason = reason

    def reset_kill_switch(self) -> None:
        self._kill_switch_active = False
        self._kill_switch_reason = None

    def submit_order(self, order: PaperOrder) -> PaperOrderResult:
        symbol = order.symbol.upper()
        if self._kill_switch_active:
            return PaperOrderResult(False, "risk_rejected", ("kill_switch_active",), self._dry_run)
        if order.client_order_id in self._accepted_order_ids:
            return PaperOrderResult(True, "duplicate_accepted", (), self._dry_run)
        if symbol not in self._allowlist:
            return PaperOrderResult(False, "rejected", ("symbol_not_allowlisted",), self._dry_run)
        if order.side.lower() not in {"buy", "sell"}:
            return PaperOrderResult(False, "rejected", ("invalid_side",), self._dry_run)
        if not order.client_order_id:
            return PaperOrderResult(False, "rejected", ("missing_client_order_id",), self._dry_run)
        if (order.quantity is None) == (order.notional is None):
            return PaperOrderResult(False, "rejected", ("quantity_or_notional_required",), self._dry_run)
        if order.quantity is not None and order.quantity <= 0:
            return PaperOrderResult(False, "rejected", ("invalid_quantity",), self._dry_run)
        if order.notional is not None and order.notional <= 0:
            return PaperOrderResult(False, "rejected", ("invalid_notional",), self._dry_run)

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

        self._accepted_order_ids.add(order.client_order_id)
        if self._dry_run:
            return PaperOrderResult(True, "dry_run_accepted", (), True)
        if self._client is None:
            return PaperOrderResult(False, "rejected", ("broker_client_missing",), False)
        response = self._call_with_retry(
            lambda: _submit_market_order(self._client, symbol=symbol, order=order),
            idempotency_check=lambda: self._lookup_existing_order(order.client_order_id),
        )
        return PaperOrderResult(True, "submitted", (), False, response)

    def _lookup_existing_order(self, client_order_id: str) -> Any | None:
        """Return the broker order matching ``client_order_id`` if it already exists.

        Used as the submit idempotency guard: if a submit times out after the
        broker accepted it, the retry resolves the existing order instead of
        sending a duplicate. Lookup failures degrade to ``None`` (retry proceeds).
        """

        if not client_order_id or self._client is None or not hasattr(self._client, "get_order_by_client_id"):
            return None
        try:
            return self._client.get_order_by_client_id(client_order_id)
        except Exception:
            return None

    def cancel_order(self, client_order_id: str | None = None, *, order_id: str | None = None) -> PaperOrderResult:
        if (client_order_id is None) == (order_id is None):
            return PaperOrderResult(False, "rejected", ("order_id_or_client_order_id_required",), self._dry_run)
        cancel_key = order_id if order_id is not None else client_order_id
        if cancel_key in self._cancelled_order_ids:
            return PaperOrderResult(True, "duplicate_cancelled", (), self._dry_run)
        self._cancelled_order_ids.add(str(cancel_key))
        if self._dry_run:
            return PaperOrderResult(True, "dry_run_cancelled", (), True)
        if self._client is None:
            return PaperOrderResult(False, "rejected", ("broker_client_missing",), False)
        broker_order_id = order_id
        if broker_order_id is None and client_order_id is not None and hasattr(self._client, "get_order_by_client_id"):
            broker_order_id = self.get_order_by_client_id(client_order_id).order_id
        elif broker_order_id is None:
            broker_order_id = client_order_id
        return PaperOrderResult(True, "cancelled", (), False, self._client.cancel_order_by_id(str(broker_order_id)))

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
    return PaperOrderSnapshot(
        order_id=str(_get_attr(order, "id", "")),
        client_order_id=str(_get_attr(order, "client_order_id", "")),
        symbol=str(_get_attr(order, "symbol", "")).upper(),
        side=_enum_text(_get_attr(order, "side", "")),
        order_type=_enum_text(_get_attr(order, "order_type", _get_attr(order, "type", ""))),
        time_in_force=_enum_text(_get_attr(order, "time_in_force", "")),
        status=_enum_text(_get_attr(order, "status", "")),
        notional=_optional_float(_get_attr(order, "notional", None)),
        quantity=_optional_float(_get_attr(order, "qty", None)),
        filled_quantity=float(_get_attr(order, "filled_qty", 0.0) or 0.0),
        filled_avg_price=_optional_float(_get_attr(order, "filled_avg_price", None)),
        submitted_at=str(_get_attr(order, "submitted_at", "")),
        created_at=str(_get_attr(order, "created_at", "")),
        updated_at=str(_get_attr(order, "updated_at", "")),
        expires_at=str(_get_attr(order, "expires_at", "")),
    )


def _enum_text(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    text = str(raw_value)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    return float(value)


def _build_get_orders_request(status: str) -> Any | None:
    normalized = status.strip().lower()
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
    except ImportError:
        return None
    statuses = {
        "open": QueryOrderStatus.OPEN,
        "closed": QueryOrderStatus.CLOSED,
        "all": QueryOrderStatus.ALL,
    }
    return GetOrdersRequest(status=statuses.get(normalized, QueryOrderStatus.OPEN))


def _submit_market_order(client: Any, *, symbol: str, order: PaperOrder) -> Any:
    payload: dict[str, object] = {
        "symbol": symbol,
        "side": order.side.lower(),
        "type": "market",
        "time_in_force": "day",
        "client_order_id": order.client_order_id,
    }
    if order.quantity is not None:
        payload["qty"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional

    submit_order = client.submit_order
    if _accepts_keyword_orders(submit_order):
        return submit_order(**payload)
    return submit_order(_build_alpaca_order_request(payload))


def _accepts_keyword_orders(submit_order: Any) -> bool:
    try:
        signature = inspect.signature(submit_order)
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())


def _build_alpaca_order_request(payload: dict[str, object]) -> Any:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
    except ImportError:
        return payload

    side = OrderSide.BUY if str(payload["side"]).lower() == "buy" else OrderSide.SELL
    request_kwargs: dict[str, object] = {
        "symbol": str(payload["symbol"]),
        "side": side,
        "time_in_force": TimeInForce.DAY,
        "client_order_id": str(payload["client_order_id"]),
    }
    if "qty" in payload:
        request_kwargs["qty"] = payload["qty"]
    if "notional" in payload:
        request_kwargs["notional"] = payload["notional"]
    return MarketOrderRequest(**request_kwargs)
