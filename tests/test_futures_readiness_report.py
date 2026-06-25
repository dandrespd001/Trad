import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class FuturesReadinessReportTests(unittest.TestCase):
    def test_parser_defaults_for_read_only_futures_readiness(self) -> None:
        args = build_parser().parse_args(["futures-readiness-report"])

        self.assertEqual(args.config, "configs/futures_micro.yml")
        self.assertEqual(args.output, "reports/tmp/futures_readiness/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/futures_readiness/latest.md")

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["futures-execute"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["futures-submit"])

    def test_valid_mes_mnq_fixture_produces_ok_report_without_live_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_futures_config(root / "futures.yml")
            output = root / "futures.json"

            exit_code = main(
                [
                    "futures-readiness-report",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "futures.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["platform_decision"]["selected"], "LEAN_IBKR_RESEARCH_ONLY")
        self.assertEqual(payload["platform_decision"]["status"], "DECIDED")
        self.assertTrue(payload["platform_decision"]["read_only"])
        self.assertEqual(payload["summary"]["contract_count"], 2)
        self.assertEqual(payload["summary"]["ready_contracts"], ["MES", "MNQ"])
        self.assertFalse(payload["permissions"]["live_trading_allowed"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["orders_enabled"])

    def test_missing_platform_decision_warns_without_contract_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_futures_config(root / "futures.yml", include_platform_decision=False)
            output = root / "futures.json"

            exit_code = main(
                [
                    "futures-readiness-report",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "futures.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["platform_decision"]["status"], "MISSING")
        self.assertIn("missing_platform_decision", payload["warnings"])
        self.assertEqual(payload["summary"]["ready_contracts"], ["MES", "MNQ"])

    def test_missing_calendar_roll_margin_or_costs_blocks_contract(self) -> None:
        cases = {
            "calendar": "calendar: null",
            "roll": "roll: null",
            "margin": "margin: null",
            "costs": "costs: null",
        }
        for field, replacement in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = write_futures_config(root / "futures.yml")
                text = config.read_text(encoding="utf-8")
                text = text.replace(f"      {field}: {{", f"      {replacement} # ")
                config.write_text(text, encoding="utf-8")
                output = root / "futures.json"

                exit_code = main(
                    [
                        "futures-readiness-report",
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                        "--markdown-output",
                        str(root / "futures.md"),
                    ]
                )
                payload = read_json(output)

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertIn(f"missing_{field}", {blocker["code"] for blocker in payload["blockers"]})


def write_futures_config(path: Path, *, include_platform_decision: bool = True) -> Path:
    platform_decision = (
        """
              platform_decision:
                selected: LEAN_IBKR_RESEARCH_ONLY
                rationale: research-only futures evaluation before execution
                alternatives: [ALPACA_ONLY, DEFER]
                read_only: true
        """
        if include_platform_decision
        else ""
    )
    path.write_text(
        textwrap.dedent(
            f"""
            permissions:
              live_trading_allowed: false
            futures:
{platform_decision.rstrip()}
              contracts:
                - symbol: MES
                  exchange: CME
                  name: Micro E-mini S&P 500
                  tick_size: 0.25
                  tick_value: 1.25
                  margin: {{placeholder_usd: 1500}}
                  calendar: {{timezone: America/New_York, session: equities_extended}}
                  roll: {{rule: quarterly_volume_placeholder}}
                  costs: {{commission_per_contract: 0.62, slippage_ticks: 1}}
                - symbol: MNQ
                  exchange: CME
                  name: Micro E-mini Nasdaq-100
                  tick_size: 0.25
                  tick_value: 0.50
                  margin: {{placeholder_usd: 1800}}
                  calendar: {{timezone: America/New_York, session: equities_extended}}
                  roll: {{rule: quarterly_volume_placeholder}}
                  costs: {{commission_per_contract: 0.62, slippage_ticks: 1}}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# futures_signal_gate() tests — pure pytest (no unittest.TestCase)
# ---------------------------------------------------------------------------

import pytest

from trading_ai.execution.futures_research import futures_signal_gate


@pytest.fixture()
def _base_signal() -> dict:
    return {"symbol": "MES", "action": "buy", "confidence": 0.7}


def test_futures_gate_disabled_blocks_all(_base_signal: dict) -> None:
    config: dict = {"futures_enabled": False, "allowed_symbols": ["MES", "MNQ"]}
    result = futures_signal_gate(config, [_base_signal])
    assert result == []


def test_futures_gate_missing_enabled_key_blocks_all(_base_signal: dict) -> None:
    # No futures_enabled key → defaults to False
    config: dict = {"allowed_symbols": ["MES"]}
    result = futures_signal_gate(config, [_base_signal])
    assert result == []


def test_futures_gate_symbol_not_in_whitelist() -> None:
    config: dict = {"futures_enabled": True, "allowed_symbols": ["MNQ"]}
    signal = {"symbol": "MES", "action": "buy"}
    result = futures_signal_gate(config, [signal])
    assert result == []


def test_futures_gate_kill_switch_blocks_all(_base_signal: dict) -> None:
    config: dict = {"futures_enabled": True, "allowed_symbols": ["MES"]}
    result = futures_signal_gate(config, [_base_signal], kill_switch_active=True)
    assert result == []


def test_futures_gate_approves_valid_signal(_base_signal: dict) -> None:
    config: dict = {"futures_enabled": True, "allowed_symbols": ["MES", "MNQ"]}
    result = futures_signal_gate(config, [_base_signal])
    assert len(result) == 1
    assert result[0]["symbol"] == "MES"


def test_futures_gate_no_whitelist_allows_any_symbol() -> None:
    # If allowed_symbols key is absent, any symbol passes
    config: dict = {"futures_enabled": True}
    signals = [{"symbol": "ANY", "action": "buy"}, {"symbol": "OTHER", "action": "hold"}]
    result = futures_signal_gate(config, signals)
    assert len(result) == 2


def test_futures_gate_filters_partial_whitelist() -> None:
    config: dict = {"futures_enabled": True, "allowed_symbols": ["MES"]}
    signals = [
        {"symbol": "MES", "action": "buy"},
        {"symbol": "MNQ", "action": "buy"},
    ]
    result = futures_signal_gate(config, signals)
    assert len(result) == 1
    assert result[0]["symbol"] == "MES"


def test_futures_gate_empty_signals_returns_empty() -> None:
    config: dict = {"futures_enabled": True}
    result = futures_signal_gate(config, [])
    assert result == []
