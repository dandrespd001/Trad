"""Tests for risk/policy.py — rewritten as pytest with parametrize."""
from __future__ import annotations

import unittest

try:
    import pytest
except ImportError:
    import pytest_shim as pytest

from trading_ai.risk.policy import RiskDecision, RiskLimits, evaluate_risk_state


def _eval(
    *,
    daily_pnl_pct: float = 0.0,
    current_drawdown_pct: float = 0.0,
    gross_exposure: float = 0.5,
    largest_position_weight: float = 0.10,
    mode: str = "paper",
    limits: RiskLimits | None = None,
) -> RiskDecision:
    return evaluate_risk_state(
        daily_pnl_pct=daily_pnl_pct,
        current_drawdown_pct=current_drawdown_pct,
        gross_exposure=gross_exposure,
        largest_position_weight=largest_position_weight,
        mode=mode,
        limits=limits or RiskLimits(),
    )


# ---------------------------------------------------------------------------
# Live mode is always blocked
# ---------------------------------------------------------------------------

def test_live_mode_is_blocked_by_default() -> None:
    decision = _eval(mode="live")
    assert not decision.allowed
    assert "live_trading_not_authorized" in decision.reasons
    assert "disable_trading" in decision.actions


# ---------------------------------------------------------------------------
# Daily loss gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("daily_pnl_pct", "expected_allowed", "expected_reason"),
    [
        (0.00, True, None),
        (-0.01, True, None),                       # within 2% limit
        (-0.019, True, None),                      # just under limit
        (-0.020, False, "daily_loss_limit_breached"),  # at limit (<=)
        (-0.030, False, "daily_loss_limit_breached"),  # beyond limit
    ],
    ids=["zero", "1pct", "1.9pct", "2pct-at-limit", "3pct-breached"],
)
def test_daily_loss_gate(
    daily_pnl_pct: float, expected_allowed: bool, expected_reason: str | None
) -> None:
    decision = _eval(daily_pnl_pct=daily_pnl_pct, limits=RiskLimits(max_daily_loss_pct=0.02))
    assert decision.allowed == expected_allowed
    if expected_reason:
        assert expected_reason in decision.reasons


# ---------------------------------------------------------------------------
# Drawdown gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("drawdown_pct", "expected_allowed", "expected_reason"),
    [
        (0.00, True, None),
        (0.05, True, None),                        # within 10% limit
        (0.099, True, None),                       # just under
        (0.100, False, "drawdown_limit_breached"), # at limit (>=)
        (0.120, False, "drawdown_limit_breached"), # beyond
    ],
    ids=["zero", "5pct", "9.9pct", "10pct-at-limit", "12pct-breached"],
)
def test_drawdown_gate(
    drawdown_pct: float, expected_allowed: bool, expected_reason: str | None
) -> None:
    decision = _eval(current_drawdown_pct=drawdown_pct, limits=RiskLimits(max_drawdown_pct=0.10))
    assert decision.allowed == expected_allowed
    if expected_reason:
        assert expected_reason in decision.reasons


# ---------------------------------------------------------------------------
# Exposure gates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("gross_exposure", "largest_weight", "expected_allowed", "expected_reasons"),
    [
        (0.70, 0.20, True, []),
        (1.00, 0.30, True, []),                    # at limits (not exceeded)
        (1.01, 0.20, False, ["gross_exposure_limit_breached"]),
        (0.70, 0.31, False, ["single_position_limit_breached"]),
        (1.25, 0.35, False, ["gross_exposure_limit_breached", "single_position_limit_breached"]),
    ],
    ids=["ok", "at-limits", "gross-exceeded", "single-exceeded", "both-exceeded"],
)
def test_exposure_gates(
    gross_exposure: float,
    largest_weight: float,
    expected_allowed: bool,
    expected_reasons: list[str],
) -> None:
    limits = RiskLimits(max_gross_exposure=1.0, max_single_position=0.30)
    decision = _eval(
        gross_exposure=gross_exposure,
        largest_position_weight=largest_weight,
        limits=limits,
    )
    assert decision.allowed == expected_allowed
    for reason in expected_reasons:
        assert reason in decision.reasons
    if expected_allowed:
        assert decision.actions == ["allow"]


# ---------------------------------------------------------------------------
# Combined breach — both loss and drawdown
# ---------------------------------------------------------------------------

def test_daily_loss_and_drawdown_breach_disable_trading() -> None:
    decision = _eval(
        daily_pnl_pct=-0.03,
        current_drawdown_pct=0.12,
        limits=RiskLimits(max_daily_loss_pct=0.02, max_drawdown_pct=0.10),
    )
    assert not decision.allowed
    assert "daily_loss_limit_breached" in decision.reasons
    assert "drawdown_limit_breached" in decision.reasons
    assert "disable_trading" in decision.actions


