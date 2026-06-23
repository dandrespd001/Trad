import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class FuturesResearchScaffoldTests(unittest.TestCase):
    def test_parser_defaults_for_futures_research_scaffold(self) -> None:
        args = build_parser().parse_args(["futures-research-scaffold", "--as-of-date", "2026-06-18"])

        self.assertEqual(args.config, "configs/futures_micro.yml")
        self.assertEqual(args.output_dir, "reports/tmp/futures_research")
        self.assertEqual(args.as_of_date, "2026-06-18")

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["futures-execute"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["futures-submit"])

    def test_complete_mes_mnq_config_generates_ok_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_futures_config(root / "futures.yml")

            exit_code = main(scaffold_args(config, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "research_manifest.json")
            markdown = (root / "out" / "2026-06-18" / "research_manifest.md").read_text(encoding="utf-8")
            output_text = json.dumps(payload)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["contracts"][0]["symbol"], "MES")
        self.assertEqual(payload["contracts"][0]["tick_value"], 1.25)
        self.assertIn("margin_placeholder", payload["contracts"][0])
        self.assertIn("data_requirements", payload)
        self.assertFalse(payload["safety"]["live_trading_allowed"])
        self.assertNotIn("futures-execute", output_text)
        self.assertNotIn("futures-submit", output_text)
        self.assertIn("Status: **OK**", markdown)

    def test_missing_platform_decision_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_futures_config(root / "futures.yml", include_platform_decision=False)

            exit_code = main(scaffold_args(config, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "research_manifest.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "WARN")
        self.assertIn("missing_platform_decision", payload["warnings"])

    def test_missing_contract_requirement_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = write_futures_config(root / "futures.yml")
            config.write_text(
                config.read_text(encoding="utf-8").replace("margin: {placeholder_usd: 1500}", "margin: null")
            )

            exit_code = main(scaffold_args(config, root / "out"))
            payload = read_json(root / "out" / "2026-06-18" / "research_manifest.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("missing_margin", {blocker["code"] for blocker in payload["blockers"]})


def scaffold_args(config: Path, output_dir: Path) -> list[str]:
    return [
        "futures-research-scaffold",
        "--config",
        str(config),
        "--output-dir",
        str(output_dir),
        "--as-of-date",
        "2026-06-18",
    ]


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


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
