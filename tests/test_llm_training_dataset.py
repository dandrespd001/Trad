import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import build_parser, main


class LlmTrainingDatasetTests(unittest.TestCase):
    def test_training_dataset_redacts_sources_and_splits_examples(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            write_json(
                source / "paper_ops_check" / "2026-06-16" / "ops.json",
                {
                    "status": "OK",
                    "as_of_date": "2026-06-16",
                    "operator_note": "token=SECRET api_key=KEY",
                    "safety": {"paper_only": True, "orders_submitted": False},
                },
            )
            write_json(
                source / "paper_evidence_index" / "2026-06-16" / "evidence.json",
                {"status": "OK", "as_of_date": "2026-06-16", "issues": [], "safety": {"paper_only": True}},
            )

            args = build_parser().parse_args(
                [
                    "llm-training-dataset",
                    "--role",
                    "paper_ops_reviewer",
                    "--as-of-date",
                    "2026-06-16",
                    "--source-root",
                    str(source),
                    "--output-dir",
                    str(root / "dataset"),
                ]
            )
            exit_code = main(
                [
                    "llm-training-dataset",
                    "--role",
                    "paper_ops_reviewer",
                    "--as-of-date",
                    "2026-06-16",
                    "--source-root",
                    str(source),
                    "--output-dir",
                    str(root / "dataset"),
                ]
            )
            payload = read_json(root / "dataset" / "paper_ops_reviewer" / "2026-06-16" / "dataset.json")
            train_lines = (root / "dataset" / "paper_ops_reviewer" / "2026-06-16" / "train.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            holdout_lines = (root / "dataset" / "paper_ops_reviewer" / "2026-06-16" / "holdout.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()

        self.assertEqual(args.role, "paper_ops_reviewer")
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["dataset_state"], "READY_FOR_SUPERVISION")
        self.assertEqual(payload["role_id"], "paper_ops_reviewer")
        self.assertEqual(payload["split_counts"]["train"], 1)
        self.assertEqual(payload["split_counts"]["holdout"], 1)
        self.assertEqual(len(train_lines), 1)
        self.assertEqual(len(holdout_lines), 1)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("KEY", serialized)
        self.assertIn("[redacted]", serialized)
        self.assertRegex(payload["examples"][0]["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["authority"]["llm_authority"], "none")

    def test_training_dataset_blocks_unknown_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(
                [
                    "llm-training-dataset",
                    "--role",
                    "unknown",
                    "--as-of-date",
                    "2026-06-16",
                    "--source-root",
                    str(root),
                    "--output-dir",
                    str(root / "dataset"),
                ]
            )

        self.assertEqual(exit_code, 2)


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