# ---------------------------------------------------------------------------
# Happy path — state inside limits
# ---------------------------------------------------------------------------

def test_paper_mode_allows_state_inside_limits() -> None:
    decision = _eval(daily_pnl_pct=-0.005, current_drawdown_pct=0.02)
    assert decision.allowed
    assert decision.reasons == []
    assert decision.actions == ["allow"]


# ---------------------------------------------------------------------------
# Unknown mode
# ---------------------------------------------------------------------------

def test_unknown_mode_blocks_trading() -> None:
    decision = _eval(mode="staging")
    assert not decision.allowed
    assert "unknown_trading_mode" in decision.reasons


# ---------------------------------------------------------------------------
# Deduplication — disable_trading appears once even with multiple breaches
# ---------------------------------------------------------------------------

def test_disable_trading_action_not_duplicated() -> None:
    decision = _eval(
        daily_pnl_pct=-0.05,
        current_drawdown_pct=0.15,
        limits=RiskLimits(max_daily_loss_pct=0.02, max_drawdown_pct=0.10),
    )
    assert decision.actions.count("disable_trading") == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
@pytest.mark.parametrize(
    "field",
    [
        "daily_pnl_pct",
        "current_drawdown_pct",
        "gross_exposure",
        "largest_position_weight",
    ],
)
def test_nonfinite_or_boolean_risk_state_fails_closed(field: str, value: object) -> None:
    kwargs = {field: value}
    decision = _eval(**kwargs)  # type: ignore[arg-type]

    assert not decision.allowed
    assert "risk_state_nonfinite" in decision.reasons
    assert "disable_trading" in decision.actions


@pytest.mark.parametrize(
    "limits",
    [
        RiskLimits(max_daily_loss_pct=float("nan")),
        RiskLimits(max_drawdown_pct=float("inf")),
        RiskLimits(max_gross_exposure=True),
        RiskLimits(max_single_position=-0.1),
        RiskLimits(max_gross_exposure=0.1, max_single_position=0.2),
        RiskLimits(live_trading_allowed="false"),  # type: ignore[arg-type]
    ],
)
def test_invalid_risk_limits_fail_closed(limits: RiskLimits) -> None:
    decision = _eval(limits=limits)

    assert not decision.allowed
    assert "invalid_risk_limits" in decision.reasons


class RiskPolicyUnittestCoverage(unittest.TestCase):
    """Ensure the stdlib release suite executes the critical risk cases."""

    def test_live_and_limit_boundaries(self) -> None:
        cases = (
            ({"mode": "live"}, "live_trading_not_authorized"),
            ({"daily_pnl_pct": -0.02}, "daily_loss_limit_breached"),
            ({"current_drawdown_pct": 0.10}, "drawdown_limit_breached"),
            ({"gross_exposure": 1.01}, "gross_exposure_limit_breached"),
            ({"largest_position_weight": 0.31}, "single_position_limit_breached"),
        )
        for kwargs, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                decision = _eval(**kwargs)  # type: ignore[arg-type]
                self.assertFalse(decision.allowed)
                self.assertIn(expected_reason, decision.reasons)

    def test_nonfinite_or_boolean_state_fails_closed(self) -> None:
        for field in (
            "daily_pnl_pct",
            "current_drawdown_pct",
            "gross_exposure",
            "largest_position_weight",
        ):
            for value in (float("nan"), float("inf"), float("-inf"), True):
                with self.subTest(field=field, value=value):
                    decision = _eval(**{field: value})  # type: ignore[arg-type]
                    self.assertFalse(decision.allowed)
                    self.assertIn("risk_state_nonfinite", decision.reasons)

    def test_invalid_limits_fail_closed(self) -> None:
        limits_cases = (
            RiskLimits(max_daily_loss_pct=float("nan")),
            RiskLimits(max_drawdown_pct=float("inf")),
            RiskLimits(max_gross_exposure=True),
            RiskLimits(max_single_position=-0.1),
            RiskLimits(max_gross_exposure=0.1, max_single_position=0.2),
            RiskLimits(live_trading_allowed="false"),  # type: ignore[arg-type]
        )
        for limits in limits_cases:
            with self.subTest(limits=limits):
                decision = _eval(limits=limits)
                self.assertFalse(decision.allowed)
                self.assertIn("invalid_risk_limits", decision.reasons)

    def test_positive_drawdown_convention_is_enforced(self) -> None:
        negative = _eval(current_drawdown_pct=-0.10)
        positive = _eval(current_drawdown_pct=0.10)

        self.assertFalse(negative.allowed)
        self.assertIn("drawdown_out_of_range", negative.reasons)
        self.assertFalse(positive.allowed)
        self.assertIn("drawdown_limit_breached", positive.reasons)
