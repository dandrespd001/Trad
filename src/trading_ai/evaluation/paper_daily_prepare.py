"""Prepare approved offline data packages for the daily paper operator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

import yaml

from trading_ai.config import ConfigError
from trading_ai.data.catalog import (
    ApprovedDataImportError,
    ApprovedDataImportResult,
    ApprovedDataValidationError,
    import_approved_data,
)
from trading_ai.data.io import ParquetDependencyError
from trading_ai.evaluation.approved_data import ApprovedEvaluationOperationalError, evaluate_approved_data
from trading_ai.evaluation.registry import EvaluationRegistryOperationalError, register_evaluation
from trading_ai.execution.paper_common import (
    read_json_artifact,
    write_json_artifact as _write_json,
    write_text_artifact as _write_text,
)
from trading_ai.execution.paper_daily import (
    PaperDailyOperationalError,
    load_paper_daily_config,
    run_paper_daily,
)
from trading_ai.execution.paper_model_alias import resolve_paper_model_route


SCHEMA_VERSION = 1
READY_STATUS = "READY"
REJECTED_STATUS = "REJECTED"
BLOCKED_STATUS = "BLOCKED"
ERROR_STATUS = "ERROR"


class PaperDailyPrepareOperationalError(RuntimeError):
    """Raised for operational failures that should return CLI exit code 2."""


@dataclass(frozen=True)
class PaperDailyPrepareResult:
    exit_code: int
    status: str
    ready_for_paper_daily: bool
    output_dir: Path
    readiness_path: Path
    readiness_markdown_path: Path
    paper_daily_config_path: Path | None
    payload: dict[str, object]


def prepare_paper_daily(
    *,
    source: str | Path | None = None,
    approved_dir: str | Path | None = None,
    dataset_id: str | None = None,
    frequency: str | None = None,
    start: str,
    end: str,
    as_of_date: str | date,
    provider: str = "manual_csv",
    license_note: str | None = None,
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    signal_model: str | Path = "models/latest_model.json",
    paper_model_alias: str | Path | None = None,
    reference_features: str | Path | None = None,
    candidate_spec: str | Path | None = None,
    approved_output_dir: str | Path = "data/raw/approved",
    output_dir: str | Path = "reports/tmp/paper_daily_prepare",
    registry_dir: str | Path = "reports/registry",
    periods_per_year: str | int = "auto",
    min_accuracy_lift: float = 0.02,
    min_test_samples: int = 30,
    run_offline_smoke: bool = False,
) -> PaperDailyPrepareResult:
    """Import/reuse, evaluate, register, and generate a daily paper config offline."""

    if bool(source) == bool(approved_dir):
        raise PaperDailyPrepareOperationalError("provide exactly one of source or approved_dir")
    resolved_as_of_date = _parse_date(as_of_date, "as_of_date").isoformat()
    resolved_start = _parse_date(start, "from").isoformat()
    resolved_end = _parse_date(end, "to").isoformat()
    if resolved_end < resolved_start:
        raise PaperDailyPrepareOperationalError("--to must be on or after --from")
    model_route = resolve_paper_model_route(
        signal_model=signal_model,
        paper_model_alias=paper_model_alias,
        as_of_date=resolved_as_of_date,
    )
    if model_route.get("route_state") == "BLOCKED":
        run_dir = _fallback_run_dir(output_dir, dataset_id, frequency, resolved_as_of_date)
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=BLOCKED_STATUS,
            exit_code=1,
            ready_for_paper_daily=False,
            reasons=[str(model_route.get("reason") or "paper_model_alias_blocked")],
            inputs=_inputs(
                source=source,
                approved_dir=approved_dir,
                dataset_id=dataset_id,
                frequency=frequency,
                start=resolved_start,
                end=resolved_end,
                as_of_date=resolved_as_of_date,
                provider=provider,
                config=config,
                risk=risk,
                signal_model=signal_model,
                paper_model_alias=paper_model_alias,
                reference_features=reference_features,
                candidate_spec=candidate_spec,
            ),
            model_route=model_route,
        )
    active_signal_model = str(model_route.get("active_model_path") or signal_model)

    import_result: ApprovedDataImportResult | None = None
    known_dataset_id = dataset_id
    known_frequency = frequency
    run_dir: Path | None = None

    try:
        if source is not None:
            _require_source_import_fields(dataset_id=dataset_id, frequency=frequency, license_note=license_note)
            known_dataset_id = str(dataset_id)
            known_frequency = str(frequency)
            run_dir = _run_dir(output_dir, known_dataset_id, known_frequency, resolved_as_of_date)
            import_result = import_approved_data(
                source=source,
                dataset_id=known_dataset_id,
                frequency=known_frequency,
                config=config,
                provider=provider,
                license_note=str(license_note),
                output_dir=approved_output_dir,
                as_of_date=resolved_as_of_date,
            )
            active_approved_dir = import_result.dataset_path.parent
            manifest = dict(import_result.manifest)
        else:
            active_approved_dir = Path(str(approved_dir))
            manifest = _read_manifest(active_approved_dir)
            known_dataset_id = _required_manifest_string(manifest, "dataset_id")
            known_frequency = _required_manifest_string(manifest, "frequency")
            run_dir = _run_dir(output_dir, known_dataset_id, known_frequency, resolved_as_of_date)
    except ApprovedDataValidationError as exc:
        if run_dir is None:
            run_dir = _fallback_run_dir(output_dir, dataset_id, frequency, resolved_as_of_date)
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=BLOCKED_STATUS,
            exit_code=1,
            ready_for_paper_daily=False,
            reasons=list(exc.errors),
            inputs=_inputs(
                source=source,
                approved_dir=approved_dir,
                dataset_id=known_dataset_id,
                frequency=known_frequency,
                start=resolved_start,
                end=resolved_end,
                as_of_date=resolved_as_of_date,
                provider=provider,
                config=config,
                risk=risk,
                signal_model=active_signal_model,
                paper_model_alias=paper_model_alias,
                reference_features=reference_features,
                candidate_spec=candidate_spec,
            ),
            model_route=model_route,
        )
    except (ApprovedDataImportError, ParquetDependencyError, OSError, json.JSONDecodeError) as exc:
        if run_dir is not None or ((known_dataset_id or dataset_id) and (known_frequency or frequency)):
            if run_dir is None:
                run_dir = _fallback_run_dir(
                    output_dir,
                    known_dataset_id or dataset_id,
                    known_frequency or frequency,
                    resolved_as_of_date,
                )
            return _write_terminal_readiness(
                run_dir=run_dir,
                status=ERROR_STATUS,
                exit_code=2,
                ready_for_paper_daily=False,
                reasons=[str(exc)],
                inputs=_inputs(
                    source=source,
                    approved_dir=approved_dir,
                    dataset_id=known_dataset_id,
                    frequency=known_frequency,
                    start=resolved_start,
                    end=resolved_end,
                    as_of_date=resolved_as_of_date,
                    provider=provider,
                    config=config,
                    risk=risk,
                    signal_model=active_signal_model,
                    paper_model_alias=paper_model_alias,
                    reference_features=reference_features,
                    candidate_spec=candidate_spec,
                ),
                model_route=model_route,
            )
        raise PaperDailyPrepareOperationalError(str(exc)) from exc

    assert run_dir is not None
    dataset_path = active_approved_dir / "ohlcv.parquet"
    manifest_path = active_approved_dir / "manifest.json"
    catalog_entry_path = active_approved_dir / "catalog_entry.json"
    inputs = _inputs(
        source=source,
        approved_dir=approved_dir,
        dataset_id=known_dataset_id,
        frequency=known_frequency,
        start=resolved_start,
        end=resolved_end,
        as_of_date=resolved_as_of_date,
        provider=provider,
        config=config,
        risk=risk,
        signal_model=signal_model,
        paper_model_alias=paper_model_alias,
        reference_features=reference_features,
        candidate_spec=candidate_spec,
    )
    approved_dataset = _approved_dataset_payload(
        manifest,
        approved_dir=active_approved_dir,
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        catalog_entry_path=catalog_entry_path,
        import_result=import_result,
    )
    stale_reason = _dataset_stale_reason(manifest, expected_end=resolved_end)
    if stale_reason is not None:
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=BLOCKED_STATUS,
            exit_code=1,
            ready_for_paper_daily=False,
            reasons=[stale_reason],
            inputs=inputs,
            approved_dataset=approved_dataset,
            model_route=model_route,
        )

    try:
        evaluation = evaluate_approved_data(
            approved_dir=active_approved_dir,
            config=config,
            risk=risk,
            output_dir=output_dir,
            as_of_date=resolved_as_of_date,
            periods_per_year=periods_per_year,
            min_accuracy_lift=min_accuracy_lift,
            min_test_samples=min_test_samples,
            candidate_spec=candidate_spec,
        )
    except (ApprovedEvaluationOperationalError, ParquetDependencyError) as exc:
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=ERROR_STATUS,
            exit_code=2,
            ready_for_paper_daily=False,
            reasons=[str(exc)],
            inputs=inputs,
            approved_dataset=approved_dataset,
            model_route=model_route,
        )

    evaluation_payload = _evaluation_payload(evaluation)
    reasons = _evaluation_reasons(evaluation.summary_path)
    if evaluation.exit_code != 0:
        status = REJECTED_STATUS if evaluation.status == REJECTED_STATUS else BLOCKED_STATUS
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=status,
            exit_code=1,
            ready_for_paper_daily=False,
            reasons=reasons,
            inputs=inputs,
            approved_dataset=approved_dataset,
            evaluation=evaluation_payload,
            model_route=model_route,
        )

    try:
        registration = register_evaluation(evaluation_dir=evaluation.output_dir, registry_dir=registry_dir)
    except EvaluationRegistryOperationalError as exc:
        return _write_terminal_readiness(
            run_dir=run_dir,
            status=ERROR_STATUS,
            exit_code=2,
            ready_for_paper_daily=False,
            reasons=[str(exc)],
            inputs=inputs,
            approved_dataset=approved_dataset,
            evaluation=evaluation_payload,
            model_route=model_route,
        )

    config_path = run_dir / "paper_daily.generated.yml"
    config_payload = _paper_daily_config_payload(
        run_dir=run_dir,
        dataset_path=dataset_path,
        start=resolved_start,
        end=resolved_end,
        as_of_date=resolved_as_of_date,
        config=config,
        risk=risk,
        signal_model=active_signal_model,
        reference_features=reference_features,
        evaluation_dir=evaluation.output_dir,
        registry_dir=registry_dir,
    )
    _write_generated_config(config_payload, config_path)

    offline_smoke = _offline_smoke_payload(requested=run_offline_smoke, ran=False, status="NOT_REQUESTED")
    if run_offline_smoke:
        offline_smoke = _run_offline_smoke(config_path)
        smoke_exit_code = _int_or_none(offline_smoke.get("exit_code"))
        smoke_reasons = _string_list(offline_smoke.get("reasons"))
        if smoke_exit_code == 1:
            return _write_terminal_readiness(
                run_dir=run_dir,
                status=BLOCKED_STATUS,
                exit_code=1,
                ready_for_paper_daily=False,
                reasons=_dedupe_strings([*reasons, "offline_smoke_blocked", *smoke_reasons]),
                inputs=inputs,
                approved_dataset=approved_dataset,
                evaluation=evaluation_payload,
                registry=_registry_payload(registration),
                paper_daily_config_path=config_path,
                offline_smoke=offline_smoke,
                model_route=model_route,
            )
        if smoke_exit_code != 0:
            return _write_terminal_readiness(
                run_dir=run_dir,
                status=ERROR_STATUS,
                exit_code=2,
                ready_for_paper_daily=False,
                reasons=_dedupe_strings([*reasons, "offline_smoke_operational_error", *smoke_reasons]),
                inputs=inputs,
                approved_dataset=approved_dataset,
                evaluation=evaluation_payload,
                registry=_registry_payload(registration),
                paper_daily_config_path=config_path,
                offline_smoke=offline_smoke,
                model_route=model_route,
            )

    return _write_terminal_readiness(
        run_dir=run_dir,
        status=READY_STATUS,
        exit_code=0,
        ready_for_paper_daily=True,
        reasons=reasons,
        inputs=inputs,
        approved_dataset=approved_dataset,
        evaluation=evaluation_payload,
        registry=_registry_payload(registration),
        paper_daily_config_path=config_path,
        offline_smoke=offline_smoke,
        model_route=model_route,
    )


def render_readiness_markdown(payload: Mapping[str, object]) -> str:
    artifacts = _mapping(payload.get("artifacts"))
    approved_dataset = _mapping(payload.get("approved_dataset"))
    evaluation = _mapping(payload.get("evaluation"))
    registry = _mapping(payload.get("registry"))
    commands = _mapping(payload.get("recommended_commands"))
    offline_smoke = _mapping(payload.get("offline_smoke"))
    smoke_artifacts = _mapping(offline_smoke.get("artifacts"))
    reasons = _string_list(payload.get("reasons"))
    ready = "READY" if payload.get("ready_for_paper_daily") is True else "NOT READY"
    lines = [
        "# Paper Daily Readiness",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        "",
        f"Ready: `{ready}`",
        f"Exit code: `{payload.get('exit_code')}`",
        f"As of date: `{payload.get('as_of_date')}`",
        f"Dataset: `{approved_dataset.get('dataset_id') or ''}` `{approved_dataset.get('frequency') or ''}`",
        f"Approved dataset: `{approved_dataset.get('dataset_path') or ''}`",
        f"Evaluation summary: `{evaluation.get('summary_path') or ''}`",
        f"Registry run: `{registry.get('run_id') or ''}`",
        f"Generated config: `{artifacts.get('paper_daily_config') or ''}`",
        "",
        "## Offline Smoke",
        "",
        f"- Requested: `{offline_smoke.get('requested')}`",
        f"- Ran: `{offline_smoke.get('ran')}`",
        f"- Status: `{offline_smoke.get('status') or ''}`",
        f"- Exit code: `{offline_smoke.get('exit_code')}`",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    if smoke_artifacts:
        for name, path in sorted(smoke_artifacts.items()):
            lines.append(f"| `{_escape_markdown(name)}` | `{_escape_markdown(path)}` |")
    else:
        lines.append("| none |  |")
    lines.extend(
        [
            "",
            "## Recommended Commands",
            "",
            f"- Offline review: `{commands.get('offline_review') or ''}`",
            f"- Broker confirmed: `{commands.get('broker_confirmed') or ''}`",
            "",
            "## Artifacts",
            "",
            "| Artifact | Path |",
            "| --- | --- |",
        ]
    )
    for name, path in sorted(artifacts.items()):
        lines.append(f"| `{_escape_markdown(name)}` | `{_escape_markdown(path)}` |")
    if reasons:
        lines.extend(["", "## Reasons", ""])
        lines.extend(f"- `{_escape_markdown(reason)}`" for reason in reasons)
    lines.append("")
    return "\n".join(lines)


def _require_source_import_fields(*, dataset_id: str | None, frequency: str | None, license_note: str | None) -> None:
    missing = []
    if not dataset_id:
        missing.append("--dataset-id")
    if not frequency:
        missing.append("--frequency")
    if not license_note:
        missing.append("--license-note")
    if missing:
        raise PaperDailyPrepareOperationalError("--source requires " + ", ".join(missing))


def _read_manifest(approved_dir: Path) -> dict[str, object]:
    manifest_path = approved_dir / "manifest.json"
    try:
        payload = read_json_artifact(manifest_path)
    except OSError as exc:
        raise PaperDailyPrepareOperationalError(f"approved package missing manifest.json: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise PaperDailyPrepareOperationalError(f"approved package has invalid manifest JSON: {manifest_path}") from exc
    except ValueError as exc:
        raise PaperDailyPrepareOperationalError(f"approved package manifest must be an object: {manifest_path}") from exc
    return payload


def _required_manifest_string(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if value in {None, ""}:
        raise PaperDailyPrepareOperationalError(f"approved manifest missing {key}")
    return str(value)


def _run_dir(output_dir: str | Path, dataset_id: str, frequency: str, as_of_date: str) -> Path:
    return Path(output_dir) / dataset_id / frequency / as_of_date


def _fallback_run_dir(
    output_dir: str | Path,
    dataset_id: str | None,
    frequency: str | None,
    as_of_date: str,
) -> Path:
    return Path(output_dir) / str(dataset_id or "unknown_dataset") / str(frequency or "unknown_frequency") / as_of_date


def _write_terminal_readiness(
    *,
    run_dir: Path,
    status: str,
    exit_code: int,
    ready_for_paper_daily: bool,
    reasons: list[str],
    inputs: Mapping[str, object],
    approved_dataset: Mapping[str, object] | None = None,
    evaluation: Mapping[str, object] | None = None,
    registry: Mapping[str, object] | None = None,
    paper_daily_config_path: Path | None = None,
    offline_smoke: Mapping[str, object] | None = None,
    model_route: Mapping[str, object] | None = None,
) -> PaperDailyPrepareResult:
    readiness_path = run_dir / "readiness.json"
    readiness_markdown_path = run_dir / "readiness.md"
    payload = _readiness_payload(
        run_dir=run_dir,
        status=status,
        exit_code=exit_code,
        ready_for_paper_daily=ready_for_paper_daily,
        reasons=reasons,
        inputs=inputs,
        approved_dataset=approved_dataset,
        evaluation=evaluation,
        registry=registry,
        paper_daily_config_path=paper_daily_config_path,
        offline_smoke=offline_smoke,
        model_route=model_route,
    )
    _write_json(payload, readiness_path)
    _write_text(render_readiness_markdown(payload), readiness_markdown_path)
    return PaperDailyPrepareResult(
        exit_code=exit_code,
        status=status,
        ready_for_paper_daily=ready_for_paper_daily,
        output_dir=run_dir,
        readiness_path=readiness_path,
        readiness_markdown_path=readiness_markdown_path,
        paper_daily_config_path=paper_daily_config_path,
        payload=payload,
    )


def _readiness_payload(
    *,
    run_dir: Path,
    status: str,
    exit_code: int,
    ready_for_paper_daily: bool,
    reasons: list[str],
    inputs: Mapping[str, object],
    approved_dataset: Mapping[str, object] | None,
    evaluation: Mapping[str, object] | None,
    registry: Mapping[str, object] | None,
    paper_daily_config_path: Path | None,
    offline_smoke: Mapping[str, object] | None,
    model_route: Mapping[str, object] | None,
) -> dict[str, object]:
    artifacts = {
        "readiness_json": str(run_dir / "readiness.json"),
        "readiness_markdown": str(run_dir / "readiness.md"),
        "paper_daily_config": str(paper_daily_config_path) if paper_daily_config_path is not None else None,
    }
    if evaluation:
        artifacts.update(
            {
                "evaluation_summary": evaluation.get("summary_path"),
                "data_quality": evaluation.get("data_quality_path"),
                "backtest_report": evaluation.get("backtest_report"),
                "promotion_report": evaluation.get("promotion_report"),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "ready_for_paper_daily": ready_for_paper_daily,
        "exit_code": exit_code,
        "as_of_date": inputs.get("as_of_date"),
        "output_dir": str(run_dir),
        "inputs": dict(inputs),
        "approved_dataset": dict(approved_dataset or {}),
        "evaluation": dict(evaluation or {}),
        "registry": dict(registry or {"registered": False}),
        "paper_daily_config_path": str(paper_daily_config_path) if paper_daily_config_path is not None else None,
        "offline_smoke": dict(
            offline_smoke
            or _offline_smoke_payload(requested=False, ran=False, status="NOT_REQUESTED")
        ),
        "model_route": dict(model_route or {"route_state": "CHAMPION", "active_model_path": inputs.get("signal_model"), "alias_hash": None, "reason": "paper_model_alias_not_provided"}),
        "artifacts": artifacts,
        "recommended_commands": _recommended_commands(
            paper_daily_config_path,
            readiness_path=run_dir / "readiness.json",
        ),
        "reasons": _dedupe_strings(reasons),
        "safety": {
            "network_downloads": False,
            "alpaca_client_built": False,
            "credentials_read": False,
            "model_latest_mutated": False,
            "broker_requires_cli_confirmations": True,
            "telegram_requires_cli_opt_in": True,
        },
    }
    return payload


def _inputs(
    *,
    source: str | Path | None,
    approved_dir: str | Path | None,
    dataset_id: str | None,
    frequency: str | None,
    start: str,
    end: str,
    as_of_date: str,
    provider: str,
    config: str | Path,
    risk: str | Path,
    signal_model: str | Path,
    paper_model_alias: str | Path | None,
    reference_features: str | Path | None,
    candidate_spec: str | Path | None,
) -> dict[str, object]:
    return {
        "source": str(source) if source is not None else None,
        "approved_dir": str(approved_dir) if approved_dir is not None else None,
        "dataset_id": dataset_id,
        "frequency": frequency,
        "from": start,
        "to": end,
        "as_of_date": as_of_date,
        "provider": provider,
        "config": str(config),
        "risk": str(risk),
        "signal_model": str(signal_model),
        "paper_model_alias": str(paper_model_alias) if paper_model_alias is not None else None,
        "reference_features": str(reference_features) if reference_features is not None else None,
        "candidate_spec": str(candidate_spec) if candidate_spec is not None else None,
    }


def _approved_dataset_payload(
    manifest: Mapping[str, object],
    *,
    approved_dir: Path,
    dataset_path: Path,
    manifest_path: Path,
    catalog_entry_path: Path,
    import_result: ApprovedDataImportResult | None,
) -> dict[str, object]:
    return {
        "dataset_id": manifest.get("dataset_id"),
        "frequency": manifest.get("frequency"),
        "dataset_hash": manifest.get("dataset_hash"),
        "source_sha256": manifest.get("source_sha256"),
        "symbols": manifest.get("symbols"),
        "row_count": manifest.get("row_count"),
        "start": manifest.get("start"),
        "end": manifest.get("end"),
        "approved_dir": str(approved_dir),
        "dataset_path": str(dataset_path),
        "manifest_path": str(manifest_path),
        "catalog_entry_path": str(catalog_entry_path),
        "imported_in_prepare": import_result is not None,
    }


def _dataset_stale_reason(manifest: Mapping[str, object], *, expected_end: str) -> str | None:
    latest = manifest.get("end")
    if latest in {None, ""}:
        return "dataset_stale:missing_end"
    try:
        latest_date = _parse_date(str(latest)[:10], "dataset_end")
        expected_date = _parse_date(expected_end, "to")
    except PaperDailyPrepareOperationalError:
        return f"dataset_stale:invalid_end:{latest}"
    if latest_date < expected_date:
        return f"dataset_stale:latest={latest_date.isoformat()}:expected={expected_date.isoformat()}"
    return None


def _evaluation_payload(result) -> dict[str, object]:
    artifacts = _summary_artifacts(result.summary_path)
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "output_dir": str(result.output_dir),
        "summary_path": str(result.summary_path),
        "summary_markdown_path": str(result.summary_markdown_path),
        "data_quality_path": str(result.data_quality_path),
        "promotion_decision_path": (
            str(result.promotion_decision_path) if result.promotion_decision_path is not None else None
        ),
        "backtest_report": _artifact_path(artifacts, "backtest"),
        "backtest_markdown": _artifact_path(artifacts, "backtest_markdown"),
        "promotion_report": _artifact_path(artifacts, "promotion_decision"),
    }


def _evaluation_reasons(summary_path: Path) -> list[str]:
    try:
        payload = read_json_artifact(summary_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return _string_list(payload.get("reasons"))


def _summary_artifacts(summary_path: Path) -> Mapping[str, object]:
    try:
        payload = read_json_artifact(summary_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return _mapping(payload.get("artifacts"))


def _artifact_path(artifacts: Mapping[str, object], name: str) -> str | None:
    artifact = _mapping(artifacts.get(name))
    path = artifact.get("path")
    return str(path) if path not in {None, ""} else None


def _registry_payload(result) -> dict[str, object]:
    return {
        "registered": True,
        "run_id": result.run_id,
        "status": result.status,
        "registry_dir": str(result.registry_dir),
        "run_path": str(result.run_path),
        "index_path": str(result.index_path),
        "markdown_path": str(result.markdown_path),
    }


def _paper_daily_config_payload(
    *,
    run_dir: Path,
    dataset_path: Path,
    start: str,
    end: str,
    as_of_date: str,
    config: str | Path,
    risk: str | Path,
    signal_model: str | Path,
    reference_features: str | Path | None,
    evaluation_dir: Path,
    registry_dir: str | Path,
) -> dict[str, object]:
    backtest_report = evaluation_dir / "backtest.json"
    promotion_report = evaluation_dir / "promotion_decision.json"
    paper_daily_dir = run_dir / "paper_daily"
    sessions_root = paper_daily_dir / "sessions"
    session_dir = sessions_root / "daily" / "{as_of_date}"
    return {
        "paper_daily": {
            "source_csv": str(dataset_path),
            "from": start,
            "to": end,
            "as_of_date": as_of_date,
            "session_dir": str(session_dir),
            "sessions_root": str(sessions_root),
            "output": str(paper_daily_dir / "daily.json"),
            "markdown_output": str(paper_daily_dir / "daily.md"),
            "observability_output": str(paper_daily_dir / "observability.json"),
            "observability_markdown_output": str(paper_daily_dir / "observability.md"),
            "monitor_output": str(paper_daily_dir / "monitor.json"),
            "monitor_markdown_output": str(paper_daily_dir / "monitor.md"),
            "ledger_output": None,
            "config": str(config),
            "risk": str(risk),
            "signal_model": str(signal_model),
            "reference_features": str(reference_features) if reference_features is not None else None,
            "signal_threshold": 0.5,
            "max_age_days": 5,
            "max_feature_age_days": 5,
            "backtest_report": str(backtest_report),
            "promotion_report": str(promotion_report),
            "reconciliation_report": None,
            "review_mlflow_paper_candidate": False,
            "mlflow_registry_dir": str(registry_dir),
            "mlflow_tracking_uri": "reports/mlruns",
            "mlflow_registered_model_name": "approved-data-logistic-baseline",
            "mlflow_alias": "paper-candidate",
        }
    }


def _write_generated_config(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Generated by prepare-paper-daily. Broker and Telegram actions remain CLI opt-in.\n"
        + yaml.safe_dump(dict(payload), sort_keys=False)
    )
    _write_text(text, path)


def _run_offline_smoke(config_path: Path) -> dict[str, object]:
    try:
        config = load_paper_daily_config(config_path)
    except (ConfigError, OSError, ValueError) as exc:
        return _offline_smoke_payload(
            requested=True,
            ran=False,
            status=ERROR_STATUS,
            exit_code=2,
            config_path=config_path,
            reasons=[str(exc)],
        )

    try:
        result = run_paper_daily(
            config=config,
            confirm_paper=False,
            confirm_auto_close=False,
            confirm_auto_submit=False,
            send_telegram=False,
        )
    except (ConfigError, PaperDailyOperationalError, ParquetDependencyError, OSError, ValueError) as exc:
        return _offline_smoke_payload(
            requested=True,
            ran=True,
            status=ERROR_STATUS,
            exit_code=2,
            config_path=config_path,
            artifacts=_smoke_artifacts_from_config(config),
            reasons=[str(exc)],
        )

    return _offline_smoke_payload(
        requested=True,
        ran=True,
        status=result.status,
        exit_code=result.exit_code,
        config_path=config_path,
        artifacts=_smoke_artifacts_from_result(result.payload, config=config),
        reasons=_string_list(result.payload.get("reasons")),
    )


def _offline_smoke_payload(
    *,
    requested: bool,
    ran: bool,
    status: str,
    exit_code: int | None = None,
    config_path: Path | None = None,
    artifacts: Mapping[str, object] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "requested": requested,
        "ran": ran,
        "status": status,
        "exit_code": exit_code,
        "config_path": str(config_path) if config_path is not None else None,
        "artifacts": dict(artifacts or {}),
        "reasons": _dedupe_strings(reasons or []),
        "confirmations": {
            "confirm_paper": False,
            "confirm_auto_close": False,
            "confirm_auto_submit": False,
        },
        "telegram": {"send_telegram": False},
    }


def _smoke_artifacts_from_result(payload: Mapping[str, object], *, config) -> dict[str, object]:
    result_artifacts = _mapping(payload.get("artifacts"))
    artifacts = _smoke_artifacts_from_config(config)
    for key, value in result_artifacts.items():
        if value is not None and value != "":
            artifacts[str(key)] = value
    return artifacts


def _smoke_artifacts_from_config(config) -> dict[str, object]:
    return {
        "daily_json": str(config.output),
        "daily_markdown": str(config.markdown_output),
        "session_dir": str(config.session_dir),
        "session_json": str(config.session_dir / "session.json"),
        "observability_json": str(config.observability_output),
        "observability_markdown": str(config.observability_markdown_output),
        "monitor_json": str(config.monitor_output),
        "monitor_markdown": str(config.monitor_markdown_output),
    }


def _recommended_commands(config_path: Path | None, *, readiness_path: Path) -> dict[str, str | None]:
    if config_path is None:
        return {"offline_review": None, "broker_confirmed": None}
    offline_base = f"PYTHONPATH=src python3 -m trading_ai.cli paper-daily --config {config_path}"
    broker_base = f"PYTHONPATH=src python3 -m trading_ai.cli paper-daily-from-readiness --readiness {readiness_path}"
    return {
        "offline_review": offline_base,
        "broker_confirmed": (
            f"{broker_base} --confirm-readiness --confirm-paper "
            "--confirm-auto-close --confirm-auto-submit --require-clean-state"
        ),
    }


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PaperDailyPrepareOperationalError(f"invalid {field_name}: {value}") from exc


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in {None, ""}]
    if value in {None, ""}:
        return []
    return [str(value)]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
