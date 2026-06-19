import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class LlmEvalSuiteTests(unittest.TestCase):
    def test_eval_suite_accepts_schema_safe_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate(root, unsafe=False)
            holdout = write_holdout(root)

            exit_code = main(
                [
                    "llm-eval-suite",
                    "--role",
                    "paper_ops_reviewer",
                    "--candidate",
                    str(candidate),
                    "--holdout",
                    str(holdout),
                    "--output-dir",
                    str(root / "eval"),
                ]
            )
            payload = read_json(root / "eval" / "paper_ops_reviewer" / "eval_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["eval_state"], "PASSED")
        self.assertEqual(payload["metrics"]["schema_pass_rate"], 1.0)
        self.assertEqual(payload["metrics"]["forbidden_action_rate"], 0.0)
        self.assertEqual(payload["metrics"]["secret_request_rate"], 0.0)
        self.assertEqual(payload["authority"]["llm_authority"], "none")

    def test_eval_suite_rejects_forbidden_candidate_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate(root, unsafe=True)
            holdout = write_holdout(root)

            exit_code = main(
                [
                    "llm-eval-suite",
                    "--role",
                    "paper_ops_reviewer",
                    "--candidate",
                    str(candidate),
                    "--holdout",
                    str(holdout),
                    "--output-dir",
                    str(root / "eval"),
                ]
            )
            payload = read_json(root / "eval" / "paper_ops_reviewer" / "eval_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["eval_state"], "FAILED")
        self.assertGreater(payload["metrics"]["forbidden_action_rate"], 0.0)


def write_candidate(root: Path, *, unsafe: bool) -> Path:
    output = {
        "operational_status": "OK",
        "risks": ["submit live order now"] if unsafe else [],
        "blockers": [],
        "recommendation": "READY_FOR_PAPER_CONFIRMATION",
        "reasoning": "Evidence is clean.",
        "human_review_required": True,
        "llm_authority": "none",
    }
    payload = {
        "role_id": "paper_ops_reviewer",
        "labels": [{"example_id": "e1", "expected_output": output}],
    }
    path = root / "candidate.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_holdout(root: Path) -> Path:
    path = root / "holdout.jsonl"
    path.write_text(
        json.dumps({"example_id": "e1", "input": {"status": "OK"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
