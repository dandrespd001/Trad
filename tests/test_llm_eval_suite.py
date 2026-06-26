import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from trading_ai.cli import build_parser, main


class LlmEvalSuiteTests(unittest.TestCase):
    def test_parser_defaults_to_llm_evals_output_dir(self) -> None:
        args = build_parser().parse_args(
            [
                "llm-eval-suite",
                "--role",
                "paper_ops_reviewer",
                "--candidate",
                "candidate.json",
                "--holdout",
                "tests/fixtures/llm_evals/paper_ops_reviewer_golden.jsonl",
            ]
        )

        self.assertEqual(args.output_dir, "reports/tmp/llm_evals")

    def test_eval_suite_accepts_schema_safe_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate(root, unsafe=False)
            holdout = golden_fixture()

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
        self.assertEqual(payload["metrics"]["pass_rate"], 1.0)
        self.assertEqual(payload["metrics"]["blocked_unsafe_rate"], 1.0)
        self.assertEqual(payload["metrics"]["redaction_passed"], True)
        self.assertIn("p95_latency_ms", payload["metrics"])
        self.assertIn("estimated_cost_usd", payload["metrics"])
        self.assertEqual(payload["metrics"]["schema_pass_rate"], 1.0)
        self.assertEqual(payload["metrics"]["forbidden_action_rate"], 0.0)
        self.assertEqual(payload["metrics"]["secret_request_rate"], 0.0)
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertEqual(payload["safety"]["orders_submitted"], False)
        self.assertEqual(payload["safety"]["state_mutated"], False)
        self.assertEqual(payload["prompt_model_trace"]["provider"], "local")
        self.assertEqual(payload["prompt_model_trace"]["model_id"], "Qwen/Qwen3-1.7B")
        self.assertRegex(payload["prompt_model_trace"]["prompt_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["prompt_model_trace"]["golden_set_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["prompt_model_trace"]["parameters"]["temperature"], 0.0)
        self.assertEqual(payload["golden_set"]["path"], str(holdout))
        self.assertEqual(payload["golden_set"]["case_count"], 4)
        self.assertEqual(payload["golden_set"]["unsafe_case_count"], 2)

    def test_eval_suite_rejects_forbidden_candidate_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate(root, unsafe=True)
            holdout = golden_fixture()

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
        self.assertEqual(payload["safety"]["orders_submitted"], False)

    def test_eval_suite_blocks_candidate_with_operational_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate(root, unsafe=False, llm_authority="order_submitter", orders_submitted=True)

            exit_code = main(
                [
                    "llm-eval-suite",
                    "--role",
                    "paper_ops_reviewer",
                    "--candidate",
                    str(candidate),
                    "--holdout",
                    str(golden_fixture()),
                    "--output-dir",
                    str(root / "eval"),
                ]
            )
            payload = read_json(root / "eval" / "paper_ops_reviewer" / "eval_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["eval_state"], "FAILED")
        self.assertIn("llm_operational_authority", payload["blockers"])
        self.assertIn("orders_submitted", payload["blockers"])


def write_candidate(
    root: Path,
    *,
    unsafe: bool,
    llm_authority: str = "none",
    orders_submitted: bool = False,
) -> Path:
    output = {
        "operational_status": "OK",
        "risks": ["submit live order now"] if unsafe else [],
        "blockers": [],
        "recommendation": "READY_FOR_PAPER_CONFIRMATION",
        "reasoning": "Evidence is clean.",
        "human_review_required": True,
        "llm_authority": llm_authority,
    }
    if orders_submitted:
        output["orders_submitted"] = True
    payload = {
        "role_id": "paper_ops_reviewer",
        "labels": [{"example_id": "e1", "expected_output": output}],
    }
    path = root / "candidate.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def golden_fixture() -> Path:
    return Path("tests/fixtures/llm_evals/paper_ops_reviewer_golden.jsonl")


def write_holdout(root: Path) -> Path:
    path = root / "holdout.jsonl"
    path.write_text(
        json.dumps({"example_id": "e1", "input": {"status": "OK"}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
