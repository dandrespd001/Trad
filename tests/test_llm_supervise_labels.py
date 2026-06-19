import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.llm.schemas import validate_against_schema


class LlmSuperviseLabelsTests(unittest.TestCase):
    def test_supervise_labels_writes_schema_valid_frontier_labels_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = write_dataset(root, status="OK")

            exit_code = main(
                [
                    "llm-supervise-labels",
                    "--role",
                    "paper_ops_reviewer",
                    "--dataset",
                    str(dataset),
                    "--frontier-model",
                    "deterministic-frontier",
                    "--output-dir",
                    str(root / "labels"),
                ]
            )
            payload = read_json(root / "labels" / "paper_ops_reviewer" / "labels.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["supervision_state"], "SUPERVISED")
        self.assertEqual(payload["teacher_mode"], "deterministic")
        self.assertEqual(payload["frontier_model"], "deterministic-frontier")
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        label = payload["labels"][0]["expected_output"]
        validate_against_schema("PaperOpsReview", label)
        self.assertEqual(label["recommendation"], "READY_FOR_PAPER_CONFIRMATION")

    def test_openai_supervision_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = write_dataset(root, status="OK")

            exit_code = main(
                [
                    "llm-supervise-labels",
                    "--role",
                    "paper_ops_reviewer",
                    "--dataset",
                    str(dataset),
                    "--frontier-model",
                    "gpt-5.5",
                    "--use-openai",
                    "--output-dir",
                    str(root / "labels"),
                ]
            )
            payload = read_json(root / "labels" / "paper_ops_reviewer" / "labels.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["supervision_state"], "BLOCKED")
        self.assertIn("missing_confirm_llm_supervision", payload["blockers"])
        self.assertFalse(payload["safety"]["credentials_read"])


def write_dataset(root: Path, *, status: str) -> Path:
    path = root / "dataset.json"
    example = {
        "example_id": "paper_ops_reviewer:2026-06-16:ops",
        "role_id": "paper_ops_reviewer",
        "as_of_date": "2026-06-16",
        "source_path": str(root / "ops.json"),
        "source_sha256": "0" * 64,
        "input": {"status": status, "issues": [], "safety": {"paper_only": True}},
        "messages": [{"role": "user", "content": "Review paper ops evidence."}],
    }
    path.write_text(
        json.dumps(
            {
                "dataset_state": "READY_FOR_SUPERVISION",
                "role_id": "paper_ops_reviewer",
                "examples": [example],
                "splits": {"train": [example], "validation": [], "holdout": []},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
