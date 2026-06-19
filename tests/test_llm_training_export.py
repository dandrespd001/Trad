import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main


class LlmTrainingExportTests(unittest.TestCase):
    def test_training_export_writes_openai_jsonl_from_supervised_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            labels = root / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        "role_id": "paper_ops_reviewer",
                        "labels": [
                            {
                                "example_id": "e1",
                                "expected_output": {
                                    "operational_status": "OK",
                                    "risks": [],
                                    "blockers": [],
                                    "recommendation": "READY_FOR_PAPER_CONFIRMATION",
                                    "reasoning": "clean",
                                    "human_review_required": True,
                                    "llm_authority": "none",
                                },
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-training-export",
                    "--role",
                    "paper_ops_reviewer",
                    "--supervised-dataset",
                    str(labels),
                    "--format",
                    "openai-jsonl",
                    "--output-dir",
                    str(root / "export"),
                ]
            )
            manifest = read_json(root / "export" / "paper_ops_reviewer" / "manifest.json")
            rows = (root / "export" / "paper_ops_reviewer" / "training.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["export_state"], "EXPORTED")
        self.assertEqual(manifest["row_count"], 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("expected_output", json.loads(rows[0]))


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
