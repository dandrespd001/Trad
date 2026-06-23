"""Local reproducible registry for approved-data evaluation packages."""

from __future__ import annotations

import hashlib
import json
import os
import string
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
APPROVED_STATUS = "APPROVED"
REJECTED_STATUS = "REJECTED"
BLOCKED_STATUS = "BLOCKED"
REGISTRY_STATUSES = (APPROVED_STATUS, REJECTED_STATUS, BLOCKED_STATUS)
REQUIRED_DATASET_FIELDS = (
    "dataset_id",
    "frequency",
    "dataset_hash",
    "source_sha256",
    "start",
    "end",
    "symbols",
    "row_count",
)


class EvaluationRegistryOperationalError(RuntimeError):
    """Raised for registry failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class EvaluationRegistrationResult:
    run_id: str
    status: str
    registry_dir: Path
    run_path: Path
    index_path: Path
    markdown_path: Path


def register_evaluation(
    *,
    evaluation_dir: str | Path,
    registry_dir: str | Path = "reports/registry",
) -> EvaluationRegistrationResult:
    """Register an existing approved-data evaluation package in a local registry."""

    evaluation_path = Path(evaluation_dir)
    registry_path = Path(registry_dir)
    summary_path = evaluation_path / "evaluation_summary.json"
    data_quality_path = evaluation_path / "data_quality.json"

    if not evaluation_path.exists() or not evaluation_path.is_dir():
        raise EvaluationRegistryOperationalError(f"evaluation directory not found: {evaluation_path}")
    if not summary_path.exists():
        raise EvaluationRegistryOperationalError(f"evaluation package missing evaluation_summary.json: {summary_path}")
    if not data_quality_path.exists():
        raise EvaluationRegistryOperationalError(f"evaluation package missing data_quality.json: {data_quality_path}")

    summary = _read_json_object(summary_path, "evaluation summary")
    _read_json_object(data_quality_path, "data quality")
    run_id, run_payload = _build_run_payload(
        summary=summary,
        summary_path=summary_path,
        data_quality_path=data_quality_path,
        evaluation_dir=evaluation_path,
        registry_dir=registry_path,
    )

    runs_dir = registry_path / "runs"
    run_path = runs_dir / f"{run_id}.json"
    registered_at = _existing_registered_at(run_path, run_id=run_id) or _utc_now()
    run_payload["registered_at"] = registered_at

    index_path = registry_path / "index.json"
    markdown_path = registry_path / "index.md"
    index_payload = _build_index(
        registry_dir=registry_path,
        index_path=index_path,
        run_path=run_path,
        run_payload=run_payload,
    )
    markdown = render_registry_markdown(index_payload)

    _write_json_atomic(run_payload, run_path)
    _write_json_atomic(index_payload, index_path)
    _write_text_atomic(markdown, markdown_path)

    return EvaluationRegistrationResult(
        run_id=run_id,
        status=str(run_payload["status"]),
        registry_dir=registry_path,
        run_path=run_path,
        index_path=index_path,
        markdown_path=markdown_path,
    )


def render_registry_markdown(index_payload: Mapping[str, object]) -> str:
    runs = index_payload.get("runs", [])
    lines = [
        "# Evaluation Registry",
        "",
        "| Run ID | Dataset | Frequency | As Of Date | Status | Eligible | "
        "Accuracy | Sharpe | CAGR | Max Drawdown | Summary |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    if isinstance(runs, list):
        for entry in runs:
            run = _mapping(entry)
            metrics = _mapping(run.get("metrics"))
            lines.append(
                "| "
                + " | ".join(
                    (
                        _escape_markdown_cell(run.get("run_id")),
                        _escape_markdown_cell(run.get("dataset_id")),
                        _escape_markdown_cell(run.get("frequency")),
                        _escape_markdown_cell(run.get("as_of_date")),
                        _escape_markdown_cell(run.get("status")),
                        _escape_markdown_cell(_bool_string(run.get("eligible_for_paper_challenger"))),
                        _escape_markdown_cell(_format_metric(metrics.get("accuracy"))),
                        _escape_markdown_cell(_format_metric(metrics.get("sharpe"))),
                        _escape_markdown_cell(_format_metric(metrics.get("cagr"))),
                        _escape_markdown_cell(_format_metric(metrics.get("max_drawdown"))),
                        _escape_markdown_cell(run.get("evaluation_summary_path")),
                    )
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def _build_run_payload(
    *,
    summary: Mapping[str, object],
    summary_path: Path,
    data_quality_path: Path,
    evaluation_dir: Path,
    registry_dir: Path,
) -> tuple[str, dict[str, object]]:
    status = _required_string(summary, "status")
    if status not in REGISTRY_STATUSES:
        raise EvaluationRegistryOperationalError(f"unsupported evaluation status: {status}")

    eligible = summary.get("eligible_for_paper_challenger")
    if not isinstance(eligible, bool):
        raise EvaluationRegistryOperationalError("evaluation summary missing boolean eligible_for_paper_challenger")

    reasons = _string_list(summary.get("reasons", []), "reasons")
    dataset = _required_mapping(summary, "approved_dataset")
    _validate_required_dataset_fields(dataset)
    dataset_id = _safe_component(_required_string(dataset, "dataset_id"), "dataset_id")
    frequency = _safe_component(_required_string(dataset, "frequency"), "frequency")
    as_of_date = _safe_component(evaluation_dir.name, "as_of_date")
    dataset_hash = _required_sha256(dataset, "dataset_hash")
    source_sha256 = _required_sha256(dataset, "source_sha256")
    symbols = _required_symbols(dataset)
    row_count = _required_int(dataset, "row_count")
    metrics = dict(_required_mapping(summary, "metrics"))
    artifacts = _required_mapping(summary, "artifacts")
    if "data_quality" not in artifacts:
        raise EvaluationRegistryOperationalError("evaluation summary artifacts missing data_quality")
    verified_artifacts = _verified_artifacts(artifacts, evaluation_dir=evaluation_dir)
    data_quality_artifact = _mapping(verified_artifacts.get("data_quality"))
    if Path(str(data_quality_artifact.get("path", ""))).resolve() != data_quality_path.resolve():
        raise EvaluationRegistryOperationalError("data_quality artifact path does not match package data_quality.json")

    summary_hash = _file_sha256(summary_path)
    run_id = _build_run_id(
        dataset_id=dataset_id,
        frequency=frequency,
        as_of_date=as_of_date,
        dataset_hash=dataset_hash,
        summary_hash=summary_hash,
    )
    run_payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "registered_at": "",
        "evaluation_dir": str(evaluation_dir),
        "evaluation_summary_path": str(summary_path),
        "status": status,
        "eligible_for_paper_challenger": eligible,
        "reasons": reasons,
        "dataset_id": dataset_id,
        "frequency": frequency,
        "as_of_date": as_of_date,
        "dataset_hash": dataset_hash,
        "source_sha256": source_sha256,
        "temporal_range": {
            "start": dataset.get("start"),
            "end": dataset.get("end"),
        },
        "symbols": symbols,
        "row_count": row_count,
        "metrics": metrics,
        "artifacts": {
            "evaluation_summary": {
                "path": str(summary_path),
                "sha256": summary_hash,
            },
            **verified_artifacts,
        },
        "registry_dir": str(registry_dir),
    }
    return run_id, run_payload


def _build_run_id(
    *,
    dataset_id: str,
    frequency: str,
    as_of_date: str,
    dataset_hash: str,
    summary_hash: str,
) -> str:
    return f"approved-{dataset_id}-{frequency}-{as_of_date}-{dataset_hash[:12]}-{summary_hash[:12]}"


def _build_index(
    *,
    registry_dir: Path,
    index_path: Path,
    run_path: Path,
    run_payload: Mapping[str, object],
) -> dict[str, object]:
    existing_entries: list[Mapping[str, object]] = []
    if index_path.exists():
        index_payload = _read_json_object(index_path, "registry index")
        raw_runs = index_payload.get("runs", [])
        if not isinstance(raw_runs, list):
            raise EvaluationRegistryOperationalError("registry index runs must be a list")
        existing_entries = [_mapping(entry) for entry in raw_runs]

    entries_by_id: dict[str, dict[str, object]] = {}
    for entry in existing_entries:
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and run_id:
            entries_by_id[run_id] = dict(entry)

    new_entry = _index_entry(run_payload, run_path=run_path)
    entries_by_id[str(new_entry["run_id"])] = new_entry
    ordered_entries = sorted(
        entries_by_id.values(),
        key=lambda entry: (
            str(entry.get("registered_at", "")),
            str(entry.get("dataset_id", "")),
            str(entry.get("frequency", "")),
            str(entry.get("as_of_date", "")),
            str(entry.get("run_id", "")),
        ),
    )
    counts = {
        status: sum(1 for entry in ordered_entries if entry.get("status") == status) for status in REGISTRY_STATUSES
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_dir": str(registry_dir),
        "generated_at": _utc_now(),
        "counts": counts,
        "runs": ordered_entries,
    }


def _index_entry(run_payload: Mapping[str, object], *, run_path: Path) -> dict[str, object]:
    metrics = _mapping(run_payload.get("metrics"))
    temporal_range = _mapping(run_payload.get("temporal_range"))
    return {
        "run_id": run_payload.get("run_id"),
        "registered_at": run_payload.get("registered_at"),
        "dataset_id": run_payload.get("dataset_id"),
        "frequency": run_payload.get("frequency"),
        "as_of_date": run_payload.get("as_of_date"),
        "status": run_payload.get("status"),
        "eligible_for_paper_challenger": run_payload.get("eligible_for_paper_challenger"),
        "dataset_hash": run_payload.get("dataset_hash"),
        "source_sha256": run_payload.get("source_sha256"),
        "start": temporal_range.get("start"),
        "end": temporal_range.get("end"),
        "row_count": run_payload.get("row_count"),
        "symbols": run_payload.get("symbols", []),
        "metrics": {
            "accuracy": metrics.get("accuracy"),
            "sharpe": metrics.get("sharpe"),
            "cagr": metrics.get("cagr"),
            "max_drawdown": metrics.get("max_drawdown"),
        },
        "evaluation_dir": run_payload.get("evaluation_dir"),
        "evaluation_summary_path": run_payload.get("evaluation_summary_path"),
        "run_path": str(run_path),
    }


def _verified_artifacts(artifacts: Mapping[str, object], *, evaluation_dir: Path) -> dict[str, dict[str, object]]:
    verified: dict[str, dict[str, object]] = {}
    for name, raw_artifact in artifacts.items():
        artifact_name = _safe_component(str(name), "artifact name")
        artifact = _required_mapping({"artifact": raw_artifact}, "artifact")
        raw_path = _required_string(artifact, "path")
        expected_hash = _required_sha256(artifact, "sha256")
        artifact_path = _resolve_artifact_path(raw_path, evaluation_dir=evaluation_dir)
        actual_hash = _file_sha256(artifact_path)
        if actual_hash != expected_hash:
            raise EvaluationRegistryOperationalError(
                f"artifact hash mismatch for {artifact_name}: expected={expected_hash} actual={actual_hash}"
            )
        verified[artifact_name] = {
            "path": str(artifact_path),
            "sha256": actual_hash,
        }
    return verified


def _resolve_artifact_path(raw_path: str, *, evaluation_dir: Path) -> Path:
    declared = Path(raw_path)
    candidates = [declared] if declared.is_absolute() else [declared, evaluation_dir / declared]
    existing_outside: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if _is_relative_to(resolved, evaluation_dir.resolve()):
            return candidate
        existing_outside.append(candidate)
    if existing_outside:
        raise EvaluationRegistryOperationalError(f"artifact path is outside evaluation directory: {raw_path}")
    raise EvaluationRegistryOperationalError(f"declared artifact not found: {raw_path}")


def _existing_registered_at(run_path: Path, *, run_id: str) -> str | None:
    if not run_path.exists():
        return None
    payload = _read_json_object(run_path, "registry run")
    if payload.get("run_id") != run_id:
        raise EvaluationRegistryOperationalError(f"registry run id mismatch: {run_path}")
    registered_at = payload.get("registered_at")
    if not isinstance(registered_at, str) or not registered_at:
        raise EvaluationRegistryOperationalError(f"registry run missing registered_at: {run_path}")
    return registered_at


def _validate_required_dataset_fields(dataset: Mapping[str, object]) -> None:
    missing = [field for field in REQUIRED_DATASET_FIELDS if field not in dataset]
    if missing:
        raise EvaluationRegistryOperationalError(
            "evaluation summary approved_dataset missing required field(s): " + ", ".join(missing)
        )


def _required_mapping(payload: Mapping[str, object], field: str) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be an object: {field}")
    return value


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationRegistryOperationalError(f"evaluation summary missing required string field: {field}")
    return value.strip()


def _required_sha256(payload: Mapping[str, object], field: str) -> str:
    value = _required_string(payload, field)
    if len(value) != 64 or any(character not in string.hexdigits for character in value):
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be a SHA-256 hex digest: {field}")
    return value.lower()


def _required_int(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool):
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be an integer: {field}")
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be an integer: {field}") from exc
    if resolved < 0:
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be non-negative: {field}")
    return resolved


def _required_symbols(dataset: Mapping[str, object]) -> list[str]:
    value = dataset.get("symbols")
    if not isinstance(value, list) or not value:
        raise EvaluationRegistryOperationalError("evaluation summary approved_dataset symbols must be a non-empty list")
    return [str(symbol).upper() for symbol in value]


def _safe_component(value: str, field: str) -> str:
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise EvaluationRegistryOperationalError(f"unsafe registry component in {field}: {value}")
    return value


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationRegistryOperationalError(f"evaluation summary field must be a list: {field}")
    return [str(item) for item in value]


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationRegistryOperationalError(f"invalid {label} JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationRegistryOperationalError(f"{label} JSON must be an object: {path}")
    return payload


def _write_json_atomic(payload: Mapping[str, object], path: Path) -> None:
    _write_text_atomic(json.dumps(payload, indent=2, sort_keys=True), path)


def _write_text_atomic(payload: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _format_metric(value: object) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.6f}"


def _bool_string(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return ""


def _escape_markdown_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
