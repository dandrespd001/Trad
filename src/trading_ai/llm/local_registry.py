"""Local-only LLM registry, cache, tuning, and alias workflows."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trading_ai.execution.paper_common import redact_secrets, read_json_artifact, write_json_artifact, write_text_artifact
from trading_ai.llm.schemas import validate_against_schema


SCHEMA_VERSION = "1.0"
DEFAULT_LOCAL_MODEL_REGISTRY = "configs/llm_local_models.json"
DEFAULT_LOCAL_CACHE_ROOT = "models/local/weights"
DEFAULT_LOCAL_CACHE_REPORT = "reports/tmp/llm_local/cache_verify.json"
DEFAULT_LOCAL_SMOKE_REPORT = "reports/tmp/llm_local/smoke.json"
DEFAULT_LOCAL_SMOKE_PROMPT = (
    "Return only a JSON object matching the PaperOpsReview schema with keys operational_status, risks, "
    "blockers, recommendation, reasoning, human_review_required, and llm_authority. Use operational_status "
    '"OK", recommendation "READY_FOR_PAPER_CONFIRMATION", human_review_required true, llm_authority "none", '
    "and include no markdown or extra text."
)
DEFAULT_LOCAL_SFT_MANIFEST = "reports/tmp/llm_local_sft/manifest.json"
DEFAULT_LOCAL_EVAL_OUTPUT_DIR = "reports/tmp/llm_local_eval_suite"
DEFAULT_LOCAL_ADAPTER_OUTPUT_DIR = "reports/tmp/llm_local_adapters"
DEFAULT_LOCAL_ALIAS_OUTPUT_DIR = "reports/tmp/llm_local_alias"

STATE_ACTIVE_LOCAL_ALIAS = "ACTIVE_LOCAL_LLM_ALIAS"
STATE_BLOCKED = "BLOCKED"
MODEL_WEIGHT_FILE_PATTERNS = (
    "*.safetensors",
    "pytorch_model*.bin",
    "model.safetensors.index.json",
    "pytorch_model.bin.index.json",
    "*.gguf",
)
MIN_MODEL_WEIGHT_FILE_BYTES = 1024
RAW_TEXT_PREVIEW_LIMIT = 4096
DEFAULT_LOCAL_SFT_EPOCHS = 1.0
DEFAULT_LOCAL_SFT_LEARNING_RATE = 2e-4
DEFAULT_LOCAL_SFT_BATCH_SIZE = 1
DEFAULT_LOCAL_SFT_GRADIENT_ACCUMULATION_STEPS = 1
DEFAULT_LOCAL_SFT_MAX_STEPS = -1
DEFAULT_LOCAL_SFT_LORA_RANK = 8
DEFAULT_LOCAL_SFT_LORA_ALPHA = 16
DEFAULT_LOCAL_SFT_LORA_DROPOUT = 0.05
DEFAULT_LOCAL_SFT_DTYPE = "auto"
DEFAULT_LOCAL_SFT_DEVICE = "auto"
DEFAULT_LOCAL_SFT_TRAINING_CONFIG = {
    "epochs": DEFAULT_LOCAL_SFT_EPOCHS,
    "learning_rate": DEFAULT_LOCAL_SFT_LEARNING_RATE,
    "batch_size": DEFAULT_LOCAL_SFT_BATCH_SIZE,
    "gradient_accumulation_steps": DEFAULT_LOCAL_SFT_GRADIENT_ACCUMULATION_STEPS,
    "max_steps": DEFAULT_LOCAL_SFT_MAX_STEPS,
    "lora_rank": DEFAULT_LOCAL_SFT_LORA_RANK,
    "lora_alpha": DEFAULT_LOCAL_SFT_LORA_ALPHA,
    "lora_dropout": DEFAULT_LOCAL_SFT_LORA_DROPOUT,
    "dtype": DEFAULT_LOCAL_SFT_DTYPE,
    "device": DEFAULT_LOCAL_SFT_DEVICE,
}


@dataclass(frozen=True)
class LlmLocalResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path | None
    payload: dict[str, object]


def run_llm_local_cache_verify(
    *,
    model_id: str,
    registry: str | Path = DEFAULT_LOCAL_MODEL_REGISTRY,
    cache_root: str | Path = DEFAULT_LOCAL_CACHE_ROOT,
    output: str | Path = DEFAULT_LOCAL_CACHE_REPORT,
    generated_at: str | None = None,
) -> LlmLocalResult:
    payload = verify_local_model_cache(
        model_id=model_id,
        registry=registry,
        cache_root=cache_root,
        generated_at=generated_at,
    )
    output_path = Path(output)
    write_json_artifact(payload, output_path)
    return LlmLocalResult(
        exit_code=0 if payload["cache_state"] == "READY" else 1,
        status=str(payload["cache_state"]),
        output_path=output_path,
        markdown_path=None,
        payload=payload,
    )


def verify_local_model_cache(
    *,
    model_id: str,
    registry: str | Path = DEFAULT_LOCAL_MODEL_REGISTRY,
    cache_root: str | Path = DEFAULT_LOCAL_CACHE_ROOT,
    generated_at: str | None = None,
) -> dict[str, object]:
    registry_path = Path(registry)
    registry_missing = not registry_path.exists()
    registry_payload = _load_registry(registry_path)
    entry = None if registry_missing else _find_model(registry_payload, model_id)
    blockers: list[str] = []
    required_files: list[str] = []
    weight_file_patterns = list(MODEL_WEIGHT_FILE_PATTERNS)
    weight_files: list[str] = []
    minimum_weight_file_bytes = MIN_MODEL_WEIGHT_FILE_BYTES
    minimum_total_weight_bytes = 0
    weight_total_bytes = 0
    if registry_missing:
        blockers.append("missing_local_model_registry")
        model_path = Path(cache_root) / _slug_model_id(model_id)
    elif entry is None:
        blockers.append("model_not_in_local_registry")
        model_path = Path(cache_root) / _slug_model_id(model_id)
    else:
        model_path = _model_path(entry, cache_root=cache_root)
        registry_required = entry.get("required_files")
        required_items = registry_required if isinstance(registry_required, list) else ["config.json", "tokenizer_config.json"]
        required_files = [str(item) for item in required_items]
        registry_weight_patterns = entry.get("weight_file_patterns")
        if isinstance(registry_weight_patterns, list) and registry_weight_patterns:
            weight_file_patterns = [str(item) for item in registry_weight_patterns]
        registry_weight_files = entry.get("weight_files")
        if isinstance(registry_weight_files, list):
            weight_files = [str(item) for item in registry_weight_files]
        minimum_weight_file_bytes = _positive_int(
            entry.get("minimum_weight_file_bytes"),
            default=MIN_MODEL_WEIGHT_FILE_BYTES,
        )
        minimum_total_weight_bytes = _positive_int(
            entry.get("minimum_total_weight_bytes"),
            default=0,
        )
    if entry is not None:
        if not model_path.is_dir():
            blockers.append("missing_local_model_dir")
        else:
            for required in required_files:
                if not (model_path / required).exists():
                    blockers.append(f"missing_required_file:{required}")
            weight_total_bytes = _weight_total_bytes(
                model_path,
                weight_files=weight_files,
                weight_file_patterns=weight_file_patterns,
            )
            blockers.extend(
                _weight_file_blockers(
                    model_path,
                    weight_files=weight_files,
                    weight_file_patterns=weight_file_patterns,
                    minimum_weight_file_bytes=minimum_weight_file_bytes,
                )
            )
            if minimum_total_weight_bytes > 0 and weight_total_bytes < minimum_total_weight_bytes:
                blockers.append(
                    f"insufficient_total_weight_bytes:{weight_total_bytes}:{minimum_total_weight_bytes}"
                )
    state = "READY" if not blockers else "MISSING"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "cache_state": state,
        "status": state,
        "model_id": model_id,
        "model_path": str(model_path),
        "registry": str(registry_path),
        "cache_root": str(Path(cache_root)),
        "license": str(entry.get("license") or "") if entry is not None else None,
        "parameter_count": entry.get("parameter_count") if entry is not None else None,
        "required_files": required_files,
        "weight_files": weight_files,
        "weight_file_patterns": weight_file_patterns,
        "minimum_weight_file_bytes": minimum_weight_file_bytes,
        "weight_total_bytes": weight_total_bytes,
        "minimum_total_weight_bytes": minimum_total_weight_bytes,
        "blockers": blockers,
        "local_files_only": True,
        "network_allowed": False,
        "transformers_kwargs": {"local_files_only": True, "trust_remote_code": False},
        "authority": _authority(),
        "safety": _safety(),
    }


def run_llm_local_smoke(
    *,
    model_id: str,
    registry: str | Path = DEFAULT_LOCAL_MODEL_REGISTRY,
    cache_root: str | Path = DEFAULT_LOCAL_CACHE_ROOT,
    schema_name: str = "PaperOpsReview",
    prompt: str = DEFAULT_LOCAL_SMOKE_PROMPT,
    output: str | Path = DEFAULT_LOCAL_SMOKE_REPORT,
    max_new_tokens: int = 256,
    fixture_response: str | Path | None = None,
    adapter_manifest: str | Path | None = None,
    generated_at: str | None = None,
) -> LlmLocalResult:
    cache = verify_local_model_cache(model_id=model_id, registry=registry, cache_root=cache_root, generated_at=generated_at)
    output_path = Path(output)
    adapter = _adapter_manifest_metadata(adapter_manifest, expected_base_model_id=model_id) if adapter_manifest else None
    if cache["cache_state"] != "READY":
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "smoke_state": "BLOCKED",
            "status": "BLOCKED",
            "model_id": model_id,
            "cache": cache,
            "schema_name": schema_name,
            "schema_passed": False,
            "fixture_response_used": fixture_response is not None,
            "model_loaded": False,
            "adapter_loaded": False,
            "adapter_manifest": str(Path(adapter_manifest)) if adapter_manifest else None,
            "adapter_hash": _mapping(adapter).get("adapter_hash") if adapter else None,
            "base_model_id": model_id,
            "blockers": [str(item) for item in _object_list(cache.get("blockers"))],
            "local_files_only": True,
            "network_allowed": False,
            "authority": _authority(),
            "safety": _safety(),
        }
        write_json_artifact(payload, output_path)
        return LlmLocalResult(1, "BLOCKED", output_path, None, payload)
    adapter_blockers = _string_list(_mapping(adapter).get("blockers")) if adapter else []
    if adapter_blockers:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or _utc_now(),
            "smoke_state": "BLOCKED",
            "status": "BLOCKED",
            "model_id": model_id,
            "model_path": cache["model_path"],
            "schema_name": schema_name,
            "schema_passed": False,
            "fixture_response_used": fixture_response is not None,
            "model_loaded": False,
            "adapter_loaded": False,
            "adapter_manifest": str(Path(adapter_manifest)) if adapter_manifest else None,
            "adapter_hash": _mapping(adapter).get("adapter_hash") if adapter else None,
            "base_model_id": model_id,
            "blockers": adapter_blockers,
            "local_files_only": True,
            "network_allowed": False,
            "authority": _authority(),
            "safety": _safety(),
        }
        write_json_artifact(payload, output_path)
        return LlmLocalResult(1, "BLOCKED", output_path, None, payload)
    started = time.perf_counter()
    fixture_used = fixture_response is not None
    adapter_path = Path(str(_mapping(adapter).get("adapter_path"))) if adapter else None
    try:
        raw_text = (
            Path(fixture_response).read_text(encoding="utf-8")
            if fixture_response is not None
            else _generate_local_text(
                model_path=Path(str(cache["model_path"])),
                prompt=prompt,
                max_new_tokens=max_new_tokens,
                adapter_path=adapter_path,
            )
        )
        parsed = _parse_json_object(raw_text)
        validate_against_schema(schema_name, parsed)
        schema_passed = True
        blockers: list[str] = []
    except Exception as exc:
        raw_text = locals().get("raw_text", "")
        parsed = {}
        schema_passed = False
        blockers = [f"schema_or_generation_failed:{exc}"]
    state = "FIXTURE_PASSED" if schema_passed and fixture_used else "PASSED" if schema_passed else "FAILED"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "smoke_state": state,
        "status": state,
        "model_id": model_id,
        "model_path": cache["model_path"],
        "schema_name": schema_name,
        "schema_passed": schema_passed,
        "fixture_response_used": fixture_used,
        "model_loaded": schema_passed and not fixture_used,
        "adapter_loaded": bool(adapter and schema_passed and not fixture_used),
        "adapter_manifest": str(Path(adapter_manifest)) if adapter_manifest else None,
        "adapter_hash": _mapping(adapter).get("adapter_hash") if adapter else None,
        "base_model_id": _mapping(adapter).get("base_model_id") if adapter else model_id,
        "response": parsed,
        "raw_text_preview": _raw_text_preview(raw_text),
        "latency_seconds": time.perf_counter() - started,
        "blockers": blockers,
        "local_files_only": True,
        "network_allowed": False,
        "authority": _authority(),
        "safety": _safety(),
    }
    write_json_artifact(payload, output_path)
    return LlmLocalResult(0 if schema_passed else 1, state, output_path, None, payload)


def run_llm_local_sft(
    *,
    role: str,
    base_model_id: str,
    training_jsonl: str | Path,
    adapter_dir: str | Path,
    output: str | Path = DEFAULT_LOCAL_SFT_MANIFEST,
    registry: str | Path = DEFAULT_LOCAL_MODEL_REGISTRY,
    cache_root: str | Path = DEFAULT_LOCAL_CACHE_ROOT,
    metrics: Mapping[str, object] | None = None,
    register_existing_adapter: bool = False,
    epochs: float = 1.0,
    learning_rate: float = 2e-4,
    batch_size: int = 1,
    gradient_accumulation_steps: int = 1,
    max_steps: int = -1,
    lora_rank: int = 8,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    dtype: str = "auto",
    device: str = "auto",
    generated_at: str | None = None,
) -> LlmLocalResult:
    training_path = Path(training_jsonl)
    adapter_path = Path(adapter_dir)
    output_path = Path(output)
    dataset_hash = _sha256_file(training_path) if training_path.is_file() else ""
    training_config = _training_config(
        epochs=epochs,
        learning_rate=learning_rate,
        batch_size=batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        max_steps=max_steps,
        lora_rank=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        dtype=dtype,
        device=device,
    )
    blockers: list[str] = _role_blockers(role)
    if not training_path.is_file():
        blockers.append("missing_training_jsonl")
    if register_existing_adapter:
        if not adapter_path.is_dir():
            blockers.append("missing_adapter_dir")
        elif not any(adapter_path.iterdir()):
            blockers.append("empty_adapter_dir")
        state = "ADAPTER_REGISTERED" if not blockers else "BLOCKED"
        payload = _local_sft_payload(
            state=state,
            role=role,
            base_model_id=base_model_id,
            training_jsonl=training_path,
            adapter_dir=adapter_path,
            dataset_hash=dataset_hash,
            adapter_hash=_sha256_tree(adapter_path) if adapter_path.exists() else None,
            metrics=dict(metrics or {}),
            training_config=training_config,
            blockers=blockers,
            registry=registry,
            cache_root=cache_root,
            generated_at=generated_at,
        )
        write_json_artifact(payload, output_path)
        return LlmLocalResult(0 if state == "ADAPTER_REGISTERED" else 1, state, output_path, None, payload)

    cache = verify_local_model_cache(
        model_id=base_model_id,
        registry=registry,
        cache_root=cache_root,
        generated_at=generated_at,
    )
    if cache["cache_state"] != "READY":
        blockers.extend(str(item) for item in _object_list(cache.get("blockers")))
    if blockers:
        payload = _local_sft_payload(
            state="BLOCKED",
            role=role,
            base_model_id=base_model_id,
            training_jsonl=training_path,
            adapter_dir=adapter_path,
            dataset_hash=dataset_hash,
            adapter_hash=None,
            metrics=dict(metrics or {}),
            training_config=training_config,
            blockers=blockers,
            registry=registry,
            cache_root=cache_root,
            generated_at=generated_at,
        )
        write_json_artifact(payload, output_path)
        return LlmLocalResult(1, "BLOCKED", output_path, None, payload)
    try:
        training_metrics = _run_transformers_lora_sft(
            model_path=Path(str(cache["model_path"])),
            training_jsonl=training_path,
            adapter_dir=adapter_path,
            training_config=training_config,
        )
    except Exception as exc:
        payload = _local_sft_payload(
            state="BLOCKED",
            role=role,
            base_model_id=base_model_id,
            training_jsonl=training_path,
            adapter_dir=adapter_path,
            dataset_hash=dataset_hash,
            adapter_hash=None,
            metrics=dict(metrics or {}),
            training_config=training_config,
            blockers=[f"local_sft_failed:{exc}"],
            registry=registry,
            cache_root=cache_root,
            generated_at=generated_at,
        )
        write_json_artifact(payload, output_path)
        return LlmLocalResult(2, "BLOCKED", output_path, None, payload)
    merged_metrics = {**training_metrics, **dict(metrics or {})}
    payload = _local_sft_payload(
        state="LOCAL_SFT_COMPLETED",
        role=role,
        base_model_id=base_model_id,
        training_jsonl=training_path,
        adapter_dir=adapter_path,
        dataset_hash=dataset_hash,
        adapter_hash=_sha256_tree(adapter_path),
        metrics=merged_metrics,
        training_config=training_config,
        blockers=[],
        registry=registry,
        cache_root=cache_root,
        generated_at=generated_at,
    )
    write_json_artifact(payload, output_path)
    return LlmLocalResult(0, "LOCAL_SFT_COMPLETED", output_path, None, payload)


def run_llm_local_eval_suite(
    *,
    role: str,
    candidate: str | Path,
    holdout: str | Path,
    base_model_id: str,
    adapter_manifest: str | Path,
    output_dir: str | Path = DEFAULT_LOCAL_EVAL_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LlmLocalResult:
    from trading_ai.llm.factory import run_llm_eval_suite

    result = run_llm_eval_suite(role=role, candidate=candidate, holdout=holdout, output_dir=output_dir, generated_at=generated_at)
    payload = dict(result.payload)
    payload["local_model"] = {
        "base_model_id": base_model_id,
        "adapter_manifest": str(Path(adapter_manifest)),
        "local_files_only": True,
        "network_allowed": False,
    }
    write_json_artifact(payload, result.output_path)
    if result.markdown_path is not None:
        write_text_artifact(_render_local_eval_markdown(payload), result.markdown_path)
    return LlmLocalResult(result.exit_code, result.status, result.output_path, result.markdown_path, payload)


def run_llm_local_adapter_report(
    *,
    role: str,
    sft_manifest: str | Path,
    eval_report: str | Path,
    smoke_report: str | Path | None = None,
    output_dir: str | Path = DEFAULT_LOCAL_ADAPTER_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LlmLocalResult:
    sft = read_json_artifact(sft_manifest)
    eval_payload = read_json_artifact(eval_report)
    smoke_payload = read_json_artifact(smoke_report) if smoke_report is not None else None
    blockers: list[str] = _role_blockers(role)
    if str(sft.get("role_id") or "") != role:
        blockers.append("role_mismatch")
    if str(sft.get("sft_state") or "").upper() not in {"ADAPTER_REGISTERED", "LOCAL_SFT_COMPLETED"}:
        blockers.append("adapter_training_not_ready")
    if str(eval_payload.get("eval_state") or "").upper() != "PASSED":
        blockers.append("eval_not_passed")
    if not str(sft.get("adapter_hash") or ""):
        blockers.append("missing_adapter_hash")
    if smoke_payload is not None:
        smoke_state = str(smoke_payload.get("smoke_state") or smoke_payload.get("status") or "").upper()
        if smoke_state != "PASSED":
            blockers.append("smoke_not_passed")
        if smoke_payload.get("adapter_loaded") is not True:
            blockers.append("smoke_adapter_not_loaded")
        smoke_hash = str(smoke_payload.get("adapter_hash") or "")
        sft_hash = str(sft.get("adapter_hash") or "")
        if smoke_hash and sft_hash and smoke_hash != sft_hash:
            blockers.append("smoke_adapter_hash_mismatch")
    state = "READY_FOR_LOCAL_ALIAS" if not blockers else "REJECTED"
    output_root = Path(output_dir) / role
    output_path = output_root / "adapter_report.json"
    markdown_path = output_root / "adapter_report.md"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "adapter_state": state,
        "status": state,
        "role_id": role,
        "base_model_id": sft.get("base_model_id"),
        "adapter_path": sft.get("adapter_path"),
        "adapter_hash": sft.get("adapter_hash"),
        "dataset_hash": sft.get("dataset_hash"),
        "sft_manifest": str(Path(sft_manifest)),
        "eval_report": str(Path(eval_report)),
        "smoke_report": str(Path(smoke_report)) if smoke_report is not None else None,
        "metrics": _mapping(eval_payload.get("metrics")),
        "blockers": blockers,
        "authority": _authority(),
        "safety": _safety(),
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_adapter_report_markdown(payload), markdown_path)
    return LlmLocalResult(0 if state == "READY_FOR_LOCAL_ALIAS" else 1, state, output_path, markdown_path, payload)


def run_llm_local_alias_decision(
    *,
    role: str,
    adapter_report: str | Path,
    reviewer: str,
    reason: str,
    decision: str,
    ttl_days: int = 30,
    output_dir: str | Path = DEFAULT_LOCAL_ALIAS_OUTPUT_DIR,
    generated_at: str | None = None,
) -> LlmLocalResult:
    generated = generated_at or _utc_now()
    report = read_json_artifact(adapter_report)
    blockers: list[str] = _role_blockers(role)
    if str(report.get("adapter_state") or "").upper() != "READY_FOR_LOCAL_ALIAS":
        blockers.append("adapter_not_ready")
    if str(report.get("role_id") or "") != role:
        blockers.append("role_mismatch")
    if decision.upper() != "APPROVE":
        blockers.append("human_approval_missing")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    state = STATE_ACTIVE_LOCAL_ALIAS if not blockers else STATE_BLOCKED
    created = date.fromisoformat(generated[:10])
    alias_material = {
        "role_id": role,
        "base_model_id": report.get("base_model_id"),
        "adapter_path": report.get("adapter_path"),
        "adapter_hash": report.get("adapter_hash"),
        "adapter_report": str(Path(adapter_report)),
        "reviewer": reviewer,
        "reason": reason,
        "decision": decision.upper(),
    }
    output_root = Path(output_dir) / role
    output_path = output_root / "current.json"
    markdown_path = output_root / "current.md"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "alias_state": state,
        "status": state,
        "role_id": role,
        "base_model_id": report.get("base_model_id") if state == STATE_ACTIVE_LOCAL_ALIAS else None,
        "adapter_path": report.get("adapter_path") if state == STATE_ACTIVE_LOCAL_ALIAS else None,
        "adapter_hash": report.get("adapter_hash") if state == STATE_ACTIVE_LOCAL_ALIAS else report.get("adapter_hash"),
        "adapter_report": str(Path(adapter_report)),
        "alias_hash": _stable_hash(alias_material) if state == STATE_ACTIVE_LOCAL_ALIAS else None,
        "created_on": created.isoformat(),
        "expires_on": (created + timedelta(days=ttl_days)).isoformat(),
        "ttl_days": ttl_days,
        "reviewer": reviewer,
        "reason": reason,
        "decision": decision.upper(),
        "blockers": blockers,
        "authority": _authority(human_review_required=True),
        "safety": _safety(),
        "mutates_latest_model": False,
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_local_alias_markdown(payload), markdown_path)
    return LlmLocalResult(0 if state == STATE_ACTIVE_LOCAL_ALIAS else 1, state, output_path, markdown_path, payload)


def _load_registry(path: str | Path) -> dict[str, object]:
    registry_path = Path(path)
    if not registry_path.exists():
        return {"models": []}
    return read_json_artifact(registry_path)


def _role_blockers(role: str) -> list[str]:
    from trading_ai.llm.factory import ROLE_POLICIES

    return [] if role in ROLE_POLICIES else ["unknown_llm_role"]


def _find_model(registry_payload: Mapping[str, object], model_id: str) -> Mapping[str, object] | None:
    for item in _object_list(registry_payload.get("models")):
        if isinstance(item, Mapping) and str(item.get("model_id") or "") == model_id:
            return item
    return None


def _model_path(entry: Mapping[str, object], *, cache_root: str | Path) -> Path:
    local_dir = Path(str(entry.get("local_dir") or _slug_model_id(str(entry.get("model_id") or "model"))))
    return local_dir if local_dir.is_absolute() else Path(cache_root) / local_dir


def _training_config(
    *,
    epochs: float,
    learning_rate: float,
    batch_size: int,
    gradient_accumulation_steps: int,
    max_steps: int,
    lora_rank: int,
    lora_alpha: int,
    lora_dropout: float,
    dtype: str,
    device: str,
) -> dict[str, object]:
    config = dict(DEFAULT_LOCAL_SFT_TRAINING_CONFIG)
    config.update(
        {
            "epochs": _positive_float(epochs, default=DEFAULT_LOCAL_SFT_EPOCHS),
            "learning_rate": _positive_float(
                learning_rate,
                default=DEFAULT_LOCAL_SFT_LEARNING_RATE,
            ),
            "batch_size": _positive_int(batch_size, default=DEFAULT_LOCAL_SFT_BATCH_SIZE),
            "gradient_accumulation_steps": _positive_int(
                gradient_accumulation_steps,
                default=DEFAULT_LOCAL_SFT_GRADIENT_ACCUMULATION_STEPS,
            ),
            "max_steps": _int_value(max_steps, default=DEFAULT_LOCAL_SFT_MAX_STEPS),
            "lora_rank": _positive_int(lora_rank, default=DEFAULT_LOCAL_SFT_LORA_RANK),
            "lora_alpha": _positive_int(lora_alpha, default=DEFAULT_LOCAL_SFT_LORA_ALPHA),
            "lora_dropout": _non_negative_float(
                lora_dropout,
                default=DEFAULT_LOCAL_SFT_LORA_DROPOUT,
            ),
            "dtype": str(dtype or DEFAULT_LOCAL_SFT_DTYPE),
            "device": str(device or DEFAULT_LOCAL_SFT_DEVICE),
        }
    )
    return config


def _adapter_manifest_metadata(
    manifest_path: str | Path | None,
    *,
    expected_base_model_id: str,
) -> dict[str, object]:
    if manifest_path is None:
        return {}
    path = Path(manifest_path)
    blockers: list[str] = []
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "adapter_manifest": str(path),
            "adapter_path": "",
            "adapter_hash": None,
            "base_model_id": expected_base_model_id,
            "blockers": [f"invalid_adapter_manifest:{exc}"],
        }
    base_model_id = str(payload.get("base_model_id") or "")
    if base_model_id != expected_base_model_id:
        blockers.append("adapter_base_model_mismatch")
    state = str(payload.get("sft_state") or payload.get("status") or "").upper()
    if state not in {"LOCAL_SFT_COMPLETED", "ADAPTER_REGISTERED"}:
        blockers.append("adapter_training_not_ready")
    adapter_hash = str(payload.get("adapter_hash") or "")
    if not adapter_hash:
        blockers.append("missing_adapter_hash")
    raw_adapter_path = str(payload.get("adapter_path") or "")
    adapter_path = Path(raw_adapter_path)
    if not raw_adapter_path:
        blockers.append("missing_adapter_path")
    elif not adapter_path.is_dir():
        blockers.append("missing_adapter_dir")
    return {
        "adapter_manifest": str(path),
        "adapter_path": str(adapter_path),
        "adapter_hash": adapter_hash or None,
        "base_model_id": base_model_id or expected_base_model_id,
        "blockers": blockers,
    }


def _weight_file_blockers(
    model_path: Path,
    *,
    weight_files: list[str],
    weight_file_patterns: list[str],
    minimum_weight_file_bytes: int,
) -> list[str]:
    if weight_files:
        blockers: list[str] = []
        for relative_path in weight_files:
            candidate = model_path / relative_path
            if not candidate.exists():
                blockers.append(f"missing_weight_file:{relative_path}")
            elif not _valid_weight_file(candidate, minimum_weight_file_bytes=minimum_weight_file_bytes):
                blockers.append(f"invalid_weight_file:{relative_path}")
        return blockers

    candidates = _weight_file_candidates(model_path, weight_file_patterns)
    if not candidates:
        return ["missing_weight_file"]
    invalid = [
        candidate.name
        for candidate in candidates
        if not _valid_weight_file(candidate, minimum_weight_file_bytes=minimum_weight_file_bytes)
    ]
    if len(invalid) == len(candidates):
        return [f"invalid_weight_file:{invalid[0]}"]
    return []


def _weight_file_candidates(model_path: Path, weight_file_patterns: list[str]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in weight_file_patterns:
        for candidate in model_path.glob(pattern):
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _weight_total_bytes(
    model_path: Path,
    *,
    weight_files: list[str],
    weight_file_patterns: list[str],
) -> int:
    candidates = (
        [model_path / relative_path for relative_path in weight_files]
        if weight_files
        else _weight_file_candidates(model_path, weight_file_patterns)
    )
    total = 0
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _valid_weight_file(path: Path, *, minimum_weight_file_bytes: int) -> bool:
    try:
        return path.stat().st_size >= minimum_weight_file_bytes
    except OSError:
        return False


def _positive_int(value: object, *, default: int) -> int:
    if not isinstance(value, (int, str)):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _int_value(value: object, *, default: int) -> int:
    if not isinstance(value, (int, str)):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _positive_float(value: object, *, default: float) -> float:
    if not isinstance(value, (float, int, str)):
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: object, *, default: float) -> float:
    if not isinstance(value, (float, int, str)):
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _model_load_kwargs(*, dtype: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"local_files_only": True, "trust_remote_code": False}
    normalized = str(dtype or "auto")
    if normalized == "auto":
        return kwargs
    try:
        import torch
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"torch is required for dtype={normalized}") from exc
    if not hasattr(torch, normalized):
        raise ValueError(f"unsupported torch dtype: {normalized}")
    kwargs["torch_dtype"] = getattr(torch, normalized)
    return kwargs


def _generate_local_text(
    *,
    model_path: Path,
    prompt: str,
    max_new_tokens: int,
    adapter_path: Path | None = None,
    dtype: str = "auto",
    device: str = "auto",
) -> str:
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ModuleNotFoundError as exc:
        raise RuntimeError("local transformers inference requires the optional transformers package") from exc
    model_kwargs = _model_load_kwargs(dtype=dtype)
    # Bandit cannot infer that model_path is a verified local cache and downloads are disabled.
    tokenizer = AutoTokenizer.from_pretrained(  # nosec B615
        str(model_path),
        local_files_only=True,
        trust_remote_code=False,
    )
    model: Any = AutoModelForCausalLM.from_pretrained(  # nosec B615
        str(model_path),
        **model_kwargs,
    )
    if adapter_path is not None:
        try:
            from peft import PeftModel
        except ModuleNotFoundError as exc:
            raise RuntimeError("local adapter smoke requires the optional peft package") from exc
        model = PeftModel.from_pretrained(model, str(adapter_path), local_files_only=True)
    if device and device != "auto" and hasattr(model, "to"):
        model = model.to(device)
    generation_prompt = _generation_prompt(tokenizer, prompt)
    inputs = tokenizer(generation_prompt, return_tensors="pt")
    output_ids = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    prompt_len = int(inputs["input_ids"].shape[-1])
    return str(tokenizer.decode(output_ids[0][prompt_len:], skip_special_tokens=True))


def _generation_prompt(tokenizer: Any, prompt: str) -> str:
    apply_chat_template = getattr(tokenizer, "apply_chat_template", None)
    if not callable(apply_chat_template):
        return prompt
    try:
        return str(
            apply_chat_template(
                [{"role": "user", "content": prompt}],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        )
    except TypeError:
        try:
            return str(
                apply_chat_template(
                    [{"role": "user", "content": prompt}],
                    tokenize=False,
                    add_generation_prompt=True,
                )
            )
        except (TypeError, ValueError):
            return prompt
    except (TypeError, ValueError):
        return prompt


def _run_transformers_lora_sft(
    *,
    model_path: Path,
    training_jsonl: Path,
    adapter_dir: Path,
    training_config: Mapping[str, object],
) -> dict[str, object]:
    try:
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments
        from trl import SFTTrainer
    except ModuleNotFoundError as exc:
        raise RuntimeError("local LoRA SFT requires transformers, datasets, peft, and trl") from exc

    adapter_dir.mkdir(parents=True, exist_ok=True)
    model_kwargs = _model_load_kwargs(dtype=str(training_config["dtype"]))
    # Bandit cannot infer that model_path is a verified local cache and downloads are disabled.
    tokenizer = AutoTokenizer.from_pretrained(  # nosec B615
        str(model_path),
        local_files_only=True,
        trust_remote_code=False,
    )
    model: Any = AutoModelForCausalLM.from_pretrained(  # nosec B615
        str(model_path),
        **model_kwargs,
    )
    device = str(training_config["device"])
    if device and device != "auto" and hasattr(model, "to"):
        model = model.to(device)
    dataset = Dataset.from_list(_read_jsonl(training_jsonl))
    batch_size = _positive_int(training_config.get("batch_size"), default=DEFAULT_LOCAL_SFT_BATCH_SIZE)
    gradient_accumulation_steps = _positive_int(
        training_config.get("gradient_accumulation_steps"),
        default=DEFAULT_LOCAL_SFT_GRADIENT_ACCUMULATION_STEPS,
    )
    epochs = _positive_float(training_config.get("epochs"), default=DEFAULT_LOCAL_SFT_EPOCHS)
    learning_rate = _positive_float(
        training_config.get("learning_rate"),
        default=DEFAULT_LOCAL_SFT_LEARNING_RATE,
    )
    max_steps = _int_value(training_config.get("max_steps"), default=DEFAULT_LOCAL_SFT_MAX_STEPS)
    lora_rank = _positive_int(training_config.get("lora_rank"), default=DEFAULT_LOCAL_SFT_LORA_RANK)
    lora_alpha = _positive_int(training_config.get("lora_alpha"), default=DEFAULT_LOCAL_SFT_LORA_ALPHA)
    lora_dropout = _non_negative_float(
        training_config.get("lora_dropout"),
        default=DEFAULT_LOCAL_SFT_LORA_DROPOUT,
    )
    args = TrainingArguments(
        output_dir=str(adapter_dir),
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        num_train_epochs=epochs,
        learning_rate=learning_rate,
        max_steps=max_steps,
        logging_steps=1,
        save_strategy="epoch",
        report_to=[],
    )
    lora = LoraConfig(
        r=lora_rank,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        task_type="CAUSAL_LM",
    )
    sft_trainer_cls: Any = SFTTrainer
    try:
        trainer = sft_trainer_cls(
            model=model,
            args=args,
            train_dataset=dataset,
            peft_config=lora,
            processing_class=tokenizer,
        )
    except TypeError:
        trainer = sft_trainer_cls(
            model=model,
            args=args,
            train_dataset=dataset,
            peft_config=lora,
            tokenizer=tokenizer,
        )
    result = trainer.train()
    trainer.save_model(str(adapter_dir))
    metrics = dict(getattr(result, "metrics", {}) or {})
    return {str(key): value for key, value in metrics.items()}


def _local_sft_payload(
    *,
    state: str,
    role: str,
    base_model_id: str,
    training_jsonl: Path,
    adapter_dir: Path,
    dataset_hash: str,
    adapter_hash: str | None,
    metrics: Mapping[str, object],
    training_config: Mapping[str, object],
    blockers: list[str],
    registry: str | Path,
    cache_root: str | Path,
    generated_at: str | None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "sft_state": state,
        "status": state,
        "role_id": role,
        "base_model_id": base_model_id,
        "training_jsonl": str(training_jsonl),
        "adapter_path": str(adapter_dir),
        "dataset_hash": dataset_hash,
        "adapter_hash": adapter_hash,
        "registry": str(Path(registry)),
        "cache_root": str(Path(cache_root)),
        "metrics": dict(metrics),
        "training_config": dict(training_config),
        "blockers": blockers,
        "local_files_only": True,
        "network_allowed": False,
        "authority": _authority(),
        "safety": _safety(),
    }


def _parse_json_object(raw_text: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        payload = None
        for index, char in enumerate(raw_text):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(raw_text[index:])
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise
    if not isinstance(payload, dict):
        raise ValueError("local LLM smoke response must be a JSON object")
    return payload


def _raw_text_preview(raw_text: str) -> str:
    redacted = redact_secrets(raw_text, env={})
    if len(redacted) <= RAW_TEXT_PREVIEW_LIMIT:
        return redacted
    return redacted[:RAW_TEXT_PREVIEW_LIMIT] + "...[truncated]"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return ""
    if path.is_file():
        return _sha256_file(path)
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = file_path.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(file_path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain one JSON object per line")
        rows.append(payload)
    return rows


def _stable_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _slug_model_id(model_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", model_id).strip("-").lower() or "model"


def _render_local_eval_markdown(payload: Mapping[str, object]) -> str:
    local_model = _mapping(payload.get("local_model"))
    return (
        "# Local LLM Eval Suite\n\n"
        f"State: **{payload.get('eval_state')}**\n\n"
        f"Role: `{payload.get('role_id')}`\n\n"
        f"Base model: `{local_model.get('base_model_id') or ''}`\n"
    )


def _render_adapter_report_markdown(payload: Mapping[str, object]) -> str:
    return (
        "# Local LLM Adapter Report\n\n"
        f"State: **{payload.get('adapter_state')}**\n\n"
        f"Role: `{payload.get('role_id')}`\n\n"
        f"Base model: `{payload.get('base_model_id') or ''}`\n"
    )


def _render_local_alias_markdown(payload: Mapping[str, object]) -> str:
    return (
        "# Local LLM Alias\n\n"
        f"State: **{payload.get('alias_state')}**\n\n"
        f"Role: `{payload.get('role_id')}`\n\n"
        f"Base model: `{payload.get('base_model_id') or ''}`\n"
    )


def _authority(*, human_review_required: bool = True) -> dict[str, object]:
    return {
        "llm_authority": "none",
        "human_review_required": human_review_required,
        "orders_submitted": False,
        "risk_changed": False,
        "live_approval_authority": False,
    }


def _safety() -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
