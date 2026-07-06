"""NVIDIA NIM OpenAI-compatible research client and model-suite policy."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.config import load_yaml_file
from trading_ai.execution.paper_common import redact_secrets as _redact_sensitive_text
from trading_ai.llm.openai_client import (
    LLMGuardrailError,
    StructuredOutputResult,
    _guard_user_input,
    _instructions,
    _prompt_cache_key,
    _prompt_hash,
    _usage_to_dict,
)
from trading_ai.llm.schemas import validate_against_schema

DEFAULT_NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL_SOURCE_URL = "https://build.nvidia.com/models"
RAW_RESPONSE_PREVIEW_LIMIT = 200

# Serverless catalog entries verified against NVIDIA Build/API reference pages
# during implementation on 2026-06-30. The suite file can disable any entry.
NVIDIA_NIM_MODEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "nvidia/llama-3.1-nemotron-ultra-253b-v1",
        "nvidia/llama-3.3-nemotron-super-49b-v1",
        "meta/llama-3.1-8b-instruct",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.3-70b-instruct",
        "qwen/qwen2.5-72b-instruct",
        "qwen/qwq-32b",
        "deepseek-ai/deepseek-r1",
        "mistralai/mistral-large",
        "mistralai/mixtral-8x22b-instruct-v0.1",
    }
)


@dataclass(frozen=True)
class NvidiaModelSpec:
    id: str
    provider: str
    enabled: bool
    source_url: str
    source_timestamp: str


class NvidiaNimSchemaError(ValueError):
    """Raised when a NIM response cannot be parsed or validated safely."""

    def __init__(
        self,
        *,
        validation_reason: str,
        raw_response: str,
        error_code: str = "provider_schema_error",
    ) -> None:
        self.error_code = error_code
        self.validation_reason = _redact_sensitive_text(validation_reason)
        self.raw_response_preview = _raw_response_preview(raw_response)
        super().__init__(f"{self.error_code}: {self.validation_reason}")


class NvidiaNimResearchClient:
    """NVIDIA NIM client scoped to structured research outputs only."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str,
        confirm_external_llm: bool,
        api_key: str | None = None,
        base_url: str = DEFAULT_NVIDIA_NIM_BASE_URL,
        usage_log_path: str | Path | None = None,
    ) -> None:
        self._model = model
        self.base_url = base_url
        self._usage_log_path = Path(usage_log_path) if usage_log_path is not None else None
        if client is not None:
            self._client = client
            return
        if not confirm_external_llm:
            raise RuntimeError("confirm_external_llm_required")
        resolved_key = api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY")
        if not resolved_key:
            raise RuntimeError("missing_nvidia_api_key")
        self._client = self._build_default_client(api_key=resolved_key, base_url=base_url)

    def create_structured_output(
        self,
        *,
        schema_name: str,
        user_input: str,
        reasoning_effort: str = "medium",
        verbosity: str = "medium",
    ) -> StructuredOutputResult:
        started = time.perf_counter()
        prompt_hash = _prompt_hash(schema_name=schema_name, model=self._model, user_input=user_input)
        prompt_cache_key = _prompt_cache_key(schema_name=schema_name, model=self._model, prompt_hash=prompt_hash)
        try:
            _guard_user_input(user_input)
            messages = [
                {"role": "system", "content": _nim_instructions(schema_name)},
                {"role": "user", "content": user_input},
            ]
            response, json_mode_fallback = _create_chat_completion(
                self._client,
                model=self._model,
                messages=messages,
            )
            response_text = _chat_completion_text(response)
            try:
                raw_text = _extract_json_object_text(response_text)
                data = json.loads(raw_text)
                validate_against_schema(schema_name, data)
            except (json.JSONDecodeError, ValueError) as exc:
                raise NvidiaNimSchemaError(validation_reason=str(exc), raw_response=response_text) from exc
            latency = time.perf_counter() - started
            usage = getattr(response, "usage", None)
            usage_payload = _usage_to_dict(usage)
            self._write_log(
                {
                    "status": "success",
                    "provider": "nvidia-nim",
                    "schema_name": schema_name,
                    "model": self._model,
                    "prompt_hash": prompt_hash,
                    "prompt_cache_key": prompt_cache_key,
                    "latency_seconds": latency,
                    "usage": usage_payload,
                    "json_mode_fallback": json_mode_fallback,
                }
            )
            return StructuredOutputResult(data, raw_text, usage_payload, latency, prompt_hash, prompt_cache_key)
        except LLMGuardrailError:
            raise
        except Exception as exc:
            self._write_log(
                {
                    "status": "error",
                    "provider": "nvidia-nim",
                    "schema_name": schema_name,
                    "model": self._model,
                    "prompt_hash": prompt_hash,
                    "prompt_cache_key": prompt_cache_key,
                    "latency_seconds": time.perf_counter() - started,
                    "error_type": type(exc).__name__,
                    "error_message": _redact_sensitive_text(str(exc)),
                    "error_code": getattr(exc, "error_code", None),
                    "validation_reason": getattr(exc, "validation_reason", None),
                    "raw_response_preview": getattr(exc, "raw_response_preview", None),
                }
            )
            raise

    def _write_log(self, payload: dict[str, Any]) -> None:
        if self._usage_log_path is None:
            return
        self._usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now(UTC).isoformat(), **payload}
        with self._usage_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    @staticmethod
    def _build_default_client(*, api_key: str, base_url: str) -> Any:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError("openai_package_required_for_nvidia_nim") from exc
        return OpenAI(base_url=base_url, api_key=api_key)


