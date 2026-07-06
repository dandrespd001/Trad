import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.execution.live_rehearsal import run_live_rehearsal


FIXTURES = Path("tests/fixtures/live_rehearsal")


class LiveRehearsalTests(unittest.TestCase):
    def test_rehearsal_runs_all_fixture_scenarios_with_expected_gates_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_live_rehearsal(fixtures=FIXTURES, output=Path(tmp) / "latest")
            payload = json.loads(result.summary_path.read_text(encoding="utf-8"))
            evidence = json.loads(result.evidence_index_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(payload["status"], "PASSED")
        self.assertEqual(payload["scenario_count"], 8)
        self.assertTrue(all(item["passed"] for item in payload["scenarios"]))
        self.assertEqual(len(evidence["items"]), 8)
        for scenario in payload["scenarios"]:
            with self.subTest(name=scenario["name"]):
                self.assertEqual(scenario["expected_gate"], scenario["observed_gate"])
                self.assertEqual(scenario["expected_blocker"], scenario["observed_blocker"])
                self.assertFalse(scenario["safety"]["orders_submitted"])
                self.assertRegex(scenario["input_hash"], r"^[0-9a-f]{64}$")
                self.assertRegex(scenario["output_hash"], r"^[0-9a-f]{64}$")

    def test_cli_live_rehearsal_writes_summary_markdown_and_evidence_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "latest"
            exit_code = main(["live-rehearsal", "--fixtures", str(FIXTURES), "--output", str(output)])
            payload = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            markdown = (output / "summary.md").read_text(encoding="utf-8")
            evidence = json.loads((output / "evidence_index.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "PASSED")
        self.assertIn("Live Rehearsal", markdown)
        self.assertEqual(evidence["status"], "PASSED")
        self.assertFalse(payload["safety"]["orders_submitted"])


if __name__ == "__main__":
    unittest.main()
