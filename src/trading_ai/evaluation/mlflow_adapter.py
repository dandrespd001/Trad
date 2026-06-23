"""Optional MLflow mirror for the local evaluation registry."""

from __future__ import annotations

import importlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REGISTRY_SOURCE_TAG = "trading_ai.source"
REGISTRY_RUN_ID_TAG = "trading_ai.registry_run_id"
REGISTRY_DIR_TAG = "trading_ai.registry_dir"
EVALUATION_DIR_TAG = "trading_ai.evaluation_dir"
STATUS_TAG = "trading_ai.status"
DATASET_ID_TAG = "trading_ai.dataset_id"
FREQUENCY_TAG = "trading_ai.frequency"
AS_OF_DATE_TAG = "trading_ai.as_of_date"
LOCAL_REGISTRY_SOURCE = "local_registry"
EVALUATION_ARTIFACT_PATH = "evaluation"


class MlflowRegistrySyncOperationalError(RuntimeError):
    """Raised for MLflow sync failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class MlflowRegistrySyncResult:
    registry_dir: Path
    tracking_uri: str
    experiment_name: str
    total_registry_runs: int
    runs_read: int
    created: int
    updated: int
    skipped: int
    run_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RegistryRun:
    run_id: str
    run_path: Path
    payload: Mapping[str, object]
    artifact_paths: tuple[Path, ...]


def sync_registry_to_mlflow(
    *,
    registry_dir: str | Path = "reports/registry",
    tracking_uri: str | Path = "reports/mlruns",
    experiment_name: str = "approved-data-evaluations",
    run_id: str | None = None,
) -> MlflowRegistrySyncResult:
    """Mirror selected local registry runs into an MLflow tracking store."""

    registry_path = Path(registry_dir)
    tracking_uri_value = str(tracking_uri)
    entries = _read_registry_index(registry_path)
    selected_entries = _select_entries(entries, run_id=run_id)
    registry_runs = tuple(_load_registry_run(entry, registry_dir=registry_path) for entry in selected_entries)

    mlflow = _import_mlflow()
    client = _build_mlflow_client(mlflow, tracking_uri=tracking_uri_value)
    experiment_id = _get_or_create_experiment(client, experiment_name)

    created = 0
    updated = 0
    for registry_run in registry_runs:
        try:
            tags = _tags_for_run(registry_run.payload, registry_dir=registry_path)
            existing = _find_existing_run(client, experiment_id=experiment_id, registry_run_id=registry_run.run_id)
            if existing is None:
                mlflow_run = _create_run(
                    client,
                    experiment_id=experiment_id,
                    run_name=registry_run.run_id,
                    tags=tags,
                )
                created += 1
            else:
                mlflow_run = existing
                updated += 1

            mlflow_run_id = _mlflow_run_id(mlflow_run, registry_run_id=registry_run.run_id)
            _set_tags(client, mlflow_run_id=mlflow_run_id, tags=tags)
            _log_params(
                client,
                mlflow_run=mlflow_run,
                mlflow_run_id=mlflow_run_id,
                registry_run_id=registry_run.run_id,
                params=_params_for_run(registry_run.payload),
            )
            _log_metrics(
                client,
                mlflow_run=mlflow_run,
                mlflow_run_id=mlflow_run_id,
                metrics=_numeric_metrics(registry_run.payload),
            )
            _log_artifacts(client, mlflow_run_id=mlflow_run_id, artifact_paths=registry_run.artifact_paths)
        except MlflowRegistrySyncOperationalError:
            raise
        except Exception as exc:  # pragma: no cover - depends on MLflow internals
            raise MlflowRegistrySyncOperationalError(
                f"MLflow sync failed for registry run {registry_run.run_id}: {exc}"
            ) from exc

    return MlflowRegistrySyncResult(
        registry_dir=registry_path,
        tracking_uri=tracking_uri_value,
        experiment_name=experiment_name,
        total_registry_runs=len(entries),
        runs_read=len(registry_runs),
        created=created,
        updated=updated,
        skipped=len(entries) - len(selected_entries),
        run_ids=tuple(registry_run.run_id for registry_run in registry_runs),
    )


def _read_registry_index(registry_dir: Path) -> list[Mapping[str, object]]:
    index_path = registry_dir / "index.json"
    if not index_path.exists():
        raise MlflowRegistrySyncOperationalError(f"registry index not found: {index_path}")
    index_payload = _read_json_object(index_path, "registry index")
    raw_runs = index_payload.get("runs")
    if not isinstance(raw_runs, list):
        raise MlflowRegistrySyncOperationalError("registry index runs must be a list")
    entries: list[Mapping[str, object]] = []
    for entry in raw_runs:
        if not isinstance(entry, Mapping):
            raise MlflowRegistrySyncOperationalError("registry index run entry must be an object")
        entries.append(entry)
    return entries


def _select_entries(entries: list[Mapping[str, object]], *, run_id: str | None) -> list[Mapping[str, object]]:
    if run_id is None:
        return entries
    selected = [entry for entry in entries if entry.get("run_id") == run_id]
    if not selected:
        raise MlflowRegistrySyncOperationalError(f"registry run not found in index: {run_id}")
    return selected


def _load_registry_run(entry: Mapping[str, object], *, registry_dir: Path) -> _RegistryRun:
    run_id = _required_string(entry, "run_id", "registry index run")
    run_path = _resolve_registry_run_path(entry.get("run_path"), registry_dir=registry_dir, run_id=run_id)
    payload = _read_json_object(run_path, "registry run")
    payload_run_id = _required_string(payload, "run_id", "registry run")
    if payload_run_id != run_id:
        raise MlflowRegistrySyncOperationalError(f"registry run id mismatch: {run_path}")
    return _RegistryRun(
        run_id=run_id,
        run_path=run_path,
        payload=payload,
        artifact_paths=_declared_artifact_paths(payload, run_path=run_path),
    )


def _resolve_registry_run_path(raw_path: object, *, registry_dir: Path, run_id: str) -> Path:
    fallback = registry_dir / "runs" / f"{run_id}.json"
    if not isinstance(raw_path, str) or not raw_path.strip():
        return fallback
    declared = Path(raw_path)
    if declared.is_absolute() or declared.exists():
        return declared
    if fallback.exists():
        return fallback
    return declared


def _declared_artifact_paths(payload: Mapping[str, object], *, run_path: Path) -> tuple[Path, ...]:
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise MlflowRegistrySyncOperationalError(f"registry run artifacts must be an object: {run_path}")
    evaluation_dir = _optional_path(payload.get("evaluation_dir"))
    artifact_paths: list[Path] = []
    for name, raw_artifact in raw_artifacts.items():
        if not isinstance(raw_artifact, Mapping):
            raise MlflowRegistrySyncOperationalError(f"registry run artifact must be an object: {name}")
        raw_path = raw_artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise MlflowRegistrySyncOperationalError(f"registry run artifact missing path: {name}")
        artifact_path = _resolve_declared_artifact_path(raw_path, evaluation_dir=evaluation_dir)
        if not artifact_path.exists():
            raise MlflowRegistrySyncOperationalError(f"declared artifact not found: {raw_path}")
        artifact_paths.append(artifact_path)
    return tuple(artifact_paths)


def _resolve_declared_artifact_path(raw_path: str, *, evaluation_dir: Path | None) -> Path:
    declared = Path(raw_path)
    if declared.is_absolute() or declared.exists() or evaluation_dir is None:
        return declared
    return evaluation_dir / declared


def _import_mlflow() -> object:
    try:
        return importlib.import_module("mlflow")
    except ImportError as exc:
        raise MlflowRegistrySyncOperationalError(
            "MLflow is not installed; install the monitoring extra with `pip install -e .[monitoring]`."
        ) from exc


def _build_mlflow_client(mlflow: object, *, tracking_uri: str) -> object:
    tracking = getattr(mlflow, "tracking", None)
    client_class = getattr(tracking, "MlflowClient", None)
    if client_class is None:
        raise MlflowRegistrySyncOperationalError("MLflow installation does not expose MlflowClient")
    try:
        return client_class(tracking_uri=tracking_uri)
    except TypeError:
        set_tracking_uri = getattr(mlflow, "set_tracking_uri", None)
        if callable(set_tracking_uri):
            set_tracking_uri(tracking_uri)
        return client_class()
    except Exception as exc:  # pragma: no cover - depends on MLflow internals
        raise MlflowRegistrySyncOperationalError(f"MLflow client initialization failed: {exc}") from exc


def _get_or_create_experiment(client: object, experiment_name: str) -> str:
    try:
        experiment = client.get_experiment_by_name(experiment_name)
        if experiment is not None:
            return str(experiment.experiment_id)
        return str(client.create_experiment(experiment_name))
    except Exception as exc:  # pragma: no cover - depends on MLflow internals
        raise MlflowRegistrySyncOperationalError(f"MLflow experiment setup failed: {exc}") from exc


def _find_existing_run(client: object, *, experiment_id: str, registry_run_id: str) -> object | None:
    filter_string = f"tags.`{REGISTRY_RUN_ID_TAG}` = '{_escape_filter_value(registry_run_id)}'"
    runs = client.search_runs(
        experiment_ids=[experiment_id],
        filter_string=filter_string,
        max_results=1,
    )
    return runs[0] if runs else None


def _create_run(
    client: object,
    *,
    experiment_id: str,
    run_name: str,
    tags: Mapping[str, str],
) -> object:
    try:
        return client.create_run(experiment_id=experiment_id, tags=dict(tags), run_name=run_name)
    except TypeError:
        fallback_tags = dict(tags)
        fallback_tags["mlflow.runName"] = run_name
        return client.create_run(experiment_id=experiment_id, tags=fallback_tags)


def _tags_for_run(payload: Mapping[str, object], *, registry_dir: Path) -> dict[str, str]:
    return {
        REGISTRY_SOURCE_TAG: LOCAL_REGISTRY_SOURCE,
        REGISTRY_RUN_ID_TAG: _required_string(payload, "run_id", "registry run"),
        REGISTRY_DIR_TAG: str(registry_dir),
        EVALUATION_DIR_TAG: _string_value(payload.get("evaluation_dir")),
        STATUS_TAG: _string_value(payload.get("status")),
        DATASET_ID_TAG: _string_value(payload.get("dataset_id")),
        FREQUENCY_TAG: _string_value(payload.get("frequency")),
        AS_OF_DATE_TAG: _string_value(payload.get("as_of_date")),
    }


def _params_for_run(payload: Mapping[str, object]) -> dict[str, str]:
    temporal_range = _mapping(payload.get("temporal_range"))
    params = {
        "dataset_id": payload.get("dataset_id"),
        "frequency": payload.get("frequency"),
        "as_of_date": payload.get("as_of_date"),
        "dataset_hash": payload.get("dataset_hash"),
        "source_sha256": payload.get("source_sha256"),
        "start": temporal_range.get("start"),
        "end": temporal_range.get("end"),
        "row_count": payload.get("row_count"),
        "symbols": payload.get("symbols"),
        "status": payload.get("status"),
        "eligible_for_paper_challenger": payload.get("eligible_for_paper_challenger"),
    }
    return {key: _param_value(value) for key, value in params.items() if value is not None}


def _numeric_metrics(payload: Mapping[str, object]) -> dict[str, float]:
    metrics = _mapping(payload.get("metrics"))
    numeric: dict[str, float] = {}
    for key, value in metrics.items():
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numeric[str(key)] = number
    return numeric


def _set_tags(client: object, *, mlflow_run_id: str, tags: Mapping[str, str]) -> None:
    for key, value in tags.items():
        client.set_tag(mlflow_run_id, key, value)


def _log_params(
    client: object,
    *,
    mlflow_run: object,
    mlflow_run_id: str,
    registry_run_id: str,
    params: Mapping[str, str],
) -> None:
    existing_params = _run_data_mapping(mlflow_run, "params")
    for key, value in params.items():
        existing_value = existing_params.get(key)
        if existing_value is None:
            client.log_param(mlflow_run_id, key, value)
        elif str(existing_value) != value:
            raise MlflowRegistrySyncOperationalError(
                "MLflow param conflict for registry run "
                f"{registry_run_id}: {key} existing={existing_value} registry={value}"
            )


def _log_metrics(
    client: object,
    *,
    mlflow_run: object,
    mlflow_run_id: str,
    metrics: Mapping[str, float],
) -> None:
    existing_metrics = _run_data_mapping(mlflow_run, "metrics")
    for key, value in metrics.items():
        existing_value = existing_metrics.get(key)
        if existing_value is None or float(existing_value) != value:
            client.log_metric(mlflow_run_id, key, value)


def _log_artifacts(client: object, *, mlflow_run_id: str, artifact_paths: tuple[Path, ...]) -> None:
    for artifact_path in artifact_paths:
        if artifact_path.is_dir():
            log_artifacts = getattr(client, "log_artifacts", None)
            if not callable(log_artifacts):
                raise MlflowRegistrySyncOperationalError(
                    f"MLflow client cannot log artifact directory: {artifact_path}"
                )
            log_artifacts(mlflow_run_id, str(artifact_path), artifact_path=EVALUATION_ARTIFACT_PATH)
        else:
            client.log_artifact(mlflow_run_id, str(artifact_path), artifact_path=EVALUATION_ARTIFACT_PATH)


def _mlflow_run_id(run: object, *, registry_run_id: str) -> str:
    info = getattr(run, "info", None)
    run_id = getattr(info, "run_id", None)
    if not isinstance(run_id, str) or not run_id:
        raise MlflowRegistrySyncOperationalError(f"MLflow run missing run_id for registry run {registry_run_id}")
    return run_id


def _run_data_mapping(run: object, field: str) -> Mapping[str, object]:
    data = getattr(run, "data", None)
    value = getattr(data, field, None)
    return value if isinstance(value, Mapping) else {}


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MlflowRegistrySyncOperationalError(f"invalid {label} JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise MlflowRegistrySyncOperationalError(f"{label} JSON must be an object: {path}")
    return payload


def _required_string(payload: Mapping[str, object], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MlflowRegistrySyncOperationalError(f"{label} missing required string field: {field}")
    return value.strip()


def _optional_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _param_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _string_value(value: object) -> str:
    return "" if value is None else str(value)


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
