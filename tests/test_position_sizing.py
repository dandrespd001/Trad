import tempfile
import unittest
from pathlib import Path

from trading_ai.config import ConfigError, load_risk_config
from trading_ai.execution.position_sizing import compute_open_notional


class ComputeOpenNotionalTests(unittest.TestCase):
    def test_fixed_notional_ignores_volatility(self) -> None:
        notional = compute_open_notional(
            sizing_mode="fixed_notional",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=0.2,
            target_volatility=0.1,
        )
        self.assertEqual(notional, 1.0)

    def test_vol_target_scales_to_target(self) -> None:
        # realized 10% == target 10% -> full weight; equity 10000 -> 10000.
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_leverage=1.0,
            max_single_position=1.0,
        )
        self.assertAlmostEqual(notional, 10000.0)

    def test_vol_target_capped_by_single_position(self) -> None:
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_single_position=0.30,
        )
        self.assertAlmostEqual(notional, 3000.0)

    def test_vol_target_capped_by_stage_cap(self) -> None:
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
            max_single_position=0.30,
            stage_cap_usd=5.0,
        )
        self.assertAlmostEqual(notional, 5.0)

    def test_vol_target_low_vol_uses_leverage_cap(self) -> None:
        # realized 5% vs target 10% -> raw weight 2.0, capped at max_leverage 1.0.
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=0.05,
            target_volatility=0.10,
            max_leverage=1.0,
            max_single_position=1.0,
        )
        self.assertAlmostEqual(notional, 10000.0)

    def test_vol_target_falls_back_when_no_equity(self) -> None:
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=0.0,
            realized_annual_volatility=0.10,
            target_volatility=0.10,
        )
        self.assertEqual(notional, 1.0)

    def test_vol_target_falls_back_when_no_volatility(self) -> None:
        notional = compute_open_notional(
            sizing_mode="vol_target",
            paper_notional_usd=1.0,
            account_equity=10000.0,
            realized_annual_volatility=None,
            target_volatility=0.10,
        )
        self.assertEqual(notional, 1.0)


class SizingConfigTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "risk.yml"
        tmp.write_text(body, encoding="utf-8")
        return tmp

    def _base(self, extra: str) -> str:
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

    def test_default_is_fixed_notional(self) -> None:
        limits = load_risk_config(self._write(self._base("")))
        self.assertEqual(limits.sizing_mode, "fixed_notional")

    def test_invalid_sizing_mode_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_risk_config(self._write(self._base("  sizing_mode: martingale\n")))

    def test_vol_target_requires_target_volatility(self) -> None:
        with self.assertRaises(ConfigError):
            load_risk_config(self._write(self._base("  sizing_mode: vol_target\n")))

    def test_vol_target_with_target_volatility_loads(self) -> None:
        limits = load_risk_config(
            self._write(self._base("  sizing_mode: vol_target\n  target_volatility: 0.1\n"))
        )
        self.assertEqual(limits.sizing_mode, "vol_target")
        self.assertAlmostEqual(limits.target_volatility, 0.1)


if __name__ == "__main__":
    unittest.main()
