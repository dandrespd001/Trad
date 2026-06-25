"""Tests for position_sizing.py — rewritten as pytest with parametrize."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trading_ai.config import ConfigError, load_risk_config
from trading_ai.execution.position_sizing import compute_open_notional


# ---------------------------------------------------------------------------
# fixed_notional mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("notional", "equity"),
    [
        (1.0, 0.0),
        (1.0, 10_000.0),
        (5.0, 10_000.0),
        (5.0, 0.0),
    ],
    ids=["fixed-1-no-equity", "fixed-1-with-equity", "fixed-5-with-equity", "fixed-5-no-equity"],
)
def test_fixed_notional_always_returns_notional(notional: float, equity: float) -> None:
    result = compute_open_notional(
        sizing_mode="fixed_notional",
        paper_notional_usd=notional,
        account_equity=equity,
        realized_annual_volatility=0.20,
        target_volatility=0.10,
    )
    assert result == pytest.approx(notional)


# ---------------------------------------------------------------------------
# vol_target mode — correct sizing
# ---------------------------------------------------------------------------

def test_vol_target_scales_to_target() -> None:
    # realized == target -> weight = 1.0, full equity used
    result = compute_open_notional(
        sizing_mode="vol_target",
        paper_notional_usd=1.0,
        account_equity=10_000.0,
        realized_annual_volatility=0.10,
        target_volatility=0.10,
        max_leverage=1.0,
        max_single_position=1.0,
    )
    assert result == pytest.approx(10_000.0)


def test_vol_target_capped_by_single_position() -> None:
    result = compute_open_notional(
        sizing_mode="vol_target",
        paper_notional_usd=1.0,
        account_equity=10_000.0,
        realized_annual_volatility=0.10,
        target_volatility=0.10,
        max_single_position=0.30,
    )
    assert result == pytest.approx(3_000.0)


def test_vol_target_capped_by_stage_cap() -> None:
    result = compute_open_notional(
        sizing_mode="vol_target",
        paper_notional_usd=1.0,
        account_equity=10_000.0,
        realized_annual_volatility=0.10,
        target_volatility=0.10,
        max_single_position=0.30,
        stage_cap_usd=5.0,
    )
    assert result == pytest.approx(5.0)


def test_vol_target_low_vol_capped_by_max_leverage() -> None:
    # realized 5% vs target 10% -> raw weight 2.0, capped at max_leverage=1.0
    result = compute_open_notional(
        sizing_mode="vol_target",
        paper_notional_usd=1.0,
        account_equity=10_000.0,
        realized_annual_volatility=0.05,
        target_volatility=0.10,
        max_leverage=1.0,
        max_single_position=1.0,
    )
    assert result == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# vol_target fallback cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("account_equity", "realized_vol", "target_vol", "description"),
    [
        (10_000.0, None, 0.10, "no-realized-vol"),
        (10_000.0, 0.0, 0.10, "zero-realized-vol"),
        (10_000.0, 0.10, 0.0, "zero-target-vol"),
    ],
    ids=["no-realized-vol", "zero-realized-vol", "zero-target-vol"],
)
def test_vol_target_falls_back_to_fixed_notional(
    account_equity: float,
    realized_vol: float | None,
    target_vol: float,
    description: str,
) -> None:
    result = compute_open_notional(
        sizing_mode="vol_target",
        paper_notional_usd=1.0,
        account_equity=account_equity,
        realized_annual_volatility=realized_vol,
        target_volatility=target_vol,
    )
    assert result == pytest.approx(1.0), f"Expected fallback to 1.0 for case: {description}"


def test_vol_target_no_equity_falls_back_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """vol_target with equity=0 and no simulated_equity falls back and logs a WARNING."""
    import logging

    with caplog.at_level(logging.WARNING, logger="trading_ai.execution.position_sizing"):
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=0.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
        )
    assert result == pytest.approx(1.0)
    assert any("fixed_notional" in r.message for r in caplog.records)


def test_vol_target_uses_simulated_equity_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """vol_target with equity=0 and simulated_equity_usd uses the simulated value."""
    import logging

    with caplog.at_level(logging.WARNING, logger="trading_ai.execution.position_sizing"):
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=0.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_leverage=1.0,
            max_single_position=1.0,
            simulated_equity_usd=10_000.0,
        )
    assert result == pytest.approx(10_000.0)
    assert any("simulated_equity_usd" in r.message for r in caplog.records)


def test_vol_target_simulated_equity_zero_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    """simulated_equity_usd=0 is treated as absent — still falls back."""
    import logging

    with caplog.at_level(logging.WARNING, logger="trading_ai.execution.position_sizing"):
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=0.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            simulated_equity_usd=0.0,
        )
    assert result == pytest.approx(1.0)
    assert any("fixed_notional" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Sizing config loading
# ---------------------------------------------------------------------------

def _write_risk_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "risk.yml"
    tmp.write_text(body, encoding="utf-8")
    return tmp


def _base_config(extra: str = "") -> str:
    return (
        "risk_limits:\n"
        "  max_daily_loss_pct: 0.02\n"
        "  max_drawdown_pct: 0.10\n"
        "  max_gross_exposure: 1.0\n"
        "  max_single_position: 0.30\n"
        "  paper_stage: CANARY\n"
        "  paper_notional_usd: 1.0\n"
        "  live_trading_allowed: false\n"
        f"{extra}"
    )


def test_default_sizing_mode_is_fixed_notional() -> None:
    limits = load_risk_config(_write_risk_config(_base_config()))
    assert limits.sizing_mode == "fixed_notional"


def test_invalid_sizing_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        load_risk_config(_write_risk_config(_base_config("  sizing_mode: martingale\n")))


def test_vol_target_requires_target_volatility() -> None:
    with pytest.raises(ConfigError):
        load_risk_config(_write_risk_config(_base_config("  sizing_mode: vol_target\n")))


def test_vol_target_with_target_volatility_loads() -> None:
    limits = load_risk_config(
        _write_risk_config(_base_config("  sizing_mode: vol_target\n  target_volatility: 0.1\n"))
    )
    assert limits.sizing_mode == "vol_target"
    assert limits.target_volatility == pytest.approx(0.1)
