"""Optional MLflow Model Registry mirror for paper-candidate registry runs."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from trading_ai.evaluation import mlflow_adapter
from trading_ai.models.baseline import LogisticBaselineModel

MODEL_ARTIFACT_NAME = "model"
SUPPORTED_MODEL_TYPE = "logistic-baseline"
ELIGIBLE_TAG = "trading_ai.eligible_for_paper_challenger"


class MlflowModelRegistryOperationalError(RuntimeError):
    """Raised for MLflow Model Registry failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class MlflowModelRegistrationResult:
    registry_dir: Path
    tracking_uri: str
    experiment_name: str
    registered_model_name: str
    alias: str
    registry_run_id: str
    mlflow_run_id: str
    model_version: str
    created: bool


class _LogisticBaselinePyfuncModel:
    def __init__(self, *, model: Mapping[str, object], feature_names: tuple[str, ...]) -> None:
        self.model = LogisticBaselineModel.from_dict(model)
        self.feature_names = feature_names

    def predict(self, context: object, model_input: object) -> list[dict[str, object]]:
        del context
        rows = _feature_rows(model_input, self.feature_names)
        predictions: list[dict[str, object]] = []
        for features in rows:
            probability = self.model.predict_probability(features)
            predictions.append(
                {
                    "probability": probability,
                    "prediction": int(probability >= 0.5),
                }
            )
        return predictions


def register_registry_mlflow_model(
    *,
    run_id: str,
    registry_dir: str | Path = "reports/registry",
    tracking_uri: str | Path = "reports/mlruns",
    experiment_name: str = "approved-data-evaluations",
    registered_model_name: str = "approved-data-logistic-baseline",
    alias: str = "paper-candidate",
) -> MlflowModelRegistrationResult:
    """Register one approved local registry run as an MLflow pyfunc ModelVersion."""

    try:
        return _register_registry_mlflow_model(
            run_id=run_id,
            registry_dir=registry_dir,
            tracking_uri=tracking_uri,
            experiment_name=experiment_name,
            registered_model_name=registered_model_name,
            alias=alias,
        )
    except MlflowModelRegistryOperationalError:
        raise
    except mlflow_adapter.MlflowRegistrySyncOperationalError as exc:
        raise MlflowModelRegistryOperationalError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - depends on MLflow internals
        raise MlflowModelRegistryOperationalError(f"MLflow model registration failed: {exc}") from exc


def _register_registry_mlflow_model(
    *,
    run_id: str,
    registry_dir: str | Path,
    tracking_uri: str | Path,
    experiment_name: str,
    registered_model_name: str,
    alias: str,
) -> MlflowModelRegistrationResult:
    registry_path = Path(registry_dir)
    tracking_uri_value = str(tracking_uri)
    entries = mlflow_adapter._read_registry_index(registry_path)
    selected_entries = mlflow_adapter._select_entries(entries, run_id=run_id)
    registry_run = mlflow_adapter._load_registry_run(selected_entries[0], registry_dir=registry_path)
    _validate_registry_run_candidate(registry_run.payload, registry_run_id=registry_run.run_id)
    model_run_path = _model_run_artifact_path(registry_run.payload)
    model_run = mlflow_adapter._read_json_object(model_run_path, "model run")
    model_payload, feature_names = _validated_logistic_model_run(model_run, registry_run_id=registry_run.run_id)

    mlflow = mlflow_adapter._import_mlflow()
    client = mlflow_adapter._build_mlflow_client(mlflow, tracking_uri=tracking_uri_value)
    experiment_id = mlflow_adapter._get_or_create_experiment(client, experiment_name)
    mlflow_run = _ensure_tracking_run(
        client,
        experiment_id=experiment_id,
        registry_dir=registry_path,
        tracking_uri=tracking_uri_value,
        experiment_name=experiment_name,
        registry_run_id=registry_run.run_id,
    )
    mlflow_run_id = mlflow_adapter._mlflow_run_id(mlflow_run, registry_run_id=registry_run.run_id)

    _ensure_registered_model(client, registered_model_name)
    version_tags = _model_version_tags(registry_run.payload, registry_dir=registry_path)
    existing_version = _find_model_version_by_registry_run_id(
        client,
        registered_model_name=registered_model_name,
        registry_run_id=registry_run.run_id,
    )
    if existing_version is not None:
        version_number = _model_version_number(existing_version)
        _set_model_version_tags(
            client,
            registered_model_name=registered_model_name,
            version=version_number,
            tags=version_tags,
        )
        _set_model_alias(client, registered_model_name=registered_model_name, alias=alias, version=version_number)
        return MlflowModelRegistrationResult(
            registry_dir=registry_path,
            tracking_uri=tracking_uri_value,
            experiment_name=experiment_name,
            registered_model_name=registered_model_name,
            alias=alias,
            registry_run_id=registry_run.run_id,
            mlflow_run_id=mlflow_run_id,
            model_version=version_number,
            created=False,
        )

    pyfunc_model = _build_mlflow_pyfunc_model(mlflow, model_payload=model_payload, feature_names=feature_names)
    logged_model = _log_pyfunc_model(mlflow, mlflow_run_id=mlflow_run_id, pyfunc_model=pyfunc_model)
    source = _logged_model_source(logged_model, mlflow_run_id=mlflow_run_id)
    created_version = _create_model_version(
        client,
        registered_model_name=registered_model_name,
        source=source,
        mlflow_run_id=mlflow_run_id,
        tags=version_tags,
    )
    version_number = _model_version_number(created_version)
    _set_model_version_tags(
        client,
        registered_model_name=registered_model_name,
        version=version_number,
        tags=version_tags,
    )
    _set_model_alias(client, registered_model_name=registered_model_name, alias=alias, version=version_number)
    return MlflowModelRegistrationResult(
        registry_dir=registry_path,
        tracking_uri=tracking_uri_value,
        experiment_name=experiment_name,
        registered_model_name=registered_model_name,
        alias=alias,
        registry_run_id=registry_run.run_id,
        mlflow_run_id=mlflow_run_id,
        model_version=version_number,
        created=True,
    )


