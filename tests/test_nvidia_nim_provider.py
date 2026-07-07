import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from trading_ai.cli import build_parser, main
from trading_ai.llm.nvidia_nim import (
    NvidiaNimSchemaError,
    NvidiaNimResearchClient,
    _nim_instructions,
    load_nvidia_model_suite,
)
from trading_ai.llm.openai_client import classify_prompt_safety
from trading_ai.llm.provider_benchmark import _provider_error_code, _smoke_prompt, run_llm_provider_benchmark


class FakeChatCompletions:
    def __init__(self, output_text: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._output_text = output_text

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        message = type("Message", (), {"content": self._output_text})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice], "usage": {"total_tokens": 42}})()


class FakeChat:
    def __init__(self, output_text: str) -> None:
        self.completions = FakeChatCompletions(output_text)


class FakeRawClient:
    def __init__(self, output_text: str) -> None:
        self.chat = FakeChat(output_text)


class FakeUsage:
    def model_dump(self) -> dict[str, int]:
        return {"total_tokens": 42}


class FakeObjectUsageCompletions(FakeChatCompletions):
    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        message = type("Message", (), {"content": self._output_text})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice], "usage": FakeUsage()})()


class FakeObjectUsageChat:
    def __init__(self, output_text: str) -> None:
        self.completions = FakeObjectUsageCompletions(output_text)


class FakeObjectUsageRawClient:
    def __init__(self, output_text: str) -> None:
        self.chat = FakeObjectUsageChat(output_text)


class FakeRejectsJsonModeCompletions:
    def __init__(self, output_text: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._output_text = output_text

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if "response_format" in kwargs:
            raise ValueError("unsupported parameter: response_format")
        message = type("Message", (), {"content": self._output_text})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice], "usage": {"total_tokens": 42}})()


class FakeRejectsJsonModeChat:
    def __init__(self, output_text: str) -> None:
        self.completions = FakeRejectsJsonModeCompletions(output_text)


class FakeRejectsJsonModeRawClient:
    def __init__(self, output_text: str) -> None:
        self.chat = FakeRejectsJsonModeChat(output_text)


class FakeStructuredClient:
    def __init__(self, *, model: str, confirm_external_llm: bool) -> None:
        self.model = model
        self.confirm_external_llm = confirm_external_llm

    def create_structured_output(self, *, schema_name: str, user_input: str, **kwargs: Any) -> Any:
        if self.model == "meta/llama-3.1-8b-instruct":
            raise NvidiaNimSchemaError(
                validation_reason="LLMSignalProposal missing required keys: risk_notes nvapi-abc123456789_SECRET_VALUE",
                raw_response="token=SHOULD_NOT_APPEAR nvapi-abc123456789_SECRET_VALUE " + ("x" * 500),
            )
        return type(
            "StructuredResult",
            (),
            {
                "latency_seconds": 0.25,
                "prompt_hash": "abc123",
                "usage": {"total_tokens": 42},
            },
        )()


def valid_llm_signal_proposal(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "proposal_kind": "entry",
        "symbol": "SPY",
        "action": "hold",
        "confidence": 0.5,
        "time_horizon": "1d",
        "thesis": "paper-only audit",
        "risk_notes": ["no execution authority"],
        "evidence_refs": ["fixture"],
        "model_id": "nvidia-nim-shadow",
        "prompt_version": "signal_proposal_auditor:nim:v1",
        "input_hashes": {},
        "llm_authority": "none",
    }
    payload.update(overrides)
    return payload


