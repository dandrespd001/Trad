"""Tests for position_sizing.py using stdlib unittest."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading_ai.config import ConfigError, load_risk_config
from trading_ai.execution.position_sizing import compute_open_notional


class PositionSizingTests(unittest.TestCase):
    def test_fixed_notional_always_returns_notional(self) -> None:
        cases = [
            (1.0, 0.0),
            (1.0, 10_000.0),
            (5.0, 10_000.0),
            (5.0, 0.0),
        ]
        for notional, equity in cases:
            with self.subTest(notional=notional, equity=equity):
                result = compute_open_notional(
                    sizing_mode="fixed_notional",
                    paper_notional_usd=notional,
                    account_equity=equity,
                    realized_annual_volatility=0.20,
                    target_volatility=0.10,
                )
                self.assertAlmostEqual(result, notional)

    def test_vol_target_scales_to_target(self) -> None:
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10_000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_leverage=1.0,
            max_single_position=1.0,
        )
        self.assertAlmostEqual(result, 10_000.0)

    def test_vol_target_capped_by_single_position(self) -> None:
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10_000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_single_position=0.30,
        )
        self.assertAlmostEqual(result, 3_000.0)

    def test_vol_target_capped_by_stage_cap(self) -> None:
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10_000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_single_position=0.30,
            stage_cap_usd=5.0,
        )
        self.assertAlmostEqual(result, 5.0)

    def test_vol_target_low_vol_capped_by_max_leverage(self) -> None:
        result = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10_000.0,
            realized_annual_volatility=0.05,
            target_volatility=0.10,
            max_leverage=1.0,
            max_single_position=1.0,
        )
        self.assertAlmostEqual(result, 10_000.0)

    def test_vol_target_falls_back_to_fixed_notional(self) -> None:
        cases = [
            (10_000.0, None, 0.10, "no-realized-vol"),
            (10_000.0, 0.0, 0.10, "zero-realized-vol"),
            (10_000.0, 0.10, 0.0, "zero-target-vol"),
        ]
        for account_equity, realized_vol, target_vol, description in cases:
            with self.subTest(description=description):
                result = compute_open_notional(
                    sizing_mode="vol_target",
                    paper_notional_usd=1.0,
                    account_equity=account_equity,
                    realized_annual_volatility=realized_vol,
                    target_volatility=target_vol,
                )
                self.assertAlmostEqual(result, 1.0)

    def test_vol_target_no_equity_falls_back_with_warning(self) -> None:
        with self.assertLogs("trading_ai.execution.position_sizing", level="WARNING") as captured:
            result = compute_open_notional(
                sizing_mode="vol_target",
                paper_notional_usd=1.0,
                account_equity=0.0,
                realized_annual_volatility=0.10,
                target_volatility=0.10,
            )
        self.assertAlmostEqual(result, 1.0)
        self.assertTrue(any("fixed_notional" in message for message in captured.output))

    def test_vol_target_uses_simulated_equity_with_warning(self) -> None:
        with self.assertLogs("trading_ai.execution.position_sizing", level="WARNING") as captured:
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
        self.assertAlmostEqual(result, 10_000.0)
        self.assertTrue(any("simulated_equity_usd" in message for message in captured.output))

    def test_vol_target_simulated_equity_zero_falls_back(self) -> None:
        with self.assertLogs("trading_ai.execution.position_sizing", level="WARNING") as captured:
            result = compute_open_notional(
                sizing_mode="vol_target",
                paper_notional_usd=1.0,
                account_equity=0.0,
                realized_annual_volatility=0.10,
                target_volatility=0.10,
                simulated_equity_usd=0.0,
            )
        self.assertAlmostEqual(result, 1.0)
        self.assertTrue(any("fixed_notional" in message for message in captured.output))

    def test_default_sizing_mode_is_fixed_notional(self) -> None:
        limits = load_risk_config(_write_risk_config(_base_config()), allow_live=False)
        self.assertEqual(limits.sizing_mode, "fixed_notional")

    def test_invalid_sizing_mode_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_risk_config(_write_risk_config(_base_config("  sizing_mode: martingale\n")), allow_live=False)

    def test_vol_target_requires_target_volatility(self) -> None:
        with self.assertRaises(ConfigError):
            load_risk_config(_write_risk_config(_base_config("  sizing_mode: vol_target\n")), allow_live=False)

    def test_vol_target_with_target_volatility_loads(self) -> None:
        limits = load_risk_config(
            _write_risk_config(_base_config("  sizing_mode: vol_target\n  target_volatility: 0.1\n")),
            allow_live=False,
        )
        self.assertEqual(limits.sizing_mode, "vol_target")
        self.assertAlmostEqual(limits.target_volatility, 0.1)


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


if __name__ == "__main__":
    unittest.main()
