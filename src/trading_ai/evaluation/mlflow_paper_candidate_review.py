"""Offline review for the MLflow paper-candidate model alias."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from trading_ai.config import ConfigError, load_universe_config
from trading_ai.data.io import ParquetDependencyError, read_records
from trading_ai.evaluation import mlflow_adapter, mlflow_model_registry
from trading_ai.models.signals import latest_valid_feature_rows

SCHEMA_VERSION = 1
PASSED_STATUS = "PASSED"
FAILED_STATUS = "FAILED"


class MlflowPaperCandidateOperationalError(RuntimeError):
    """Raised for review failures that should return CLI exit code 2."""


class MlflowPaperCandidateValidationError(RuntimeError):
    """Raised when the alias resolves but the candidate fails review."""

    def __init__(self, message: str, *, result: MlflowPaperCandidateReviewResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class MlflowPaperCandidateReviewResult:
    output_path: Path
    markdown_path: Path
    report: Mapping[str, object]

    @property
    def passed(self) -> bool:
        return self.report.get("status") == PASSED_STATUS


def review_mlflow_paper_candidate(
    *,
    registry_dir: str | Path = "reports/registry",
    tracking_uri: str | Path = "reports/mlruns",
    registered_model_name: str = "approved-data-logistic-baseline",
    alias: str = "paper-candidate",
    features: str | Path = "data/processed/features.csv",
    config: str | Path = "configs/universe.yml",
    output: str | Path = "reports/tmp/mlflow_paper_candidate_review/latest.json",
    markdown_output: str | Path = "reports/tmp/mlflow_paper_candidate_review/latest.md",
) -> MlflowPaperCandidateReviewResult:
    """Validate that the MLflow alias points to a runnable local-registry paper candidate."""

    try:
        return _review_mlflow_paper_candidate(
            registry_dir=registry_dir,
            tracking_uri=tracking_uri,
            registered_model_name=registered_model_name,
            alias=alias,
            features=features,
            config=config,
            output=output,
            markdown_output=markdown_output,
        )
    except MlflowPaperCandidateValidationError:
        raise
    except MlflowPaperCandidateOperationalError:
        raise
    except mlflow_adapter.MlflowRegistrySyncOperationalError as exc:
        raise MlflowPaperCandidateOperationalError(str(exc)) from exc
    except mlflow_model_registry.MlflowModelRegistryOperationalError as exc:
        raise MlflowPaperCandidateOperationalError(str(exc)) from exc
    except (ConfigError, ParquetDependencyError, OSError, ValueError) as exc:
        raise MlflowPaperCandidateOperationalError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - depends on MLflow internals
        raise MlflowPaperCandidateOperationalError(f"MLflow paper-candidate review failed: {exc}") from exc


def _review_mlflow_paper_candidate(
    *,
    registry_dir: str | Path,
    tracking_uri: str | Path,
    registered_model_name: str,
    alias: str,
    features: str | Path,
    config: str | Path,
    output: str | Path,
    markdown_output: str | Path,
) -> MlflowPaperCandidateReviewResult:
    registry_path = Path(registry_dir)
    tracking_uri_value = str(tracking_uri)
    feature_source = Path(features)
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    failures: list[str] = []
    warnings: list[str] = []

    report = _base_report(
        registered_model_name=registered_model_name,
        alias=alias,
        feature_source=feature_source,
        failures=failures,
        warnings=warnings,
    )

    mlflow = mlflow_adapter._import_mlflow()
    client = mlflow_adapter._build_mlflow_client(mlflow, tracking_uri=tracking_uri_value)
    version = _get_model_version_by_alias(
        client,
        registered_model_name=registered_model_name,
        alias=alias,
    )
    version_number = _model_version_number(version)
    report["model_version"] = version_number
    version_tags = _version_tag_mapping(version)

    registry_run_id = _tag_value(version_tags, mlflow_adapter.REGISTRY_RUN_ID_TAG)
    if not registry_run_id:
        failures.append(f"model version missing tag {mlflow_adapter.REGISTRY_RUN_ID_TAG}")
        return _write_failed_review(report, output_path=output_path, markdown_path=markdown_path)

    report["registry_run_id"] = registry_run_id
    _validate_version_tags_before_local_lookup(version_tags, failures)

    entries = mlflow_adapter._read_registry_index(registry_path)
    registry_run = mlflow_adapter._load_registry_run(
        mlflow_adapter._select_entries(entries, run_id=registry_run_id)[0],
        registry_dir=registry_path,
    )
    local_payload = registry_run.payload
    report["local_registry_status"] = _string_value(local_payload.get("status"))
    report["eligible_for_paper_challenger"] = local_payload.get("eligible_for_paper_challenger")
    report["dataset_id"] = _string_value(local_payload.get("dataset_id"))
    report["frequency"] = _string_value(local_payload.get("frequency"))
    report["as_of_date"] = _string_value(local_payload.get("as_of_date"))

    _validate_version_tags_against_local_registry(version_tags, local_payload, failures)
    _validate_local_registry_candidate(local_payload, registry_run_id=registry_run_id, failures=failures)

    model_run_path = mlflow_model_registry._model_run_artifact_path(local_payload)
    model_run = mlflow_adapter._read_json_object(model_run_path, "model run")
    feature_names = _validate_logistic_model_run(model_run, registry_run_id=registry_run_id, failures=failures)
    report["feature_names"] = list(feature_names)

    if failures:
        return _write_failed_review(report, output_path=output_path, markdown_path=markdown_path)

    pyfunc_model, model_uri = _load_candidate_pyfunc(
        mlflow,
        registered_model_name=registered_model_name,
        alias=alias,
        version=version_number,
        warnings=warnings,
    )
    report["model_uri"] = model_uri

    smoke_rows = _load_smoke_rows(
        feature_source=feature_source,
        config_path=Path(config),
        feature_names=feature_names,
        failures=failures,
    )
    if failures:
        return _write_failed_review(report, output_path=output_path, markdown_path=markdown_path)

    predictions = _predict_candidate(pyfunc_model, smoke_rows)
    prediction_sample = _validate_predictions(predictions, smoke_rows=smoke_rows, failures=failures)
    report["prediction_sample"] = prediction_sample

    if failures:
        return _write_failed_review(report, output_path=output_path, markdown_path=markdown_path)

    report["status"] = PASSED_STATUS
    return _write_review(report, output_path=output_path, markdown_path=markdown_path)


def _base_report(
    *,
    registered_model_name: str,
    alias: str,
    feature_source: Path,
    failures: list[str],
    warnings: list[str],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": FAILED_STATUS,
        "registered_model_name": registered_model_name,
        "alias": alias,
        "model_version": "",
        "model_uri": "",
        "registry_run_id": "",
        "local_registry_status": "",
        "eligible_for_paper_challenger": None,
        "dataset_id": "",
        "frequency": "",
        "as_of_date": "",
        "feature_names": [],
        "feature_source": str(feature_source),
        "prediction_sample": [],
        "failures": failures,
        "warnings": warnings,
    }


def _get_model_version_by_alias(client: object, *, registered_model_name: str, alias: str) -> object:
    get_model_version_by_alias = getattr(client, "get_model_version_by_alias", None)
    if not callable(get_model_version_by_alias):
        raise MlflowPaperCandidateOperationalError("MLflow client cannot resolve registered model aliases")
    try:
        return get_model_version_by_alias(registered_model_name, alias)
    except Exception as exc:
        raise MlflowPaperCandidateOperationalError(
            f"MLflow model alias not found: model={registered_model_name} alias={alias}"
        ) from exc


def _validate_version_tags_before_local_lookup(tags: Mapping[str, object], failures: list[str]) -> None:
    expected = {
        mlflow_adapter.REGISTRY_SOURCE_TAG: mlflow_adapter.LOCAL_REGISTRY_SOURCE,
        mlflow_adapter.STATUS_TAG: "APPROVED",
        mlflow_model_registry.ELIGIBLE_TAG: "true",
    }
    for tag_name, expected_value in expected.items():
        actual = _tag_value(tags, tag_name)
        if actual != expected_value:
            failures.append(f"model version tag {tag_name} expected {expected_value}, got {_display_value(actual)}")


def _validate_version_tags_against_local_registry(
    tags: Mapping[str, object],
    local_payload: Mapping[str, object],
    failures: list[str],
) -> None:
    comparisons = (
        (mlflow_adapter.DATASET_ID_TAG, "dataset_id"),
        (mlflow_adapter.FREQUENCY_TAG, "frequency"),
        (mlflow_adapter.AS_OF_DATE_TAG, "as_of_date"),
    )
    for tag_name, field in comparisons:
        expected = _string_value(local_payload.get(field))
        actual = _tag_value(tags, tag_name)
        if actual != expected:
            failures.append(f"model version tag {tag_name} expected {expected}, got {_display_value(actual)}")


def _validate_local_registry_candidate(
    payload: Mapping[str, object],
    *,
    registry_run_id: str,
    failures: list[str],
) -> None:
    status = payload.get("status")
    if status != "APPROVED":
        failures.append(f"local registry run is not APPROVED: {registry_run_id} status={_display_value(status)}")
    if payload.get("eligible_for_paper_challenger") is not True:
        failures.append(f"local registry run is not eligible for paper challenger: {registry_run_id}")


def _validate_logistic_model_run(
    model_run: Mapping[str, object],
    *,
    registry_run_id: str,
    failures: list[str],
) -> tuple[str, ...]:
    try:
        _, feature_names = mlflow_model_registry._validated_logistic_model_run(
            model_run,
            registry_run_id=registry_run_id,
        )
    except mlflow_model_registry.MlflowModelRegistryOperationalError as exc:
        failures.append(str(exc))
        return ()
    return feature_names


def _load_candidate_pyfunc(
    mlflow: object,
    *,
    registered_model_name: str,
    alias: str,
    version: str,
    warnings: list[str],
) -> tuple[object, str]:
    pyfunc = getattr(mlflow, "pyfunc", None)
    load_model = getattr(pyfunc, "load_model", None)
    if not callable(load_model):
        raise MlflowPaperCandidateOperationalError("MLflow installation does not expose pyfunc.load_model")

    alias_uri = f"models:/{registered_model_name}@{alias}"
    version_uri = f"models:/{registered_model_name}/{version}"
    try:
        return load_model(alias_uri), alias_uri
    except Exception as alias_exc:
        try:
            model = load_model(version_uri)
        except Exception as version_exc:
            raise MlflowPaperCandidateOperationalError(
                f"MLflow pyfunc model load failed for {alias_uri} and {version_uri}: {version_exc}"
            ) from alias_exc
        warnings.append(f"alias model URI failed; loaded version URI instead: {version_uri}")
        return model, version_uri


def _load_smoke_rows(
    *,
    feature_source: Path,
    config_path: Path,
    feature_names: tuple[str, ...],
    failures: list[str],
) -> tuple[Mapping[str, object], ...]:
    if not feature_source.exists():
        raise MlflowPaperCandidateOperationalError(f"feature source not found: {feature_source}")
    records = read_records(feature_source)
    if not records:
        failures.append(f"feature source contains no rows: {feature_source}")
        return ()

    missing_columns = [name for name in feature_names if all(name not in row for row in records)]
    if missing_columns:
        failures.append("feature source missing required column(s): " + ", ".join(missing_columns))
        return ()

    universe = load_universe_config(config_path)
    latest_rows = latest_valid_feature_rows(
        records,
        feature_names=feature_names,
        allowlist=universe.symbols,
    )
    if not latest_rows:
        failures.append("feature source has no valid latest rows for the configured universe")
        return ()
    return tuple(dict(row) for _, row in sorted(latest_rows.items()))


def _predict_candidate(pyfunc_model: object, smoke_rows: tuple[Mapping[str, object], ...]) -> object:
    predict = getattr(pyfunc_model, "predict", None)
    if not callable(predict):
        raise MlflowPaperCandidateOperationalError("loaded MLflow pyfunc model does not expose predict")
    try:
        return predict(list(smoke_rows))
    except TypeError as first_exc:
        try:
            return predict(None, list(smoke_rows))
        except TypeError:
            raise MlflowPaperCandidateOperationalError(f"MLflow pyfunc prediction failed: {first_exc}") from first_exc
        except Exception as exc:
            raise MlflowPaperCandidateOperationalError(f"MLflow pyfunc prediction failed: {exc}") from exc
    except Exception as exc:
        raise MlflowPaperCandidateOperationalError(f"MLflow pyfunc prediction failed: {exc}") from exc


def _validate_predictions(
    predictions: object,
    *,
    smoke_rows: tuple[Mapping[str, object], ...],
    failures: list[str],
) -> list[dict[str, object]]:
    try:
        prediction_records = _prediction_records(predictions)
    except ValueError as exc:
        failures.append(str(exc))
        return []

    if len(prediction_records) != len(smoke_rows):
        failures.append(f"prediction row count mismatch: expected {len(smoke_rows)}, got {len(prediction_records)}")
        return []

    sample: list[dict[str, object]] = []
    for index, prediction in enumerate(prediction_records):
        probability_raw = prediction.get("probability")
        try:
            probability = float(str(probability_raw))
        except (TypeError, ValueError):
            failures.append(f"prediction {index} probability is not numeric")
            continue
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            failures.append(f"prediction {index} probability is outside [0, 1]")
            continue

        prediction_value = prediction.get("prediction")
        if prediction_value not in {0, 1}:
            failures.append(f"prediction {index} class is not 0 or 1")
            continue

        row = smoke_rows[index]
        sample.append(
            {
                "timestamp": _string_value(row.get("timestamp")),
                "symbol": _string_value(row.get("symbol")).upper(),
                "probability": probability,
                "prediction": int(prediction_value),
            }
        )
    return sample


def _prediction_records(predictions: object) -> tuple[Mapping[str, object], ...]:
    to_dict = getattr(predictions, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = None
        if isinstance(records, list):
            return tuple(_require_mapping(record, "prediction rows must be objects") for record in records)

    if isinstance(predictions, Mapping):
        if _looks_like_column_mapping(predictions):
            return _records_from_column_mapping(predictions)
        return (predictions,)

    if isinstance(predictions, (list, tuple)):
        return tuple(_require_mapping(record, "prediction rows must be objects") for record in predictions)

    raise ValueError("prediction output must be a DataFrame, mapping, or list of mappings")


def _records_from_column_mapping(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    columns = {str(key): _sequence_list(value) for key, value in payload.items() if _is_sequence(value)}
    lengths = {len(value) for value in columns.values()}
    if len(lengths) != 1:
        raise ValueError("prediction column lengths must match")
    row_count = next(iter(lengths), 0)
    return tuple({key: values[index] for key, values in columns.items()} for index in range(row_count))


def _looks_like_column_mapping(payload: Mapping[str, object]) -> bool:
    if not payload:
        return False
    return all(_is_sequence(value) for value in payload.values())


def _is_sequence(value: object) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray, Mapping))


def _require_mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(message)
    return value


def _write_failed_review(
    report: dict[str, object],
    *,
    output_path: Path,
    markdown_path: Path,
) -> MlflowPaperCandidateReviewResult:
    report["status"] = FAILED_STATUS
    result = _write_review(report, output_path=output_path, markdown_path=markdown_path)
    failures = report.get("failures")
    if isinstance(failures, list) and failures:
        message = "; ".join(str(failure) for failure in failures)
    else:
        message = "MLflow paper-candidate review failed"
    raise MlflowPaperCandidateValidationError(message, result=result)


def _write_review(
    report: Mapping[str, object],
    *,
    output_path: Path,
    markdown_path: Path,
) -> MlflowPaperCandidateReviewResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_mlflow_paper_candidate_review_markdown(report), encoding="utf-8")
    return MlflowPaperCandidateReviewResult(
        output_path=output_path,
        markdown_path=markdown_path,
        report=report,
    )


def render_mlflow_paper_candidate_review_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# MLflow Paper Candidate Review",
        "",
        f"Status: {_string_value(report.get('status'))}",
        "",
        f"- Registered model: {_string_value(report.get('registered_model_name'))}",
        f"- Alias: {_string_value(report.get('alias'))}",
        f"- Model version: {_string_value(report.get('model_version'))}",
        f"- Model URI: {_string_value(report.get('model_uri'))}",
        f"- Registry run: {_string_value(report.get('registry_run_id'))}",
        f"- Local registry status: {_string_value(report.get('local_registry_status'))}",
        f"- Eligible for paper challenger: {_string_value(report.get('eligible_for_paper_challenger'))}",
        f"- Dataset: {_string_value(report.get('dataset_id'))}",
        f"- Frequency: {_string_value(report.get('frequency'))}",
        f"- As of date: {_string_value(report.get('as_of_date'))}",
        f"- Feature source: {_string_value(report.get('feature_source'))}",
        "",
        "## Prediction Sample",
        "",
        "| Symbol | Timestamp | Probability | Prediction |",
        "| --- | --- | ---: | ---: |",
    ]
    sample = report.get("prediction_sample")
    if isinstance(sample, list) and sample:
        for raw_row in sample:
            row = raw_row if isinstance(raw_row, Mapping) else {}
            lines.append(
                "| "
                + " | ".join(
                    (
                        _escape_markdown_cell(row.get("symbol")),
                        _escape_markdown_cell(row.get("timestamp")),
                        _escape_markdown_cell(_format_probability(row.get("probability"))),
                        _escape_markdown_cell(row.get("prediction")),
                    )
                )
                + " |"
            )
    else:
        lines.append("|  |  |  |  |")

    lines.extend(["", "## Failures", ""])
    _append_markdown_list(lines, report.get("failures"))
    lines.extend(["", "## Warnings", ""])
    _append_markdown_list(lines, report.get("warnings"))
    return "\n".join(lines) + "\n"


def _append_markdown_list(lines: list[str], values: object) -> None:
    if isinstance(values, list) and values:
        for value in values:
            lines.append(f"- {_string_value(value)}")
        return
    lines.append("- None")


def _model_version_number(version: object) -> str:
    value = getattr(version, "version", None)
    if value is None:
        raise MlflowPaperCandidateOperationalError("MLflow model version missing version number")
    return str(value)


def _version_tag_mapping(version: object) -> Mapping[str, object]:
    tags = getattr(version, "tags", None)
    return tags if isinstance(tags, Mapping) else {}


def _tag_value(tags: Mapping[str, object], tag_name: str) -> str:
    value = tags.get(tag_name)
    return value.strip() if isinstance(value, str) else ""


def _display_value(value: object) -> str:
    if value is None or value == "":
        return "<missing>"
    return str(value)


def _string_value(value: object) -> str:
    return "" if value is None else str(value)


def _format_probability(value: object) -> str:
    if value is None:
        return ""
    try:
        return f"{float(str(value)):.6f}"
    except (TypeError, ValueError):
        return str(value)


def _sequence_list(value: object) -> list[object]:
    return list(cast(Iterable[object], value))


def _escape_markdown_cell(value: object) -> str:
    return _string_value(value).replace("|", "\\|").replace("\n", " ")