class NvidiaNimProviderTests(unittest.TestCase):
    def test_parser_exposes_llm_provider_benchmark(self) -> None:
        args = build_parser().parse_args(
            [
                "llm-provider-benchmark",
                "--provider",
                "nvidia-nim",
                "--model-suite",
                "configs/llm_nvidia_models.yml",
                "--role",
                "signal_proposal_auditor",
                "--as-of-date",
                "2026-06-30",
            ]
        )

        self.assertEqual(args.provider, "nvidia-nim")
        self.assertEqual(args.role, "signal_proposal_auditor")
        self.assertFalse(args.confirm_external_llm)

    def test_nim_client_does_not_read_env_without_confirmation(self) -> None:
        old_key = os.environ.get("NVIDIA_API_KEY")
        os.environ["NVIDIA_API_KEY"] = "nvapi-secret-test"
        try:
            with self.assertRaises(RuntimeError) as ctx:
                NvidiaNimResearchClient(model="meta/llama-3.1-8b-instruct", confirm_external_llm=False)
        finally:
            if old_key is None:
                os.environ.pop("NVIDIA_API_KEY", None)
            else:
                os.environ["NVIDIA_API_KEY"] = old_key

        self.assertIn("confirm_external_llm_required", str(ctx.exception))
        self.assertNotIn("nvapi-secret-test", str(ctx.exception))

    def test_fake_nim_client_validates_schema_and_sets_openai_compatible_base_url(self) -> None:
        fake = FakeRawClient(json.dumps(valid_llm_signal_proposal()))
        client = NvidiaNimResearchClient(
            client=fake,
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(result.data["llm_authority"], "none")
        call = fake.chat.completions.calls[0]
        self.assertEqual(call["model"], "meta/llama-3.1-8b-instruct")
        self.assertEqual(call["messages"][0]["role"], "system")
        self.assertIn("JSON object", call["messages"][0]["content"])
        self.assertEqual(call["messages"][1]["content"], "Audit SPY")
        self.assertEqual(client.base_url, "https://integrate.api.nvidia.com/v1")

    def test_nim_client_accepts_json_wrapped_in_markdown_or_text(self) -> None:
        payload = valid_llm_signal_proposal()
        cases = (
            json.dumps(payload),
            f"```json\n{json.dumps(payload)}\n```",
            f"Here is the audit:\n{json.dumps(payload)}\nDone.",
        )

        for output_text in cases:
            with self.subTest(output_text=output_text):
                client = NvidiaNimResearchClient(
                    client=FakeRawClient(output_text),
                    model="meta/llama-3.1-8b-instruct",
                    confirm_external_llm=False,
                    api_key="unused",
                )

                result = client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

                self.assertEqual(result.data["llm_authority"], "none")
                self.assertEqual(result.data["symbol"], "SPY")

    def test_nim_client_rejects_response_without_json_object(self) -> None:
        client = NvidiaNimResearchClient(
            client=FakeRawClient("I cannot provide JSON for this request."),
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        with self.assertRaises(ValueError) as ctx:
            client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertIn("missing_json_object", str(ctx.exception))

    def test_nim_client_rejects_invalid_schema_with_safe_diagnostics(self) -> None:
        payload = {"secret": "token=SHOULD_NOT_APPEAR", **valid_llm_signal_proposal()}
        payload.pop("risk_notes")
        payload["provider_key"] = "nvapi-abc123456789_SECRET_VALUE"
        output = json.dumps(payload)
        client = NvidiaNimResearchClient(
            client=FakeRawClient(output),
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        with self.assertRaises(NvidiaNimSchemaError) as ctx:
            client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(ctx.exception.error_code, "provider_schema_error")
        self.assertIn("missing required keys: risk_notes", ctx.exception.validation_reason)
        self.assertIn("token:[redacted]", ctx.exception.raw_response_preview)
        self.assertNotIn("SHOULD_NOT_APPEAR", ctx.exception.raw_response_preview)
        self.assertNotIn("nvapi-abc123456789_SECRET_VALUE", ctx.exception.raw_response_preview)
        self.assertLessEqual(len(ctx.exception.raw_response_preview), 220)

    def test_nim_schema_error_redacts_nvidia_keys_from_safe_diagnostics(self) -> None:
        error = NvidiaNimSchemaError(
            validation_reason="schema failed with nvapi-abc123456789_SECRET_VALUE",
            raw_response="provider echoed nvapi-abc123456789_SECRET_VALUE",
        )

        self.assertIn("[redacted-nvidia-api-key]", error.validation_reason)
        self.assertIn("[redacted-nvidia-api-key]", error.raw_response_preview)
        self.assertNotIn("nvapi-abc123456789_SECRET_VALUE", error.validation_reason)
        self.assertNotIn("nvapi-abc123456789_SECRET_VALUE", error.raw_response_preview)

    def test_nim_client_uses_json_response_format_and_retries_without_it_when_rejected(self) -> None:
        payload = valid_llm_signal_proposal()
        fake = FakeRejectsJsonModeRawClient(json.dumps(payload))
        client = NvidiaNimResearchClient(
            client=fake,
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        calls = fake.chat.completions.calls
        self.assertEqual(result.data["llm_authority"], "none")
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", calls[1])

    def test_nim_client_returns_json_serializable_usage(self) -> None:
        payload = valid_llm_signal_proposal()
        client = NvidiaNimResearchClient(
            client=FakeObjectUsageRawClient(json.dumps(payload)),
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(result.usage, {"total_tokens": 42})
        json.dumps({"usage": result.usage})

    def test_signal_proposal_prompt_contains_exact_schema_example(self) -> None:
        instructions = _nim_instructions("LLMSignalProposal")

        self.assertIn('"symbol": "SPY"', instructions)
        self.assertIn('"action": "hold"', instructions)
        self.assertIn('"confidence": 0.5', instructions)
        self.assertIn('"thesis": "paper-only audit"', instructions)
        self.assertIn('"risk_notes": ["no execution authority"]', instructions)
        self.assertIn('"evidence_refs": ["fixture"]', instructions)
        self.assertIn('"llm_authority": "none"', instructions)

    def test_provider_benchmark_smoke_prompts_pass_local_guardrails(self) -> None:
        for role in ("signal_proposal_auditor", "paper_ops_reviewer"):
            with self.subTest(role=role):
                prompt = _smoke_prompt(role=role, as_of_date="2026-06-30")
                safety = classify_prompt_safety(prompt)

                self.assertTrue(safety.allowed, safety.reason)

    def test_model_suite_rejects_non_allowlisted_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            suite = Path(temp_dir) / "suite.yml"
            suite.write_text(
                textwrap.dedent(
                    """
                    models:
                      - id: unknown/private-model
                        provider: nvidia-nim
                        enabled: true
                        source_url: https://build.nvidia.com/models
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                load_nvidia_model_suite(suite)

        self.assertIn("not allowlisted", str(ctx.exception))

    def test_default_nvidia_model_suite_enables_only_available_smoke_models(self) -> None:
        models = load_nvidia_model_suite("configs/llm_nvidia_models.yml")
        enabled = {model.id for model in models if model.enabled}

        self.assertEqual(
            enabled,
            {
                "nvidia/llama-3.3-nemotron-super-49b-v1",
                "meta/llama-3.1-8b-instruct",
                "meta/llama-3.3-70b-instruct",
            },
        )

    def test_provider_error_codes_distinguish_unavailable_models_and_schema_errors(self) -> None:
        not_found = type("NotFoundError", (Exception,), {})("model not found")

        self.assertEqual(_provider_error_code(not_found), "provider_model_unavailable")
        self.assertEqual(
            _provider_error_code(json.JSONDecodeError("Expecting value", "not json", 0)),
            "provider_schema_error",
        )

    def test_provider_benchmark_blocks_external_calls_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            suite = root / "suite.yml"
            suite.write_text(
                textwrap.dedent(
                    """
                    models:
                      - id: meta/llama-3.1-8b-instruct
                        provider: nvidia-nim
                        enabled: true
                        source_url: https://build.nvidia.com/meta/llama-3_1-8b-instruct
                        source_timestamp: "2026-06-30T00:00:00Z"
                    """
                ),
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "llm-provider-benchmark",
                    "--provider",
                    "nvidia-nim",
                    "--model-suite",
                    str(suite),
                    "--role",
                    "signal_proposal_auditor",
                    "--as-of-date",
                    "2026-06-30",
                    "--output-dir",
                    str(root / "benchmark"),
                ]
            )
            payload = json.loads(
                (root / "benchmark" / "nvidia-nim" / "signal_proposal_auditor" / "2026-06-30" / "benchmark.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertFalse(payload["external_llm_used"])
        self.assertEqual(payload["authority"]["llm_authority"], "none")
        self.assertFalse(payload["safety"]["orders_submitted"])

    def test_provider_benchmark_writes_partial_result_when_one_confirmed_model_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            suite = root / "suite.yml"
            suite.write_text(
                textwrap.dedent(
                    """
                    models:
                      - id: meta/llama-3.1-8b-instruct
                        provider: nvidia-nim
                        enabled: true
                      - id: meta/llama-3.3-70b-instruct
                        provider: nvidia-nim
                        enabled: true
                    """
                ),
                encoding="utf-8",
            )

            with mock.patch("trading_ai.llm.provider_benchmark.NvidiaNimResearchClient", FakeStructuredClient):
                result = run_llm_provider_benchmark(
                    provider="nvidia-nim",
                    model_suite=suite,
                    role="signal_proposal_auditor",
                    as_of_date="2026-06-30",
                    output_dir=root / "benchmark",
                    confirm_external_llm=True,
                )
            payload = json.loads(result.output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(payload["status"], "PARTIAL")
        self.assertTrue(payload["external_llm_used"])
        statuses = {item["model_id"]: item["status"] for item in payload["models"]}
        self.assertEqual(statuses["meta/llama-3.1-8b-instruct"], "error")
        self.assertEqual(statuses["meta/llama-3.3-70b-instruct"], "ok")
        error = next(item for item in payload["models"] if item["model_id"] == "meta/llama-3.1-8b-instruct")
        self.assertEqual(error["error_code"], "provider_schema_error")
        self.assertIn("risk_notes", error["validation_reason"])
        self.assertIn("token:[redacted]", error["raw_response_preview"])
        self.assertLessEqual(len(error["raw_response_preview"]), 220)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("SHOULD_NOT_APPEAR", serialized)
        self.assertNotIn("nvapi-abc123456789_SECRET_VALUE", serialized)
        self.assertIn("[redacted-nvidia-api-key]", serialized)
        self.assertNotIn("token=", serialized)


class NvidiaNimTruncationGuardTests(unittest.TestCase):
    """A truncated completion must become a hard, attributable error instead
    of a silently parsed partial JSON (decision veracity guard)."""

    def _client_with_response(self, *, finish_reason: str, completion_tokens: int) -> NvidiaNimResearchClient:
        payload = valid_llm_signal_proposal()
        choice = type(
            "Choice",
            (),
            {"message": type("Message", (), {"content": json.dumps(payload)})(), "finish_reason": finish_reason},
        )()
        response = type(
            "Response",
            (),
            {"choices": [choice], "usage": {"total_tokens": completion_tokens, "completion_tokens": completion_tokens}},
        )()
        completions = type("Completions", (), {"create": lambda self, **kwargs: response})()
        fake = type("Fake", (), {"chat": type("Chat", (), {"completions": completions})()})()
        return NvidiaNimResearchClient(
            client=fake,
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

    def test_nim_client_detects_truncation_by_finish_reason(self) -> None:
        client = self._client_with_response(finish_reason="length", completion_tokens=100)

        with self.assertRaises(NvidiaNimSchemaError) as ctx:
            client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(ctx.exception.error_code, "nvidia_nim_response_truncated")
        self.assertIn("finish_reason=length", ctx.exception.validation_reason)

    def test_nim_client_detects_truncation_by_token_saturation(self) -> None:
        client = self._client_with_response(finish_reason="stop", completion_tokens=1024)

        with self.assertRaises(NvidiaNimSchemaError) as ctx:
            client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(ctx.exception.error_code, "nvidia_nim_response_truncated")
        self.assertIn("completion_tokens=1024", ctx.exception.validation_reason)

    def test_nim_client_accepts_untruncated_response(self) -> None:
        client = self._client_with_response(finish_reason="stop", completion_tokens=100)

        result = client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(result.data["llm_authority"], "none")

    def test_nim_client_passes_timeout_and_retries_to_chat_completion(self) -> None:
        payload = valid_llm_signal_proposal()
        fake = FakeRawClient(json.dumps(payload))
        client = NvidiaNimResearchClient(
            client=fake,
            model="meta/llama-3.1-8b-instruct",
            confirm_external_llm=False,
            api_key="unused",
        )

        client.create_structured_output(schema_name="LLMSignalProposal", user_input="Audit SPY")

        self.assertEqual(len(fake.chat.completions.calls), 1)
        call = fake.chat.completions.calls[0]
        self.assertEqual(call["timeout"], 60.0)
        self.assertEqual(call["max_retries"], 2)


if __name__ == "__main__":
    unittest.main()
