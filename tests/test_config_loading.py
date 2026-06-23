import tempfile
import textwrap
import unittest
from pathlib import Path

from trading_ai.config import ConfigError, load_risk_config, load_universe_config


class ConfigLoadingTests(unittest.TestCase):
    def test_default_universe_contains_expected_etfs(self) -> None:
        universe = load_universe_config(Path("configs/universe.yml"))

        self.assertEqual(
            universe.symbols,
            ("SPY", "QQQ", "IWM", "TLT", "GLD", "XLK", "XLF", "XLE", "XLV", "XLI"),
        )

    def test_default_risk_config_keeps_live_trading_disabled(self) -> None:
        risk = load_risk_config(Path("configs/risk.yml"))

        self.assertFalse(risk.live_trading_allowed)
        self.assertEqual(risk.max_daily_loss_pct, 0.02)
        self.assertEqual(risk.max_drawdown_pct, 0.10)
        self.assertEqual(risk.max_gross_exposure, 1.0)
        self.assertEqual(risk.max_single_position, 0.30)
        self.assertEqual(risk.paper_notional_usd, 1.0)

    def test_universe_rejects_duplicate_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "universe.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    universe:
                      symbols: [SPY, SPY]
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "duplicate"):
                load_universe_config(path)

    def test_risk_config_rejects_live_trading_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "risk.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    risk_limits:
                      max_daily_loss_pct: 0.02
                      max_drawdown_pct: 0.10
                      max_gross_exposure: 1.0
                      max_single_position: 0.30
                      live_trading_allowed: true
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "live trading"):
                load_risk_config(path)

    def test_risk_config_rejects_fraction_above_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "risk.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    risk_limits:
                      max_daily_loss_pct: 0.02
                      max_drawdown_pct: 0.10
                      max_gross_exposure: 1.25
                      max_single_position: 0.30
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "max_gross_exposure"):
                load_risk_config(path)

    def test_risk_config_loads_custom_paper_notional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "risk.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    risk_limits:
                      max_daily_loss_pct: 0.02
                      max_drawdown_pct: 0.10
                      max_gross_exposure: 1.0
                      max_single_position: 0.30
                      paper_notional_usd: 1.5
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            risk = load_risk_config(path)

            self.assertEqual(risk.paper_notional_usd, 1.5)

    def test_risk_config_rejects_invalid_paper_notional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "risk.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    risk_limits:
                      max_daily_loss_pct: 0.02
                      max_drawdown_pct: 0.10
                      max_gross_exposure: 1.0
                      max_single_position: 0.30
                      paper_notional_usd: 0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "paper_notional_usd"):
                load_risk_config(path)

    def test_risk_config_rejects_single_position_above_gross_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "risk.yml"
            path.write_text(
                textwrap.dedent(
                    """
                    risk_limits:
                      max_daily_loss_pct: 0.02
                      max_drawdown_pct: 0.10
                      max_gross_exposure: 0.20
                      max_single_position: 0.30
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "max_single_position"):
                load_risk_config(path)


if __name__ == "__main__":
    unittest.main()
