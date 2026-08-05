"""Strict, signed execution-cost primitives.

Positive values always mean a cost to the strategy and negative values mean
price improvement or a rebate.  Inputs are converted through ``Decimal`` so a
binary floating-point artefact cannot flip a gate at a configured boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

_BPS = Decimal("10000")
_ZERO = Decimal("0")


class ExecutionCostEvidenceError(ValueError):
    """Raised when execution-cost evidence is missing or internally invalid."""


@dataclass(frozen=True)
class ExecutionCostComponents:
    side: str
    quantity: Decimal
    decision_mid: Decimal
    fill_price: Decimal
    decision_notional: Decimal
    price_shortfall_usd: Decimal
    fee_cost_usd: Decimal
    opportunity_cost_usd: Decimal
    total_shortfall_usd: Decimal
    implementation_shortfall_bps: Decimal
    gap_cost_usd: Decimal | None
    latency_price_cost_usd: Decimal | None
    effective_spread_cost_usd: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "quantity": float(self.quantity),
            "decision_mid": float(self.decision_mid),
            "fill_price": float(self.fill_price),
            "decision_notional": float(self.decision_notional),
            "price_shortfall_usd": float(self.price_shortfall_usd),
            "fee_cost_usd": float(self.fee_cost_usd),
            "opportunity_cost_usd": float(self.opportunity_cost_usd),
            "total_shortfall_usd": float(self.total_shortfall_usd),
            "implementation_shortfall_bps": float(self.implementation_shortfall_bps),
            "gap_cost_usd": _float_or_none(self.gap_cost_usd),
            "latency_price_cost_usd": _float_or_none(self.latency_price_cost_usd),
            "effective_spread_cost_usd": _float_or_none(self.effective_spread_cost_usd),
        }


def signed_price_cost_bps(*, side: object, benchmark_price: object, execution_price: object) -> Decimal:
    """Return side-aware price cost in bps; adverse execution is positive."""

    direction = _side_direction(side)
    benchmark = _positive_decimal(benchmark_price, field="benchmark_price")
    execution = _positive_decimal(execution_price, field="execution_price")
    return direction * (execution - benchmark) / benchmark * _BPS


def fee_cost_from_net_amount(net_amount: object) -> Decimal:
    """Convert broker cash-flow sign into strategy-cost sign.

    Broker fee debits are negative cash movements, therefore ``-1.25`` maps
    to a positive cost of ``1.25``.  A positive rebate remains a negative cost.
    """

    return -_finite_decimal(net_amount, field="net_amount")


def execution_cost_components(
    *,
    side: object,
    quantity: object,
    decision_mid: object,
    fill_price: object,
    arrival_mid: object | None = None,
    fill_mid: object | None = None,
    fee_cost_usd: object = 0,
    unfilled_quantity: object = 0,
    horizon_price: object | None = None,
) -> ExecutionCostComponents:
    """Calculate signed implementation shortfall and optional decomposition.

    The decomposition is available only when both ``arrival_mid`` and
    ``fill_mid`` are supplied.  Opportunity cost requires a horizon price when
    the unfilled quantity is non-zero.  No missing component is silently
    treated as zero.
    """

    direction = _side_direction(side)
    normalized_side = str(side).strip().lower()
    qty = _positive_decimal(quantity, field="quantity")
    decision = _positive_decimal(decision_mid, field="decision_mid")
    fill = _positive_decimal(fill_price, field="fill_price")
    fee = _finite_decimal(fee_cost_usd, field="fee_cost_usd")
    unfilled = _nonnegative_decimal(unfilled_quantity, field="unfilled_quantity")

    opportunity = _ZERO
    if unfilled > 0:
        if horizon_price is None:
            raise ExecutionCostEvidenceError("horizon_price is required when unfilled_quantity is positive")
        horizon = _positive_decimal(horizon_price, field="horizon_price")
        opportunity = direction * unfilled * (horizon - decision)
    elif horizon_price is not None:
        _positive_decimal(horizon_price, field="horizon_price")

    gap: Decimal | None = None
    latency_price: Decimal | None = None
    spread: Decimal | None = None
    if (arrival_mid is None) != (fill_mid is None):
        raise ExecutionCostEvidenceError("arrival_mid and fill_mid must be supplied together")
    if arrival_mid is not None and fill_mid is not None:
        arrival = _positive_decimal(arrival_mid, field="arrival_mid")
        contemporaneous = _positive_decimal(fill_mid, field="fill_mid")
        gap = direction * qty * (arrival - decision)
        latency_price = direction * qty * (contemporaneous - arrival)
        spread = direction * qty * (fill - contemporaneous)

    price_shortfall = direction * qty * (fill - decision)
    if (
        gap is not None
        and latency_price is not None
        and spread is not None
        and gap + latency_price + spread != price_shortfall
    ):
        raise ExecutionCostEvidenceError("execution-cost decomposition does not add to price shortfall")

    total = price_shortfall + fee + opportunity
    decision_notional = qty * decision
    bps = total / decision_notional * _BPS
    return ExecutionCostComponents(
        side=normalized_side,
        quantity=qty,
        decision_mid=decision,
        fill_price=fill,
        decision_notional=decision_notional,
        price_shortfall_usd=price_shortfall,
        fee_cost_usd=fee,
        opportunity_cost_usd=opportunity,
        total_shortfall_usd=total,
        implementation_shortfall_bps=bps,
        gap_cost_usd=gap,
        latency_price_cost_usd=latency_price,
        effective_spread_cost_usd=spread,
    )


def execution_latency_ms(*, submitted_at: object, filled_at: object) -> int:
    submitted = _aware_datetime(submitted_at, field="submitted_at")
    filled = _aware_datetime(filled_at, field="filled_at")
    delta_ms = int((filled - submitted).total_seconds() * 1000)
    if delta_ms < 0:
        raise ExecutionCostEvidenceError("filled_at precedes submitted_at")
    return delta_ms


def summarize_signed_bps(values: list[object]) -> dict[str, object]:
    observations = sorted(_finite_decimal(value, field="cost_bps") for value in values)
    if not observations:
        return {
            "n": 0,
            "min": None,
            "median": None,
            "p90": None,
            "max": None,
            "adverse_n": 0,
            "favorable_n": 0,
            "zero_n": 0,
        }
    count = len(observations)
    midpoint = count // 2
    if count % 2:
        median = observations[midpoint]
    else:
        median = (observations[midpoint - 1] + observations[midpoint]) / Decimal("2")
    # Nearest-rank p90: ceil(0.9*n), expressed without a floating-point round.
    rank = max(1, (9 * count + 9) // 10)
    p90 = observations[rank - 1]
    return {
        "n": count,
        "min": float(observations[0]),
        "median": float(median),
        "p90": float(p90),
        "max": float(observations[-1]),
        "adverse_n": sum(value > 0 for value in observations),
        "favorable_n": sum(value < 0 for value in observations),
        "zero_n": sum(value == 0 for value in observations),
    }


def _side_direction(side: object) -> Decimal:
    normalized = str(side).strip().lower()
    if normalized == "buy":
        return Decimal("1")
    if normalized == "sell":
        return Decimal("-1")
    raise ExecutionCostEvidenceError("side must be buy or sell")


def _finite_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ExecutionCostEvidenceError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExecutionCostEvidenceError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ExecutionCostEvidenceError(f"{field} must be a finite number")
    return result


def _positive_decimal(value: object, *, field: str) -> Decimal:
    result = _finite_decimal(value, field=field)
    if result <= 0:
        raise ExecutionCostEvidenceError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: object, *, field: str) -> Decimal:
    result = _finite_decimal(value, field=field)
    if result < 0:
        raise ExecutionCostEvidenceError(f"{field} must be non-negative")
    return result


def _aware_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionCostEvidenceError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionCostEvidenceError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionCostEvidenceError(f"{field} must include a timezone")
    return parsed


def _float_or_none(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "ExecutionCostComponents",
    "ExecutionCostEvidenceError",
    "execution_cost_components",
    "execution_latency_ms",
    "fee_cost_from_net_amount",
    "signed_price_cost_bps",
    "summarize_signed_bps",
]