def _validate_registry_run_candidate(payload: Mapping[str, object], *, registry_run_id: str) -> None:
    status = payload.get("status")
    if status != "APPROVED":
        raise MlflowModelRegistryOperationalError(f"registry run is not APPROVED: {registry_run_id} status={status}")
    if payload.get("eligible_for_paper_challenger") is not True:
        raise MlflowModelRegistryOperationalError(
            f"registry run is not eligible for paper challenger: {registry_run_id}"
        )
    artifacts = _mapping(payload.get("artifacts"))
    if "model_run" not in artifacts:
        raise MlflowModelRegistryOperationalError(f"registry run missing model_run artifact: {registry_run_id}")


def _model_run_artifact_path(payload: Mapping[str, object]) -> Path:
    artifacts = _mapping(payload.get("artifacts"))
    artifact = _mapping(artifacts.get("model_run"))
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise MlflowModelRegistryOperationalError("registry run model_run artifact missing path")
    evaluation_dir = _optional_path(payload.get("evaluation_dir"))
    declared = Path(raw_path)
    if declared.is_absolute() or declared.exists() or evaluation_dir is None:
        model_run_path = declared
    else:
        model_run_path = evaluation_dir / declared
    if not model_run_path.exists():
        raise MlflowModelRegistryOperationalError(f"declared artifact not found: {raw_path}")
    return model_run_path


def _validated_logistic_model_run(
    model_run: Mapping[str, object],
    *,
    registry_run_id: str,
) -> tuple[Mapping[str, object], tuple[str, ...]]:
    model_type = model_run.get("model_type")
    if model_type != SUPPORTED_MODEL_TYPE:
        raise MlflowModelRegistryOperationalError(
            f"unsupported model_type for registry run {registry_run_id}: {model_type}"
        )
    raw_model = model_run.get("model")
    if not isinstance(raw_model, Mapping):
        raise MlflowModelRegistryOperationalError(f"model_run missing logistic model payload: {registry_run_id}")
    raw_feature_names = model_run.get("feature_names")
    if not isinstance(raw_feature_names, list) or not raw_feature_names:
        raise MlflowModelRegistryOperationalError(f"model_run missing feature_names: {registry_run_id}")
    feature_names = tuple(str(name) for name in raw_feature_names)
    try:
        model = LogisticBaselineModel.from_dict(raw_model)
    except (KeyError, TypeError, ValueError) as exc:
        raise MlflowModelRegistryOperationalError(
            f"invalid logistic-baseline model payload for registry run {registry_run_id}"
        ) from exc
    if model.feature_names != feature_names:
        raise MlflowModelRegistryOperationalError(
            f"model_run feature_names do not match serialized model for registry run {registry_run_id}"
        )
    return raw_model, feature_names


