"""OpenAI Responses API wrapper for research assistance only."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.llm.schemas import schema_for, validate_against_schema


class LLMGuardrailError(ValueError):
    """Raised when a prompt asks the LLM to bypass trading controls."""


@dataclass(frozen=True)
class StructuredOutputResult:
    data: dict[str, Any]
    raw_text: str
    usage: Any | None
    latency_seconds: float


@dataclass(frozen=True)
class PromptSafety:
    allowed: bool
    reason: str = "allowed"


class OpenAIResearchClient:
    """Responses API client scoped to analysis/reporting tasks."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = "gpt-5.5",
        usage_log_path: str | Path | None = None,
    ) -> None:
        self._client = client or self._build_default_client()
        self._model = model
        self._usage_log_path = Path(usage_log_path) if usage_log_path is not None else None

    def create_structured_output(
        self,
        *,
        schema_name: str,
        user_input: str,
        reasoning_effort: str = "medium",
        verbosity: str = "medium",
    ) -> StructuredOutputResult:
        started = time.perf_counter()
        try:
            _guard_user_input(user_input)
        except LLMGuardrailError as exc:
            latency = time.perf_counter() - started
            self._write_log(
                {
                    "status": "blocked",
                    "schema_name": schema_name,
                    "model": self._model,
                    "latency_seconds": latency,
                    "error_type": type(exc).__name__,
                    "error_message": _redact_secrets(str(exc)),
                }
            )
            raise
        schema = schema_for(schema_name)
        try:
            response = self._client.responses.create(
                model=self._model,
                instructions=_instructions(schema_name),
                input=user_input,
                reasoning={"effort": reasoning_effort},
                text={
                    "verbosity": verbosity,
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
                store=False,
                prompt_cache_key=f"trading-ai:{schema_name}:v1",
            )
            latency = time.perf_counter() - started
            raw_text = str(response.output_text)
            data = json.loads(raw_text)
            validate_against_schema(schema_name, data)
        except Exception as exc:
            latency = time.perf_counter() - started
            self._write_log(
                {
                    "status": "error",
                    "schema_name": schema_name,
                    "model": self._model,
                    "latency_seconds": latency,
                    "error_type": type(exc).__name__,
                    "error_message": _redact_secrets(str(exc)),
                }
            )
            raise
        usage = getattr(response, "usage", None)
        self._write_log(
            {
                "status": "success",
                "schema_name": schema_name,
                "model": self._model,
                "latency_seconds": latency,
                "usage": _usage_to_dict(usage),
            }
        )
        return StructuredOutputResult(
            data=data,
            raw_text=raw_text,
            usage=usage,
            latency_seconds=latency,
        )

    def _write_log(self, payload: dict[str, Any]) -> None:
        if self._usage_log_path is None:
            return
        self._usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        with self._usage_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    @staticmethod
    def _build_default_client() -> Any:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError("OpenAI integration requires the optional openai package") from exc
        return OpenAI()


def _instructions(schema_name: str) -> str:
    return (
        "You are a research assistant for a paper-trading system. "
        "You may summarize, explain, and propose research tests. "
        "You must not send orders, modify risk limits, read secrets, activate live trading, "
        "or claim authority over broker execution. "
        f"Return only valid JSON matching the {schema_name} schema."
    )


def _guard_user_input(user_input: str) -> None:
    safety = classify_prompt_safety(user_input)
    if not safety.allowed:
        raise LLMGuardrailError(f"request violates LLM trading guardrails: {safety.reason}")


def classify_prompt_safety(user_input: str) -> PromptSafety:
    lowered = user_input.lower()
    checks = (
        (".env", "secret_read_request"),
        ("leer .env", "secret_read_request"),
        ("read .env", "secret_read_request"),
        ("secret", "secret_read_request"),
        ("secreto", "secret_read_request"),
        ("live trading", "live_trading_request"),
        ("opera en vivo", "live_trading_request"),
        ("trading en vivo", "live_trading_request"),
        ("activate live", "live_trading_request"),
        ("activar live", "live_trading_request"),
        ("send live order", "order_execution_request"),
        ("send order", "order_execution_request"),
        ("enviar orden", "order_execution_request"),
        ("modify risk", "risk_limit_change_request"),
        ("risk limit", "risk_limit_change_request"),
        ("cambia los limites", "risk_limit_change_request"),
        ("cambiar los limites", "risk_limit_change_request"),
        ("limites de riesgo", "risk_limit_change_request"),
    )
    for fragment, reason in checks:
        if fragment in lowered:
            return PromptSafety(False, reason)
    return PromptSafety(True)


def _usage_to_dict(usage: Any | None) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if hasattr(usage, "__dict__"):
        return dict(usage.__dict__)
    return {"value": str(usage)}


def _redact_secrets(value: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED_OPENAI_KEY]", value)
