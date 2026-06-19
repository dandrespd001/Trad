"""OpenAI Responses API wrapper for research assistance only."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_ai.llm.schemas import schema_for, validate_against_schema
from trading_ai.llm.model_policy import DEFAULT_OPENAI_MODEL


class LLMGuardrailError(ValueError):
    """Raised when a prompt asks the LLM to bypass trading controls."""


@dataclass(frozen=True)
class StructuredOutputResult:
    data: dict[str, Any]
    raw_text: str
    usage: Any | None
    latency_seconds: float
    prompt_hash: str
    prompt_cache_key: str


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
        model: str = DEFAULT_OPENAI_MODEL,
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
        prompt_hash = _prompt_hash(schema_name=schema_name, model=self._model, user_input=user_input)
        prompt_cache_key = _prompt_cache_key(schema_name=schema_name, model=self._model, prompt_hash=prompt_hash)
        try:
            _guard_user_input(user_input)
        except LLMGuardrailError as exc:
            latency = time.perf_counter() - started
            self._write_log(
                {
                    "status": "blocked",
                    "schema_name": schema_name,
                    "model": self._model,
                    "prompt_hash": prompt_hash,
                    "prompt_cache_key": prompt_cache_key,
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
                prompt_cache_key=prompt_cache_key,
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
                    "prompt_hash": prompt_hash,
                    "prompt_cache_key": prompt_cache_key,
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
                "prompt_hash": prompt_hash,
                "prompt_cache_key": prompt_cache_key,
                "latency_seconds": latency,
                "usage": _usage_to_dict(usage),
            }
        )
        return StructuredOutputResult(
            data=data,
            raw_text=raw_text,
            usage=usage,
            latency_seconds=latency,
            prompt_hash=prompt_hash,
            prompt_cache_key=prompt_cache_key,
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
        raise RuntimeError(
            "External LLM APIs are disabled for runtime trading workflows; "
            "use the local LLM registry and local-transformers commands instead."
        )


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
    lowered = _normalized_prompt(user_input)
    compact = "".join(character for character in lowered if character.isalnum())
    checks = (
        (".env", "secret_read_request"),
        ("leer .env", "secret_read_request"),
        ("read .env", "secret_read_request"),
        ("secret", "secret_read_request"),
        ("secreto", "secret_read_request"),
        ("api key", "secret_read_request"),
        ("access token", "secret_read_request"),
        ("openai key", "secret_read_request"),
        ("alpaca key", "secret_read_request"),
        ("broker credentials", "secret_read_request"),
        ("live trading", "live_trading_request"),
        ("opera en vivo", "live_trading_request"),
        ("trading en vivo", "live_trading_request"),
        ("activate live", "live_trading_request"),
        ("activar live", "live_trading_request"),
        ("send live order", "order_execution_request"),
        ("submit live order", "order_execution_request"),
        ("send order", "order_execution_request"),
        ("submit order", "order_execution_request"),
        ("submit an order", "order_execution_request"),
        ("place order", "order_execution_request"),
        ("place an order", "order_execution_request"),
        ("execute order", "order_execution_request"),
        ("execute an order", "order_execution_request"),
        ("enviar orden", "order_execution_request"),
        ("enviar una orden", "order_execution_request"),
        ("activate alias without scorecard", "alias_activation_request"),
        ("enable alias without scorecard", "alias_activation_request"),
        ("skip scorecard", "alias_activation_request"),
        ("mutate latest_model.json", "model_mutation_request"),
        ("change latest_model.json", "model_mutation_request"),
        ("replace latest_model.json", "model_mutation_request"),
        ("use broker credentials", "broker_access_request"),
        ("read broker credentials", "broker_access_request"),
        ("modify risk", "risk_limit_change_request"),
        ("risk limit", "risk_limit_change_request"),
        ("cambia los limites", "risk_limit_change_request"),
        ("cambiar los limites", "risk_limit_change_request"),
        ("limites de riesgo", "risk_limit_change_request"),
    )
    for fragment, reason in checks:
        if fragment in lowered:
            return PromptSafety(False, reason)
    compact_checks = (
        ("readenv", "secret_read_request"),
        ("apikey", "secret_read_request"),
        ("accesstoken", "secret_read_request"),
        ("openaikey", "secret_read_request"),
        ("alpacakey", "secret_read_request"),
        ("brokercredentials", "secret_read_request"),
        ("livetrading", "live_trading_request"),
        ("submitorder", "order_execution_request"),
        ("submitanorder", "order_execution_request"),
        ("sendorder", "order_execution_request"),
        ("sendliveorder", "order_execution_request"),
        ("submitliveorder", "order_execution_request"),
        ("placeorder", "order_execution_request"),
        ("placeanorder", "order_execution_request"),
        ("executeorder", "order_execution_request"),
        ("executeanorder", "order_execution_request"),
        ("enviarorden", "order_execution_request"),
        ("enviarunaorden", "order_execution_request"),
        ("activatealiaswithoutscorecard", "alias_activation_request"),
        ("enablealiaswithoutscorecard", "alias_activation_request"),
        ("skipscorecard", "alias_activation_request"),
        ("mutatelatestmodeljson", "model_mutation_request"),
        ("changelatestmodeljson", "model_mutation_request"),
        ("replacelatestmodeljson", "model_mutation_request"),
        ("usebrokercredentials", "broker_access_request"),
        ("readbrokercredentials", "broker_access_request"),
        ("modifyrisk", "risk_limit_change_request"),
        ("risklimit", "risk_limit_change_request"),
        ("limitesderiesgo", "risk_limit_change_request"),
    )
    for fragment, reason in compact_checks:
        if fragment in compact:
            return PromptSafety(False, reason)
    return PromptSafety(True)


def _normalized_prompt(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(character for character in decomposed if not unicodedata.combining(character))
    spaced = re.sub(r"[^a-zA-Z0-9._]+", " ", ascii_text.lower())
    return re.sub(r"\s+", " ", spaced).strip()


def _prompt_hash(*, schema_name: str, model: str, user_input: str) -> str:
    material = json.dumps(
        {"schema_name": schema_name, "model": model, "user_input": user_input},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prompt_cache_key(*, schema_name: str, model: str, prompt_hash: str) -> str:
    model_token = re.sub(r"[^a-zA-Z0-9._-]+", "-", model).strip("-") or "model"
    return f"trading-ai:{schema_name}:{model_token}:{prompt_hash[:24]}"


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