def _ensure_tracking_run(
    client: object,
    *,
    experiment_id: str,
    registry_dir: Path,
    tracking_uri: str,
    experiment_name: str,
    registry_run_id: str,
) -> object:
    existing = mlflow_adapter._find_existing_run(
        client,
        experiment_id=experiment_id,
        registry_run_id=registry_run_id,
    )
    if existing is not None:
        return existing
    mlflow_adapter.sync_registry_to_mlflow(
        registry_dir=registry_dir,
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
        run_id=registry_run_id,
    )
    synced = mlflow_adapter._find_existing_run(
        client,
        experiment_id=experiment_id,
        registry_run_id=registry_run_id,
    )
    if synced is None:
        raise MlflowModelRegistryOperationalError(f"MLflow tracking run was not found after sync: {registry_run_id}")
    return synced


def _ensure_registered_model(client: object, registered_model_name: str) -> None:
    get_registered_model = getattr(client, "get_registered_model", None)
    if callable(get_registered_model):
        try:
            model = get_registered_model(registered_model_name)
            if model is not None:
                return
        except Exception:
            # Some MLflow backends raise for missing models instead of returning None.
            model = None
    create_registered_model = getattr(client, "create_registered_model", None)
    if not callable(create_registered_model):
        raise MlflowModelRegistryOperationalError("MLflow client cannot create registered models")
    try:
        create_registered_model(registered_model_name)
    except Exception as exc:
        if callable(get_registered_model):
            try:
                get_registered_model(registered_model_name)
                return
            except Exception:
                # Creation may have raced or failed; fall through to the original setup error.
                model = None
        raise MlflowModelRegistryOperationalError(
            f"MLflow registered model setup failed for {registered_model_name}: {exc}"
        ) from exc


def _find_model_version_by_registry_run_id(
    client: object,
    *,
    registered_model_name: str,
    registry_run_id: str,
) -> object | None:
    search_model_versions = getattr(client, "search_model_versions", None)
    if not callable(search_model_versions):
        raise MlflowModelRegistryOperationalError("MLflow client cannot search model versions")
    filter_string = f"name = '{_escape_filter_value(registered_model_name)}'"
    try:
        versions = search_model_versions(filter_string=filter_string)
    except TypeError:
        versions = search_model_versions(filter_string)
    for version in versions:
        tags = _version_tag_mapping(version)
        if tags.get(mlflow_adapter.REGISTRY_RUN_ID_TAG) == registry_run_id:
            return version
    return None


def _build_mlflow_pyfunc_model(
    mlflow: object,
    *,
    model_payload: Mapping[str, object],
    feature_names: tuple[str, ...],
) -> object:
    pyfunc = getattr(mlflow, "pyfunc", None)
    python_model_base = getattr(pyfunc, "PythonModel", object)
    if python_model_base is object:
        return _LogisticBaselinePyfuncModel(model=model_payload, feature_names=feature_names)

    class MlflowLogisticBaselinePyfuncModel(_LogisticBaselinePyfuncModel, python_model_base):  # type: ignore[misc, valid-type]
        pass

    return MlflowLogisticBaselinePyfuncModel(model=model_payload, feature_names=feature_names)


def _log_pyfunc_model(mlflow: object, *, mlflow_run_id: str, pyfunc_model: object) -> object:
    start_run = getattr(mlflow, "start_run", None)
    if callable(start_run):
        with start_run(run_id=mlflow_run_id):
            return _call_pyfunc_log_model(mlflow, pyfunc_model=pyfunc_model)
    return _call_pyfunc_log_model(mlflow, pyfunc_model=pyfunc_model)


def _call_pyfunc_log_model(mlflow: object, *, pyfunc_model: object) -> object:
    pyfunc = getattr(mlflow, "pyfunc", None)
    log_model = getattr(pyfunc, "log_model", None)
    if not callable(log_model):
        raise MlflowModelRegistryOperationalError("MLflow installation does not expose pyfunc.log_model")
    try:
        return log_model(name=MODEL_ARTIFACT_NAME, python_model=pyfunc_model)
    except TypeError as first_exc:
        try:
            return log_model(artifact_path=MODEL_ARTIFACT_NAME, python_model=pyfunc_model)
        except TypeError:
            raise first_exc from None


