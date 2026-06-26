"""Alpaca live adapter boundary with submit disabled until go-live."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from trading_ai.risk.policy import RiskLimits, evaluate_risk_state


@dataclass(frozen=True)
class LiveOrder:
    symbol: str
    side: str
    client_order_id: str
    notional: float | None = None
    quantity: float | None = None
    estimated_position_weight: float = 0.0
    projected_gross_exposure: float = 0.0
    daily_pnl_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    reference_price: float | None = None
    live_price: float | None = None
    max_price_deviation_pct: float = 0.05


@dataclass(frozen=True)
class LiveOrderResult:
    accepted: bool
    status: str
    reasons: tuple[str, ...]
    dry_run: bool
    broker_response: Any | None = None


class AlpacaLiveBroker:
    """Live broker boundary.

    The class validates live risk semantics, but does not submit orders before
    the later go-live sprint connects an explicit human-approved submit path.
    """

    def __init__(
        self,
        *,
        client: Any | None,
        allowlist: tuple[str, ...],
        risk_limits: RiskLimits,
        submit_enabled: bool = False,
        order_request_factory: Callable[[LiveOrder], Any] | None = None,
    ) -> None:
        self._client = client
        self._allowlist = {symbol.upper() for symbol in allowlist}
        self._risk_limits = risk_limits
        self._submit_enabled = submit_enabled
        self._order_request_factory = order_request_factory or _build_market_order_request

    def validate_order(self, order: LiveOrder) -> LiveOrderResult:
        reasons: list[str] = []
        symbol = order.symbol.upper()
        if symbol not in self._allowlist:
            reasons.append("symbol_not_allowlisted")
        if order.side.lower() not in {"buy", "sell"}:
            reasons.append("invalid_side")
        if order.notional is None and order.quantity is None:
            reasons.append("missing_notional_or_quantity")
        if order.notional is not None and order.notional <= 0:
            reasons.append("invalid_notional")
        if order.quantity is not None and order.quantity <= 0:
            reasons.append("invalid_quantity")
        if order.side.lower() == "buy":
            if order.reference_price is None:
                reasons.append("missing_reference_price")
            elif order.reference_price <= 0:
                reasons.append("invalid_reference_price")
            if order.live_price is None:
                reasons.append("missing_live_price")
            elif order.live_price <= 0:
                reasons.append("invalid_live_price")
            if (
                order.reference_price is not None
                and order.reference_price > 0
                and order.live_price is not None
                and order.live_price > 0
            ):
                deviation = abs(order.live_price - order.reference_price) / order.reference_price
                if deviation > order.max_price_deviation_pct:
                    reasons.append("price_sanity_failed")

        risk = evaluate_risk_state(
            daily_pnl_pct=order.daily_pnl_pct,
            current_drawdown_pct=order.current_drawdown_pct,
            gross_exposure=order.projected_gross_exposure,
            largest_position_weight=order.estimated_position_weight,
            mode="live",
            limits=self._risk_limits,
        )
        if not risk.allowed:
            reasons.extend(_normalize_live_risk_reason(reason) for reason in risk.reasons)
        if not self._risk_limits.live_trading_allowed:
            reasons.append("live_trading_not_allowed_by_risk_config")

        clean_reasons = _dedupe(reasons)
        return LiveOrderResult(
            accepted=not clean_reasons,
            status="validated" if not clean_reasons else "rejected",
            reasons=tuple(clean_reasons),
            dry_run=True,
        )

    def submit_order(self, order: LiveOrder) -> LiveOrderResult:
        validation = self.validate_order(order)
        reasons = list(validation.reasons)
        if not self._submit_enabled:
            reasons.append("live_submit_not_enabled")
        if self._client is None:
            reasons.append("live_client_missing")
        clean_reasons = _dedupe(reasons)
        if clean_reasons:
            return LiveOrderResult(
                accepted=False,
                status="rejected",
                reasons=tuple(clean_reasons),
                dry_run=True,
                broker_response=None,
            )
        try:
            order_request = self._order_request_factory(order)
            response = self._client.submit_order(order_request)
        except Exception:
            return LiveOrderResult(
                accepted=False,
                status="rejected",
                reasons=("live_submit_error",),
                dry_run=False,
                broker_response=None,
            )
        return LiveOrderResult(
            accepted=True,
            status="submitted",
            reasons=(),
            dry_run=False,
            broker_response=response,
        )


def _normalize_live_risk_reason(reason: str) -> str:
    if reason == "single_position_limit_breached":
        return "single_position_limit"
    if reason == "live_trading_not_authorized":
        return "live_trading_not_allowed_by_risk_config"
    return reason


def _build_market_order_request(order: LiveOrder) -> Any:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
    except ImportError:
        payload: dict[str, object] = {
            "symbol": order.symbol.upper(),
            "side": order.side.lower(),
            "client_order_id": order.client_order_id,
            "time_in_force": "day",
        }
        if order.notional is not None:
            payload["notional"] = float(order.notional)
        if order.quantity is not None:
            payload["qty"] = float(order.quantity)
        return payload

    kwargs: dict[str, object] = {
        "symbol": order.symbol.upper(),
        "side": OrderSide.BUY if order.side.lower() == "buy" else OrderSide.SELL,
        "time_in_force": TimeInForce.DAY,
        "client_order_id": order.client_order_id,
    }
    if order.notional is not None:
        kwargs["notional"] = float(order.notional)
    if order.quantity is not None:
        kwargs["qty"] = float(order.quantity)
    return MarketOrderRequest(**kwargs)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
