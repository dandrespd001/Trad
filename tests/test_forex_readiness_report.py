import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class ForexReadinessReportTests(unittest.TestCase):
    def test_parser_defaults_for_read_only_forex_readiness(self) -> None:
        args = build_parser().parse_args(["forex-readiness-report"])

        self.assertEqual(args.config, "configs/forex_major.yml")
        self.assertEqual(args.output, "reports/tmp/forex_readiness/latest.json")
        self.assertEqual(args.markdown_output, "reports/tmp/forex_readiness/latest.md")

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["forex-execute"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["forex-submit"])

    def test_valid_major_fx_fixture_produces_ok_report_without_live_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_forex_config(root / "forex.yml")
            output = root / "forex.json"

            exit_code = main(
                [
                    "forex-readiness-report",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "forex.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["platform_decision"]["selected"], "OANDA_PRACTICE_RESEARCH_ONLY")
        self.assertEqual(payload["platform_decision"]["status"], "DECIDED")
        self.assertTrue(payload["platform_decision"]["read_only"])
        self.assertEqual(payload["summary"]["pair_count"], 2)
        self.assertEqual(payload["summary"]["ready_pairs"], ["EURUSD", "USDJPY"])
        self.assertFalse(payload["permissions"]["live_trading_allowed"])
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertFalse(payload["safety"]["broker_client_built"])
        self.assertFalse(payload["safety"]["orders_enabled"])
        self.assertFalse(payload["safety"]["credentials_read"])

    def test_missing_platform_decision_warns_without_pair_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_forex_config(root / "forex.yml", include_platform_decision=False)
            output = root / "forex.json"

            exit_code = main(
                [
                    "forex-readiness-report",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "forex.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["platform_decision"]["status"], "MISSING")
        self.assertIn("missing_platform_decision", payload["warnings"])
        self.assertEqual(payload["summary"]["ready_pairs"], ["EURUSD", "USDJPY"])

    def test_missing_session_liquidity_or_costs_blocks_pair(self) -> None:
        cases = {
            "sessions": "sessions: null",
            "liquidity": "liquidity: null",
            "costs": "costs: null",
        }
        for field, replacement in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = write_forex_config(root / "forex.yml")
                text = config.read_text(encoding="utf-8")
                text = text.replace(f"      {field}: {{", f"      {replacement} # ")
                config.write_text(text, encoding="utf-8")
                output = root / "forex.json"

                exit_code = main(
                    [
                        "forex-readiness-report",
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                        "--markdown-output",
                        str(root / "forex.md"),
                    ]
                )
                payload = read_json(output)

                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "BLOCKED")
                self.assertIn(f"missing_{field}", {blocker["code"] for blocker in payload["blockers"]})

    def test_live_permissions_block_readiness_even_with_valid_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_forex_config(root / "forex.yml")
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "live_trading_allowed: false",
                    "live_trading_allowed: true",
                ),
                encoding="utf-8",
            )
            output = root / "forex.json"

            exit_code = main(
                [
                    "forex-readiness-report",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(root / "forex.md"),
                ]
            )
            payload = read_json(output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("live_trading_allowed_true", {blocker["code"] for blocker in payload["blockers"]})
        self.assertFalse(payload["safety"]["orders_enabled"])


def write_forex_config(path: Path, *, include_platform_decision: bool = True) -> Path:
    platform_decision = (
        """
              platform_decision:
                selected: OANDA_PRACTICE_RESEARCH_ONLY
                rationale: research-only FX evaluation before execution
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
            forex:
{platform_decision.rstrip()}
              pairs:
                - symbol: EURUSD
                  base_currency: EUR
                  quote_currency: USD
                  venue: OTC_SPOT
                  pip_size: 0.0001
                  lot_size: 1000
                  sessions: {{timezone: UTC, active: 24x5}}
                  liquidity: {{tier: major, spread_watch: required}}
                  costs: {{spread_pips: 0.8, slippage_pips: 0.2, financing_model: placeholder}}
                - symbol: USDJPY
                  base_currency: USD
                  quote_currency: JPY
                  venue: OTC_SPOT
                  pip_size: 0.01
                  lot_size: 1000
                  sessions: {{timezone: UTC, active: 24x5}}
                  liquidity: {{tier: major, spread_watch: required}}
                  costs: {{spread_pips: 0.9, slippage_pips: 0.2, financing_model: placeholder}}
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
