import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_ai.cli import main
from trading_ai.llm import local_registry
from trading_ai.llm.openai_client import OpenAIResearchClient


class LlmLocalRegistryTests(unittest.TestCase):
    def test_openai_runtime_is_disabled_even_when_key_exists(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-secret"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "External LLM APIs are disabled"):
                OpenAIResearchClient(model="gpt-5.5")

    def test_openai_supervision_flag_is_blocked_without_building_api_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    {
                        "dataset_state": "READY_FOR_SUPERVISION",
                        "examples": [
                            {
                                "example_id": "e1",
                                "input": {"status": "OK"},
                                "source_sha256": "0" * 64,
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-secret"}, clear=False):
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
                        "--confirm-llm-supervision",
                        "--output-dir",
                        str(root / "supervision"),
                    ]
                )
            payload = read_json(root / "supervision" / "paper_ops_reviewer" / "labels.json")

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["supervision_state"], "BLOCKED")
        self.assertIn("external_llm_api_disabled", payload["blockers"])
        self.assertEqual(payload["teacher_mode"], "disabled_external_api")
        self.assertTrue(payload["external_llm_requested"])
        self.assertFalse(payload["external_llm_used"])

    def test_local_cache_verify_blocks_missing_registry_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(root / "missing_registry.json"),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["cache_state"], "MISSING")
        self.assertIn("missing_local_model_registry", payload["blockers"])
        self.assertNotIn("model_not_in_local_registry", payload["blockers"])

    def test_local_cache_verify_blocks_missing_model_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["cache_state"], "MISSING")
        self.assertTrue(payload["local_files_only"])
        self.assertFalse(payload["network_allowed"])
        self.assertIn("missing_local_model_dir", payload["blockers"])

    def test_local_cache_verify_rejects_directory_without_weight_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["cache_state"], "MISSING")
        self.assertIn("missing_weight_file", payload["blockers"])

    def test_local_cache_verify_rejects_placeholder_weight_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_text("weights", encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["cache_state"], "MISSING")
        self.assertIn("invalid_weight_file:model.safetensors", payload["blockers"])

    def test_local_cache_verify_accepts_complete_local_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"0" * 2048)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["cache_state"], "READY")
        self.assertEqual(payload["model_path"], str(model_dir))

    def test_local_cache_verify_rejects_insufficient_total_weight_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"0" * 2048)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                                "minimum_total_weight_bytes": 4096,
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["cache_state"], "MISSING")
        self.assertEqual(payload["weight_total_bytes"], 2048)
        self.assertEqual(payload["minimum_total_weight_bytes"], 4096)
        self.assertIn("insufficient_total_weight_bytes:2048:4096", payload["blockers"])
        self.assertTrue(payload["local_files_only"])
        self.assertFalse(payload["network_allowed"])

    def test_local_cache_verify_accepts_weight_total_at_declared_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model-00001-of-00002.safetensors").write_bytes(b"0" * 2048)
            (model_dir / "model-00002-of-00002.safetensors").write_bytes(b"1" * 2048)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                                "minimum_total_weight_bytes": 4096,
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-cache-verify",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--output",
                    str(root / "cache_report.json"),
                ]
            )
            payload = read_json(root / "cache_report.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["cache_state"], "READY")
        self.assertEqual(payload["weight_total_bytes"], 4096)
        self.assertEqual(payload["minimum_total_weight_bytes"], 4096)
        self.assertTrue(payload["local_files_only"])
        self.assertFalse(payload["network_allowed"])

    def test_local_smoke_validates_fixture_response_against_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / "weights" / "qwen3-0.6b"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"0" * 2048)
            registry = root / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "model_id": "Qwen/Qwen3-0.6B",
                                "local_dir": "qwen3-0.6b",
                                "license": "Apache-2.0",
                            }
                        ]
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            fixture = root / "response.json"
            secret_marker = "SHOULD_NOT_LEAK"
            fixture.write_text(
                "secret_key="
                + secret_marker
                + "\n"
                + json.dumps(
                    {
                        "operational_status": "OK",
                        "risks": [],
                        "blockers": [],
                        "recommendation": "READY_FOR_PAPER_CONFIRMATION",
                        "reasoning": "local fixture response",
                        "human_review_required": True,
                        "llm_authority": "none",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-local-smoke",
                    "--registry",
                    str(registry),
                    "--cache-root",
                    str(root / "weights"),
                    "--model-id",
                    "Qwen/Qwen3-0.6B",
                    "--fixture-response",
                    str(fixture),
                    "--output",
                    str(root / "smoke.json"),
                ]
            )
            payload = read_json(root / "smoke.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["smoke_state"], "FIXTURE_PASSED")
        self.assertTrue(payload["schema_passed"])
        self.assertTrue(payload["local_files_only"])
        self.assertTrue(payload["fixture_response_used"])
        self.assertFalse(payload["model_loaded"])
        self.assertNotIn("raw_text", payload)
        self.assertIn("raw_text_preview", payload)
        self.assertNotIn(secret_marker, json.dumps(payload, sort_keys=True))

    def test_local_smoke_with_adapter_manifest_reports_loaded_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = write_ready_model_cache(root)
            registry = write_registry(root / "registry.json")
            adapter_dir = root / "adapter"
            adapter_dir.mkdir()
            (adapter_dir / "adapter_model.safetensors").write_text("adapter", encoding="utf-8")
            manifest = root / "adapter_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sft_state": "LOCAL_SFT_COMPLETED",
                        "role_id": "paper_ops_reviewer",
                        "base_model_id": "Qwen/Qwen3-0.6B",
                        "adapter_path": str(adapter_dir),
                        "adapter_hash": "e" * 64,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with patch(
                "trading_ai.llm.local_registry._generate_local_text",
                return_value=json.dumps(valid_paper_ops_review(), sort_keys=True),
            ) as generate:
                exit_code = main(
                    [
                        "llm-local-smoke",
                        "--registry",
                        str(registry),
                        "--cache-root",
                        str(root / "weights"),
                        "--model-id",
                        "Qwen/Qwen3-0.6B",
                        "--adapter-manifest",
                        str(manifest),
                        "--output",
                        str(root / "smoke.json"),
                    ]
                )
            payload = read_json(root / "smoke.json")

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["smoke_state"], "PASSED")
        self.assertTrue(payload["adapter_loaded"])
        self.assertEqual(payload["adapter_manifest"], str(manifest))
        self.assertEqual(payload["adapter_hash"], "e" * 64)
        self.assertEqual(payload["base_model_id"], "Qwen/Qwen3-0.6B")
        self.assertEqual(generate.call_args.kwargs["model_path"], model_dir)
        self.assertEqual(generate.call_args.kwargs["adapter_path"], adapter_dir)
        self.assertIn("PaperOpsReview", generate.call_args.kwargs["prompt"])
        self.assertIn("llm_authority", generate.call_args.kwargs["prompt"])
        self.assertIn("READY_FOR_PAPER_CONFIRMATION", generate.call_args.kwargs["prompt"])

    def test_generate_local_text_loads_peft_adapter_with_local_files_only(self) -> None:
        adapter_calls: list[dict[str, object]] = []
        chat_template_calls: list[dict[str, object]] = []
        tokenized_prompts: list[str] = []

        class FakeInputIds:
            shape = (1, 1)

        class FakeTokenizer:
            def apply_chat_template(
                self,
                messages: list[dict[str, str]],
                *,
                tokenize: bool,
                add_generation_prompt: bool,
            ) -> str:
                chat_template_calls.append(
                    {
                        "messages": messages,
                        "tokenize": tokenize,
                        "add_generation_prompt": add_generation_prompt,
                    }
                )
                return f"chat:{messages[0]['content']}:assistant"

            def __call__(self, prompt: str, *, return_tensors: str) -> dict[str, object]:
                tokenized_prompts.append(prompt)
                return {"input_ids": FakeInputIds()}

            def decode(self, token_ids: object, *, skip_special_tokens: bool) -> str:
                return json.dumps(valid_paper_ops_review(), sort_keys=True)

        class FakeModel:
            def generate(self, **kwargs: object) -> list[list[int]]:
                return [[0, 1]]

        class FakeAutoTokenizer:
            @staticmethod
            def from_pretrained(path: str, **kwargs: object) -> FakeTokenizer:
                return FakeTokenizer()

        class FakeAutoModelForCausalLM:
            @staticmethod
            def from_pretrained(path: str, **kwargs: object) -> FakeModel:
                return FakeModel()

        class FakePeftModel:
            @staticmethod
            def from_pretrained(model: FakeModel, adapter_path: str, **kwargs: object) -> FakeModel:
                adapter_calls.append({"adapter_path": adapter_path, **kwargs})
                return model

        fake_transformers = types.ModuleType("transformers")
        fake_transformers.AutoTokenizer = FakeAutoTokenizer
        fake_transformers.AutoModelForCausalLM = FakeAutoModelForCausalLM
        fake_peft = types.ModuleType("peft")
        fake_peft.PeftModel = FakePeftModel

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter_dir = root / "adapter"
            adapter_dir.mkdir()
            with patch.dict(sys.modules, {"transformers": fake_transformers, "peft": fake_peft}):
                text = local_registry._generate_local_text(
                    model_path=root / "model",
                    prompt="review",
                    max_new_tokens=8,
                    adapter_path=adapter_dir,
                )

        self.assertIn("READY_FOR_PAPER_CONFIRMATION", text)
        self.assertEqual(adapter_calls, [{"adapter_path": str(adapter_dir), "local_files_only": True}])
        self.assertEqual(
            chat_template_calls,
            [
                {
                    "messages": [{"role": "user", "content": "review"}],
                    "tokenize": False,
                    "add_generation_prompt": True,
                }
            ],
        )
        self.assertEqual(tokenized_prompts, ["chat:review:assistant"])


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_ready_model_cache(root: Path) -> Path:
    model_dir = root / "weights" / "qwen3-0.6b"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"0" * 2048)
    return model_dir


def write_registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "model_id": "Qwen/Qwen3-0.6B",
                        "local_dir": "qwen3-0.6b",
                        "license": "Apache-2.0",
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def valid_paper_ops_review() -> dict[str, object]:
    return {
        "operational_status": "OK",
        "risks": [],
        "blockers": [],
        "recommendation": "READY_FOR_PAPER_CONFIRMATION",
        "reasoning": "adapter smoke response",
        "human_review_required": True,
        "llm_authority": "none",
    }


if __name__ == "__main__":
    unittest.main()
