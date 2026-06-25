"""Position sizing for paper opens.

``fixed_notional`` keeps the governed CANARY behaviour (a constant USD notional).
``vol_target`` scales the position to a target annual volatility using the
existing :func:`trading_ai.research.metrics.volatility_target_weight`, capped by
the single-position weight and the stage notional cap.

Note: ``vol_target`` requires live account equity and the symbol's realized
volatility at *order-build* time. The current governance flow builds the order
intent offline (dry-run, equity unknown) and replays it with a hash-checked fixed
notional, so enabling ``vol_target`` end-to-end also requires moving sizing to
execute-time and relaxing the fixed-notional approval check. Until then this
helper returns the fixed notional whenever it cannot size safely, so wiring it in
is behaviour-preserving for CANARY.
"""

from __future__ import annotations

import logging

from trading_ai.research.metrics import volatility_target_weight

FIXED_NOTIONAL = "fixed_notional"
VOL_TARGET = "vol_target"

_log = logging.getLogger(__name__)


def compute_open_notional(
    *,
    sizing_mode: str,
    paper_notional_usd: float,
    account_equity: float = 0.0,
    realized_annual_volatility: float | None = None,
    target_volatility: float = 0.0,
    max_leverage: float = 1.0,
    max_single_position: float = 1.0,
    stage_cap_usd: float | None = None,
    simulated_equity_usd: float | None = None,
) -> float:
    """Return the USD notional for a new long position.

    Falls back to ``paper_notional_usd`` whenever ``vol_target`` cannot be applied
    (missing equity, missing/zero realized volatility, or no target volatility).

    ``simulated_equity_usd`` provides an explicit offline equity substitute for
    vol_target in dry-run / research mode. When account_equity <= 0 and
    simulated_equity_usd > 0, sizing uses the simulated value and logs a WARNING.
    When both are <= 0, sizing falls back to fixed_notional with a WARNING so the
    caller is aware the intended vol_target mode was not applied.
    """

    if sizing_mode != VOL_TARGET:
        return float(paper_notional_usd)

    effective_equity = account_equity
    if effective_equity <= 0:
        if simulated_equity_usd is not None and simulated_equity_usd > 0:
            _log.warning(
                "vol_target: account_equity=%.2f; using simulated_equity_usd=%.2f for sizing",
                account_equity,
                simulated_equity_usd,
            )
            effective_equity = simulated_equity_usd
        else:
            _log.warning(
                "vol_target: account_equity=%.2f and no simulated_equity_usd; "
                "falling back to fixed_notional=%.2f",
                account_equity,
                paper_notional_usd,
            )
            return float(paper_notional_usd)

    if (
        realized_annual_volatility is None
        or realized_annual_volatility <= 0
        or target_volatility <= 0
    ):
        return float(paper_notional_usd)
    weight = volatility_target_weight(
        realized_annual_volatility=realized_annual_volatility,
        target_annual_volatility=target_volatility,
        max_leverage=max_leverage,
    )
    if max_single_position > 0:
        weight = min(weight, max_single_position)
    notional = effective_equity * weight
    if stage_cap_usd is not None:
        notional = min(notional, float(stage_cap_usd))
    return max(0.0, notional)