def _nim_instructions(schema_name: str) -> str:
    instructions = (
        f"{_instructions(schema_name)} "
        "Return exactly one JSON object, with no markdown fences, prose, comments, or trailing text."
    )
    if schema_name == "LLMSignalProposal":
        instructions += (
            " For LLMSignalProposal, use this exact JSON shape and field types: "
            '{"proposal_kind": "entry", "symbol": "SPY", "action": "hold", "confidence": 0.5, '
            '"time_horizon": "1d", '
            '"thesis": "paper-only audit", "risk_notes": ["no execution authority"], '
            '"evidence_refs": ["fixture"], "model_id": "nvidia-nim-shadow", '
            '"prompt_version": "signal_proposal_auditor:nim:v1", "input_hashes": {}, '
            '"llm_authority": "none"}.'
        )
    return instructions


def _create_chat_completion(client: Any, *, model: str, messages: list[dict[str, str]]) -> tuple[Any, bool]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 1024,
    }
    try:
        return client.chat.completions.create(**kwargs, response_format={"type": "json_object"}), False
    except Exception as exc:
        if not _json_mode_rejected(exc):
            raise
        return client.chat.completions.create(**kwargs), True


def _json_mode_rejected(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


def _chat_completion_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError("nvidia_nim_chat_completion_missing_choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    raise ValueError("nvidia_nim_chat_completion_missing_content")


def _extract_json_object_text(raw_text: str) -> str:
    decoder = json.JSONDecoder()
    for index, character in enumerate(raw_text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw_text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return raw_text[index : index + end]
    raise ValueError("nvidia_nim_response_missing_json_object")


def _raw_response_preview(raw_response: str) -> str:
    redacted = _redact_sensitive_text(raw_response).replace("token=[redacted]", "token:[redacted]")
    if len(redacted) <= RAW_RESPONSE_PREVIEW_LIMIT:
        return redacted
    return redacted[:RAW_RESPONSE_PREVIEW_LIMIT] + "...[truncated]"


def load_nvidia_model_suite(path: str | Path) -> tuple[NvidiaModelSpec, ...]:
    payload = load_yaml_file(path)
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("model suite must contain a non-empty models list")
    models: list[NvidiaModelSpec] = []
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            raise ValueError("model suite entries must be mappings")
        model_id = str(raw.get("id") or "").strip()
        if model_id not in NVIDIA_NIM_MODEL_ALLOWLIST:
            raise ValueError(f"NVIDIA NIM model is not allowlisted: {model_id}")
        provider = str(raw.get("provider") or "nvidia-nim")
        if provider != "nvidia-nim":
            raise ValueError(f"NVIDIA NIM suite entry has invalid provider: {provider}")
        models.append(
            NvidiaModelSpec(
                id=model_id,
                provider=provider,
                enabled=bool(raw.get("enabled", True)),
                source_url=str(raw.get("source_url") or NVIDIA_MODEL_SOURCE_URL),
                source_timestamp=str(raw.get("source_timestamp") or ""),
            )
        )
    return tuple(models)
