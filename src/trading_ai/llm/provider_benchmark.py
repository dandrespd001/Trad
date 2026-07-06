"""Governed external LLM provider benchmarks."""

from __future__ import annotations

import json
from json import JSONDecodeError
from dataclasses import dataclass
from pathlib import Path

from trading_ai.execution.paper_common import redact_secrets, write_json_artifact, write_text_artifact
from trading_ai.llm.factory import ROLE_POLICIES
from trading_ai.llm.nvidia_nim import NvidiaNimResearchClient, load_nvidia_model_suite


@dataclass(frozen=True)
class LlmProviderBenchmarkResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path


def run_llm_provider_benchmark(
    *,
    provider: str,
    model_suite: str | Path,
    role: str,
    as_of_date: str,
    output_dir: str | Path = "reports/tmp/llm_provider_benchmark",
    confirm_external_llm: bool = False,
) -> LlmProviderBenchmarkResult:
    if provider != "nvidia-nim":
        raise ValueError("only nvidia-nim provider is supported")
    policy = ROLE_POLICIES.get(role)
    if policy is None:
        raise ValueError(f"unknown LLM role: {role}")
    models = tuple(model for model in load_nvidia_model_suite(model_suite) if model.enabled)
    output_root = Path(output_dir) / provider / role / as_of_date
    output_path = output_root / "benchmark.json"
    markdown_path = output_root / "benchmark.md"

    if not confirm_external_llm:
        payload = _base_payload(provider, role, as_of_date, model_suite)
        payload.update(
            {
                "status": "BLOCKED",
                "external_llm_requested": True,
                "external_llm_used": False,
                "models": [_model_payload(model, status="blocked_missing_confirmation") for model in models],
                "errors": [{"code": "missing_confirm_external_llm", "message": "external LLM calls require confirmation"}],
            }
        )
        return _write_result(payload, output_path, markdown_path, exit_code=2)

    results: list[dict[str, object]] = []
    for model in models:
        try:
            client = NvidiaNimResearchClient(model=model.id, confirm_external_llm=True)
            result = client.create_structured_output(
                schema_name=str(policy["schema_name"]),
                user_input=_smoke_prompt(role=role, as_of_date=as_of_date),
            )
            results.append(
                {
                    **_model_payload(model, status="ok"),
                    "latency_seconds": result.latency_seconds,
                    "prompt_hash": result.prompt_hash,
                    "usage": result.usage,
                }
            )
        except Exception as exc:
            error_details = _provider_error_details(exc)
            results.append(
                {
                    **_model_payload(model, status="error"),
                    "error_type": type(exc).__name__,
                    "error_code": error_details["error_code"],
                    **{key: value for key, value in error_details.items() if key != "error_code"},
                }
            )
    ok_count = sum(1 for result in results if result.get("status") == "ok")
    error_count = sum(1 for result in results if result.get("status") == "error")
    status = "PARTIAL" if ok_count and error_count else "OK" if ok_count else "ERROR"
    payload = _base_payload(provider, role, as_of_date, model_suite)
    payload.update(
        {
            "status": status,
            "external_llm_requested": True,
            "external_llm_used": ok_count > 0,
            "models": results,
            "errors": [result for result in results if result.get("status") == "error"],
        }
    )
    return _write_result(
        payload,
        output_path,
        markdown_path,
        exit_code=0 if status == "OK" else 1 if status == "PARTIAL" else 2,
    )


def _provider_error_code(exc: Exception) -> str:
    explicit_code = getattr(exc, "error_code", None)
    if isinstance(explicit_code, str) and explicit_code:
        return explicit_code
    if type(exc).__name__ == "NotFoundError":
        return "provider_model_unavailable"
    if isinstance(exc, (JSONDecodeError, ValueError)):
        return "provider_schema_error"
    message = str(exc).lower()
    if "api_key" in message or "api key" in message or "credential" in message:
        return "provider_credentials_unavailable"
    if "confirm_external_llm" in message:
        return "external_llm_confirmation_required"
    if "schema" in message or "json" in message:
        return "provider_schema_error"
    return "provider_model_error"


def _provider_error_details(exc: Exception) -> dict[str, object]:
    details: dict[str, object] = {"error_code": _provider_error_code(exc)}
    validation_reason = getattr(exc, "validation_reason", None)
    if isinstance(validation_reason, str) and validation_reason:
        details["validation_reason"] = redact_secrets(validation_reason)
    raw_response_preview = getattr(exc, "raw_response_preview", None)
    if isinstance(raw_response_preview, str) and raw_response_preview:
        details["raw_response_preview"] = redact_secrets(raw_response_preview)
    return details


def _base_payload(provider: str, role: str, as_of_date: str, model_suite: str | Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "provider": provider,
        "role": role,
        "as_of_date": as_of_date,
        "model_suite": str(model_suite),
        "authority": {"llm_authority": "none", "broker_client_built": False, "credentials_read": False},
        "safety": {"paper_only": True, "orders_submitted": False, "llm_authority": "none"},
    }


def _model_payload(model: object, *, status: str) -> dict[str, object]:
    return {
        "model_id": getattr(model, "id"),
        "provider": getattr(model, "provider"),
        "enabled": getattr(model, "enabled"),
        "status": status,
        "source_url": getattr(model, "source_url"),
        "source_timestamp": getattr(model, "source_timestamp"),
    }


def _smoke_prompt(*, role: str, as_of_date: str) -> str:
    if role == "signal_proposal_auditor":
        return (
            f"Paper-only signal proposal audit for {as_of_date}. "
            "Return one hold recommendation for SPY with llm_authority none and no broker authority."
        )
    return f"Paper-only operational review for {as_of_date}; return llm_authority none and no broker authority."


def _write_result(
    payload: dict[str, object],
    output_path: Path,
    markdown_path: Path,
    *,
    exit_code: int,
) -> LlmProviderBenchmarkResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_markdown(payload), markdown_path)
    return LlmProviderBenchmarkResult(exit_code, str(payload["status"]), output_path, markdown_path)


def _render_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# LLM Provider Benchmark",
        "",
        f"- Provider: `{payload.get('provider')}`",
        f"- Role: `{payload.get('role')}`",
        f"- Status: `{payload.get('status')}`",
        f"- External LLM used: `{payload.get('external_llm_used')}`",
        "",
        "| Model | Status |",
        "| --- | --- |",
    ]
    for model in payload.get("models", []):
        item = model if isinstance(model, dict) else {}
        lines.append(f"| `{item.get('model_id')}` | `{item.get('status')}` |")
    lines.append("")
    if payload.get("errors"):
        lines.append("```json")
        lines.append(json.dumps(payload["errors"], indent=2, sort_keys=True))
        lines.append("```")
    return "\n".join(lines)
