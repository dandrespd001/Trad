import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from trading_ai import config as config_module
from trading_ai.config import (
    ConfigError,
    load_risk_config,
    load_risk_config_bytes,
    load_universe_config,
    load_universe_config_bytes,
    load_yaml_bytes,
)


class ConfigLoadingTests(unittest.TestCase):
    def test_immutable_byte_loaders_match_path_loaders(self) -> None:
        risk_path = Path("configs/risk.yml")
        universe_path = Path("configs/universe.yml")

        self.assertEqual(
            load_risk_config_bytes(risk_path.read_bytes()),
            load_risk_config(risk_path, allow_live=False),
        )
        self.assertEqual(
            load_universe_config_bytes(universe_path.read_bytes()),
            load_universe_config(universe_path),
        )

    def test_immutable_byte_loader_rejects_ambiguous_or_invalid_inputs(self) -> None:
        invalid_payloads = (
            b"",
            b"\xff",
            b"- not\n- a\n- mapping\n",
            b"universe:\n  symbols: [SPY]\n  symbols: [QQQ]\n",
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ConfigError):
                load_yaml_bytes(payload)

        with self.assertRaisesRegex(ConfigError, "live trading"):
            load_risk_config_bytes(
                b"risk_limits:\n"
                b"  max_daily_loss_pct: 0.02\n"
                b"  max_drawdown_pct: 0.10\n"
                b"  max_gross_exposure: 1.0\n"
                b"  max_single_position: 0.30\n"
                b"  live_trading_allowed: true\n"
            )

    def test_risk_config_rejects_duplicate_keys(self) -> None:
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
                      live_trading_allowed: false
                      live_trading_allowed: true
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "duplicate configuration key"):
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_nonfinite_boolean_and_text_numbers(self) -> None:
        for raw_value in (".nan", ".inf", "-.inf", "true", '"0.02"'):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "risk.yml"
                path.write_text(
                    textwrap.dedent(
                        f"""
                        risk_limits:
                          max_daily_loss_pct: {raw_value}
                          max_drawdown_pct: 0.10
                          max_gross_exposure: 1.0
                          max_single_position: 0.30
                          live_trading_allowed: false
                        """
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ConfigError, "max_daily_loss_pct"):
                    load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_noninteger_count_values(self) -> None:
        for raw_value in ("1.5", "true", '"3"'):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "risk.yml"
                path.write_text(
                    textwrap.dedent(
                        f"""
                        risk_limits:
                          max_daily_loss_pct: 0.02
                          max_drawdown_pct: 0.10
                          max_gross_exposure: 1.0
                          max_single_position: 0.30
                          max_buy_signals: {raw_value}
                          live_trading_allowed: false
                        """
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ConfigError, "max_buy_signals"):
                    load_risk_config(path, allow_live=False)

    def test_default_universe_contains_expected_etfs(self) -> None:
        universe = load_universe_config(Path("configs/universe.yml"))

        self.assertEqual(
            universe.symbols,
            ("SPY", "QQQ", "IWM", "TLT", "GLD", "XLK", "XLF", "XLE", "XLV", "XLI"),
        )

    def test_default_risk_config_keeps_live_trading_disabled(self) -> None:
        risk = load_risk_config(Path("configs/risk.yml"), allow_live=False)

        self.assertFalse(risk.live_trading_allowed)
        self.assertEqual(risk.max_daily_loss_pct, 0.02)
        self.assertEqual(risk.max_drawdown_pct, 0.10)
        self.assertEqual(risk.max_gross_exposure, 1.0)
        self.assertEqual(risk.max_single_position, 0.02)  # operator goal 2026-07-08: max 2% of capital per trade
        self.assertEqual(risk.paper_notional_usd, 1.0)
        self.assertEqual(risk.min_signal_margin, 0.05)
        self.assertEqual(risk.max_buy_signals, 3)
        self.assertEqual(risk.max_consecutive_error_days, 3)

    def test_default_risk_config_keeps_breakeven_ratchet_disabled(self) -> None:
        risk = load_risk_config(Path("configs/risk.yml"), allow_live=False)

        self.assertEqual(risk.breakeven_trigger_atr_mult, 0.0)
        self.assertEqual(risk.breakeven_buffer_atr_mult, 0.0)

    def test_risk_config_loads_breakeven_ratchet_fields(self) -> None:
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
                      breakeven_trigger_atr_mult: 1.5
                      breakeven_buffer_atr_mult: 0.25
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            risk = load_risk_config(path, allow_live=False)

            self.assertEqual(risk.breakeven_trigger_atr_mult, 1.5)
            self.assertEqual(risk.breakeven_buffer_atr_mult, 0.25)

    def test_risk_config_rejects_negative_breakeven_trigger_atr_mult(self) -> None:
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
                      breakeven_trigger_atr_mult: -1.0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "breakeven_trigger_atr_mult"):
                load_risk_config(path, allow_live=False)

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

    def test_universe_rejects_nonstring_symbols_and_metadata(self) -> None:
        documents = (
            "universe:\n  symbols: [SPY, 123]\n",
            "universe:\n  symbols: [SPY]\n  market: 123\n",
        )
        for document in documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "universe.yml"
                path.write_text(document, encoding="utf-8")
                with self.assertRaises(ConfigError):
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
                load_risk_config(path, allow_live=False)

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
                load_risk_config(path, allow_live=False)

    def test_risk_config_loads_scale_up_paper_notional_with_reviewer_and_reason(self) -> None:
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
                      paper_stage: SCALE_UP
                      paper_stage_reviewer: reviewer@example.com
                      paper_stage_reason: 30 clean trial days
                      paper_notional_usd: 1.5
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            risk = load_risk_config(path, allow_live=False)

            self.assertEqual(risk.paper_notional_usd, 1.5)
            self.assertEqual(risk.paper_stage, "SCALE_UP")
            self.assertEqual(risk.paper_stage_reviewer, "reviewer@example.com")

    def test_risk_config_rejects_canary_notional_above_one(self) -> None:
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

            with self.assertRaisesRegex(ConfigError, "CANARY"):
                load_risk_config(path, allow_live=False)

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
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_invalid_signal_quality_limits(self) -> None:
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
                      min_signal_margin: -0.01
                      max_buy_signals: 0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "min_signal_margin"):
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_zero_max_buy_signals(self) -> None:
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
                      max_buy_signals: 0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "max_buy_signals"):
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_negative_max_consecutive_error_days(self) -> None:
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
                      max_consecutive_error_days: -1
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "max_consecutive_error_days"):
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_invalid_paper_stage(self) -> None:
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
                      paper_stage: LIVE
                      paper_notional_usd: 1.0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "paper_stage"):
                load_risk_config(path, allow_live=False)

    def test_risk_config_rejects_scale_up_without_reviewer_or_reason(self) -> None:
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
                      paper_stage: SCALE_UP
                      paper_notional_usd: 2.0
                      live_trading_allowed: false
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "paper_stage_reviewer"):
                load_risk_config(path, allow_live=False)

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
                load_risk_config(path, allow_live=False)


