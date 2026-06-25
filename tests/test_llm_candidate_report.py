import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import main


class LlmCandidateReportTests(unittest.TestCase):
    def test_candidate_report_marks_ready_when_candidate_beats_baseline_and_passes_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = write_eval(root / "baseline.json", state="PASSED", pass_rate=0.95, forbidden=0.0)
            candidate = write_eval(root / "candidate.json", state="PASSED", pass_rate=1.0, forbidden=0.0)

            exit_code = main(
                [
                    "llm-candidate-report",
                    "--role",
                    "paper_ops_reviewer",
                    "--baseline-eval",
                    str(baseline),
                    "--candidate-eval",
                    str(candidate),
                    "--output-dir",
                    str(root / "candidate_report"),
                ]
            )
            payload = read_json(root / "candidate_report" / "paper_ops_reviewer" / "candidate_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["candidate_state"], "READY_FOR_ALIAS")
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertEqual(payload["blockers"], [])

    def test_candidate_report_rejects_failed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = write_eval(root / "baseline.json", state="PASSED", pass_rate=1.0, forbidden=0.0)
            candidate = write_eval(root / "candidate.json", state="FAILED", pass_rate=1.0, forbidden=0.5)

            exit_code = main(
                [
                    "llm-candidate-report",
                    "--role",
                    "paper_ops_reviewer",
                    "--baseline-eval",
                    str(baseline),
                    "--candidate-eval",
                    str(candidate),
                    "--output-dir",
                    str(root / "candidate_report"),
                ]
            )
            payload = read_json(root / "candidate_report" / "paper_ops_reviewer" / "candidate_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["candidate_state"], "REJECTED")
        self.assertIn("candidate_eval_not_passed", payload["blockers"])


def write_eval(path: Path, *, state: str, pass_rate: float, forbidden: float) -> Path:
    payload = {
        "eval_state": state,
        "metrics": {
            "schema_pass_rate": pass_rate,
            "forbidden_action_rate": forbidden,
            "secret_request_rate": 0.0,
            "artifact_hallucination_rate": 0.0,
            "safety_recall": 1.0,
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
