import json
import tempfile
import unittest
from pathlib import Path

from trading_ai.cli import main
from trading_ai.llm.factory import run_llm_training_export


class LlmLocalWorkflowTests(unittest.TestCase):
    def test_training_export_writes_trl_chat_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            labels = write_labels(root / "labels.json")

            exit_code = main(
                [
                    "llm-training-export",
                    "--role",
                    "paper_ops_reviewer",
                    "--supervised-dataset",
                    str(labels),
                    "--format",
                    "trl-jsonl",
                    "--output-dir",
                    str(root / "export"),
                ]
            )
            manifest = read_json(root / "export" / "paper_ops_reviewer" / "manifest.json")
            rows = (root / "export" / "paper_ops_reviewer" / "training.jsonl").read_text(encoding="utf-8").splitlines()
            row = json.loads(rows[0])
            labels_hash = sha256(labels)

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["format"], "trl-jsonl")
        self.assertEqual(manifest["dataset_hash"], labels_hash)
        self.assertEqual(row["messages"][0]["role"], "user")
        self.assertEqual(row["messages"][1]["role"], "assistant")
        self.assertIn("llm_authority", row["messages"][1]["content"])

    def test_training_export_function_defaults_to_trl_chat_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            labels = write_labels(root / "labels.json")

            result = run_llm_training_export(
                role="paper_ops_reviewer",
                supervised_dataset=labels,
                output_dir=root / "export",
            )
            manifest = read_json(root / "export" / "paper_ops_reviewer" / "manifest.json")
            row = json.loads(
                (root / "export" / "paper_ops_reviewer" / "training.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(manifest["format"], "trl-jsonl")
        self.assertIn("messages", row)
        self.assertNotIn("expected_output", row)

    def test_local_sft_manifest_records_model_adapter_dataset_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            training = root / "training.jsonl"
            training.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}, sort_keys=True) + "\n", encoding="utf-8")
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
            (adapter / "adapter_model.safetensors").write_text("weights", encoding="utf-8")

            exit_code = main(
                [
                    "llm-local-sft",
                    "--role",
                    "paper_ops_reviewer",
                    "--base-model-id",
                    "Qwen/Qwen3-0.6B",
                    "--training-jsonl",
                    str(training),
                    "--adapter-dir",
                    str(adapter),
                    "--metrics-json",
                    json.dumps({"train_loss": 0.42}),
                    "--output",
                    str(root / "sft_manifest.json"),
                    "--register-existing-adapter",
                ]
            )
            payload = read_json(root / "sft_manifest.json")
            training_hash = sha256(training)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["sft_state"], "ADAPTER_REGISTERED")
        self.assertEqual(payload["base_model_id"], "Qwen/Qwen3-0.6B")
        self.assertEqual(payload["dataset_hash"], training_hash)
        self.assertRegex(payload["adapter_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["metrics"]["train_loss"], 0.42)
        self.assertEqual(payload["authority"]["llm_authority"], "none")

    def test_local_sft_blocks_unknown_role_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            training = root / "training.jsonl"
            training.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}, sort_keys=True) + "\n", encoding="utf-8")
            adapter = root / "adapter"
            adapter.mkdir()
            (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
            (adapter / "adapter_model.safetensors").write_text("weights", encoding="utf-8")

            exit_code = main(
                [
                    "llm-local-sft",
                    "--role",
                    "invented_role",
                    "--base-model-id",
                    "Qwen/Qwen3-0.6B",
                    "--training-jsonl",
                    str(training),
                    "--adapter-dir",
                    str(adapter),
                    "--output",
                    str(root / "sft_manifest.json"),
                    "--register-existing-adapter",
                ]
            )
            payload = read_json(root / "sft_manifest.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["sft_state"], "BLOCKED")
        self.assertIn("unknown_llm_role", payload["blockers"])

    def test_local_alias_requires_passed_eval_ready_adapter_and_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter_report = root / "adapter_report.json"
            adapter_report.write_text(
                json.dumps(
                    {
                        "adapter_state": "READY_FOR_LOCAL_ALIAS",
                        "role_id": "paper_ops_reviewer",
                        "base_model_id": "Qwen/Qwen3-0.6B",
                        "adapter_path": str(root / "adapter"),
                        "adapter_hash": "a" * 64,
                        "eval_report": str(root / "eval.json"),
                        "metrics": {"schema_pass_rate": 1.0},
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-alias-decision",
                    "--role",
                    "paper_ops_reviewer",
                    "--adapter-report",
                    str(adapter_report),
                    "--reviewer",
                    "qa",
                    "--reason",
                    "local eval gates passed",
                    "--decision",
                    "APPROVE",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            alias = read_json(root / "alias" / "paper_ops_reviewer" / "current.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(alias["alias_state"], "ACTIVE_LOCAL_LLM_ALIAS")
        self.assertEqual(alias["base_model_id"], "Qwen/Qwen3-0.6B")
        self.assertEqual(alias["adapter_hash"], "a" * 64)
        self.assertFalse(alias["safety"]["orders_submitted"])
        self.assertFalse(Path("models/latest_model.json").is_dir())

    def test_local_alias_blocks_unknown_role_even_when_adapter_report_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter_report = root / "adapter_report.json"
            adapter_report.write_text(
                json.dumps(
                    {
                        "adapter_state": "READY_FOR_LOCAL_ALIAS",
                        "role_id": "invented_role",
                        "base_model_id": "Qwen/Qwen3-0.6B",
                        "adapter_path": str(root / "adapter"),
                        "adapter_hash": "b" * 64,
                        "eval_report": str(root / "eval.json"),
                        "metrics": {"schema_pass_rate": 1.0},
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-alias-decision",
                    "--role",
                    "invented_role",
                    "--adapter-report",
                    str(adapter_report),
                    "--reviewer",
                    "qa",
                    "--reason",
                    "ready report still needs a known role",
                    "--decision",
                    "APPROVE",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            alias = read_json(root / "alias" / "invented_role" / "current.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(alias["alias_state"], "BLOCKED")
        self.assertIn("unknown_llm_role", alias["blockers"])

    def test_local_alias_blocks_unready_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter_report = root / "adapter_report.json"
            adapter_report.write_text(
                json.dumps(
                    {
                        "adapter_state": "REJECTED",
                        "role_id": "paper_ops_reviewer",
                        "base_model_id": "Qwen/Qwen3-0.6B",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-alias-decision",
                    "--role",
                    "paper_ops_reviewer",
                    "--adapter-report",
                    str(adapter_report),
                    "--reviewer",
                    "qa",
                    "--reason",
                    "not ready",
                    "--decision",
                    "APPROVE",
                    "--output-dir",
                    str(root / "alias"),
                ]
            )
            alias = read_json(root / "alias" / "paper_ops_reviewer" / "current.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(alias["alias_state"], "BLOCKED")
        self.assertIn("adapter_not_ready", alias["blockers"])


def write_labels(path: Path) -> Path:
    path.write_text(
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
    return path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