class LoadRiskConfigAllowLiveTests(unittest.TestCase):
    def test_allow_live_is_required(self) -> None:
        with self.assertRaises(TypeError):
            load_risk_config(Path("configs/risk.yml"))  # type: ignore[call-arg]

    def test_allow_live_true_writes_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self._risk_config(root, live_enabled=True)
            audit_path = root / "reports" / "tmp" / "live_bypass_audit.jsonl"

            with mock.patch.object(config_module, "LIVE_BYPASS_AUDIT_PATH", audit_path, create=True):
                risk = load_risk_config(path, allow_live=True)

            self.assertTrue(risk.live_trading_allowed)
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('"allow_live": true', lines[0])
            self.assertIn('"caller":', lines[0])
            self.assertIn('"path":', lines[0])
            self.assertIn('"timestamp":', lines[0])

    def test_allow_live_false_does_not_write_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = self._risk_config(root, live_enabled=False)
            audit_path = root / "reports" / "tmp" / "live_bypass_audit.jsonl"

            with mock.patch.object(config_module, "LIVE_BYPASS_AUDIT_PATH", audit_path, create=True):
                risk = load_risk_config(path, allow_live=False)

            self.assertFalse(risk.live_trading_allowed)
            self.assertFalse(audit_path.exists())

    def test_allow_live_false_rejects_live_enabled_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._risk_config(Path(temp_dir), live_enabled=True)

            with self.assertRaisesRegex(ConfigError, "live trading"):
                load_risk_config(path, allow_live=False)

    def test_live_flag_requires_a_yaml_boolean(self) -> None:
        for raw_value in ('"false"', '"true"', "0", "1", "null"):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                path = root / "risk.yml"
                path.write_text(
                    textwrap.dedent(
                        f"""
                        risk_limits:
                          max_daily_loss_pct: 0.02
                          max_drawdown_pct: 0.10
                          max_gross_exposure: 1.0
                          max_single_position: 0.30
                          live_trading_allowed: {raw_value}
                        """
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ConfigError, "live_trading_allowed"):
                    load_risk_config(path, allow_live=True)

    def test_allow_live_runtime_argument_requires_a_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "allow_live"):
            load_risk_config(
                Path("configs/risk.yml"),
                allow_live="false",  # type: ignore[arg-type]
            )

    def _risk_config(self, root: Path, *, live_enabled: bool) -> Path:
        path = root / "risk.yml"
        path.write_text(
            textwrap.dedent(
                f"""
                risk_limits:
                  max_daily_loss_pct: 0.02
                  max_drawdown_pct: 0.10
                  max_gross_exposure: 1.0
                  max_single_position: 0.30
                  live_trading_allowed: {str(live_enabled).lower()}
                """
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