def _logged_model_source(logged_model: object, *, mlflow_run_id: str) -> str:
    for attribute in ("model_uri", "artifact_uri"):
        value = getattr(logged_model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return f"runs:/{mlflow_run_id}/{MODEL_ARTIFACT_NAME}"


def _create_model_version(
    client: object,
    *,
    registered_model_name: str,
    source: str,
    mlflow_run_id: str,
    tags: Mapping[str, str],
) -> object:
    create_model_version = getattr(client, "create_model_version", None)
    if not callable(create_model_version):
        raise MlflowModelRegistryOperationalError("MLflow client cannot create model versions")
    try:
        return create_model_version(
            name=registered_model_name,
            source=source,
            run_id=mlflow_run_id,
            tags=dict(tags),
        )
    except TypeError:
        return create_model_version(name=registered_model_name, source=source, run_id=mlflow_run_id)


def _set_model_version_tags(
    client: object,
    *,
    registered_model_name: str,
    version: str,
    tags: Mapping[str, str],
) -> None:
    set_model_version_tag = getattr(client, "set_model_version_tag", None)
    if not callable(set_model_version_tag):
        raise MlflowModelRegistryOperationalError("MLflow client cannot tag model versions")
    for key, value in tags.items():
        set_model_version_tag(registered_model_name, version, key, value)


def _set_model_alias(client: object, *, registered_model_name: str, alias: str, version: str) -> None:
    set_registered_model_alias = getattr(client, "set_registered_model_alias", None)
    if not callable(set_registered_model_alias):
        raise MlflowModelRegistryOperationalError("MLflow client cannot set registered model aliases")
    set_registered_model_alias(registered_model_name, alias, version)


def _model_version_tags(payload: Mapping[str, object], *, registry_dir: Path) -> dict[str, str]:
    return {
        mlflow_adapter.REGISTRY_SOURCE_TAG: mlflow_adapter.LOCAL_REGISTRY_SOURCE,
        mlflow_adapter.REGISTRY_RUN_ID_TAG: _required_string(payload, "run_id", "registry run"),
        mlflow_adapter.REGISTRY_DIR_TAG: str(registry_dir),
        mlflow_adapter.STATUS_TAG: _string_value(payload.get("status")),
        mlflow_adapter.DATASET_ID_TAG: _string_value(payload.get("dataset_id")),
        mlflow_adapter.FREQUENCY_TAG: _string_value(payload.get("frequency")),
        mlflow_adapter.AS_OF_DATE_TAG: _string_value(payload.get("as_of_date")),
        ELIGIBLE_TAG: _bool_string(payload.get("eligible_for_paper_challenger")),
    }


def _model_version_number(version: object) -> str:
    value = getattr(version, "version", None)
    if value is None:
        raise MlflowModelRegistryOperationalError("MLflow model version missing version number")
    return str(value)


def _version_tag_mapping(version: object) -> Mapping[str, object]:
    tags = getattr(version, "tags", None)
    return tags if isinstance(tags, Mapping) else {}


def _feature_rows(model_input: object, feature_names: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
    records = _records_from_model_input(model_input)
    feature_rows: list[tuple[float, ...]] = []
    for record in records:
        values: list[float] = []
        for feature_name in feature_names:
            raw_value = record.get(feature_name)
            if raw_value in {None, ""}:
                raise ValueError(f"model input missing required feature: {feature_name}")
            try:
                value = float(str(raw_value))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"model input feature is not numeric: {feature_name}") from exc
            if not math.isfinite(value):
                raise ValueError(f"model input feature is not finite: {feature_name}")
            values.append(value)
        feature_rows.append(tuple(values))
    return tuple(feature_rows)


def _records_from_model_input(model_input: object) -> tuple[Mapping[str, object], ...]:
    to_dict = getattr(model_input, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = None
        if isinstance(records, list):
            return tuple(_require_record(record) for record in records)
    if isinstance(model_input, Mapping):
        if _looks_like_column_mapping(model_input):
            return _records_from_column_mapping(model_input)
        return (model_input,)
    if isinstance(model_input, (list, tuple)):
        return tuple(_require_record(record) for record in model_input)
    raise ValueError("model input must be a DataFrame, mapping, or list of mappings")


def _looks_like_column_mapping(payload: Mapping[str, object]) -> bool:
    if not payload:
        return False
    return all(_is_sequence(value) for value in payload.values())


def _records_from_column_mapping(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    columns = {str(key): _sequence_list(value) for key, value in payload.items() if _is_sequence(value)}
    lengths = {len(value) for value in columns.values()}
    if len(lengths) != 1:
        raise ValueError("model input column lengths must match")
    row_count = next(iter(lengths), 0)
    return tuple({key: values[index] for key, values in columns.items()} for index in range(row_count))


def _require_record(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("model input rows must be mappings")
    return value


def _is_sequence(value: object) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, Mapping))


def _sequence_list(value: object) -> list[object]:
    return list(cast(Iterable[object], value))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


def _required_string(payload: Mapping[str, object], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MlflowModelRegistryOperationalError(f"{label} missing required string field: {field}")
    return value.strip()


def _bool_string(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else _string_value(value)


def _string_value(value: object) -> str:
    return "" if value is None else str(value)


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
