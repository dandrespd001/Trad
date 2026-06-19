import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class LlmModelAliasDecisionTests(unittest.TestCase):
    def test_model_alias_requires_ready_candidate_and_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate_report(root / "candidate.json", "READY_FOR_ALIAS")

            exit_code = main(
                [
                    "llm-model-alias-decision",
                    "--role",
                    "paper_ops_reviewer",
                    "--candidate-report",
                    str(candidate),
                    "--reviewer",
                    "qa",
                    "--reason",
                    "eval gates passed",
                    "--decision",
                    "APPROVE",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            payload = read_json(root / "alias" / "paper_ops_reviewer" / "current.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["alias_state"], "ACTIVE_LLM_ALIAS")
        self.assertEqual(payload["role_id"], "paper_ops_reviewer")
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertTrue(payload["authority"]["human_review_required"])
        self.assertFalse(payload["safety"]["orders_submitted"])
        self.assertRegex(payload["alias_hash"], r"^[0-9a-f]{64}$")

    def test_model_alias_blocks_defer_or_unready_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidate = write_candidate_report(root / "candidate.json", "REJECTED")

            exit_code = main(
                [
                    "llm-model-alias-decision",
                    "--role",
                    "paper_ops_reviewer",
                    "--candidate-report",
                    str(candidate),
                    "--reviewer",
                    "qa",
                    "--reason",
                    "not ready",
                    "--decision",
                    "DEFER",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            payload = read_json(root / "alias" / "paper_ops_reviewer" / "current.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["alias_state"], "BLOCKED")
        self.assertIn("candidate_not_ready", payload["blockers"])
        self.assertIn("human_approval_missing", payload["blockers"])


def write_candidate_report(path: Path, state: str) -> Path:
    payload = {
        "role_id": "paper_ops_reviewer",
        "candidate_state": state,
        "candidate": {
            "model": "gpt-5.5",
            "prompt_version": "paper_ops_reviewer:v1",
            "eval_report": "eval.json",
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
