"""Offline paper-session orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from trading_ai.config import load_risk_config, load_universe_config
from trading_ai.data.freshness import evaluate_ohlcv_freshness
from trading_ai.data.io import read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.data.market_data import ApprovedLocalMarketDataProvider, MarketDataRequest
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.alpaca_paper import (
    AlpacaPaperBroker,
    PaperOrder,
    PaperOrderResult,
    PaperPreflightDecision,
    evaluate_paper_preflight,
)
from trading_ai.execution.paper_audit import evaluate_paper_audit, render_paper_audit_markdown
from trading_ai.features.engineering import build_features
from trading_ai.monitoring.drift import evaluate_feature_drift, render_feature_drift_markdown
from trading_ai.models.baseline import load_model
from trading_ai.models.signals import ModelSignal, generate_model_signals, latest_valid_feature_rows


SCHEMA_VERSION = "1.0"
PAPER_SIGNAL_ORDER_NOTIONAL = 1.0


@dataclass(frozen=True)
class PaperSessionResult:
    exit_code: int
    ready_for_paper_review: bool
    output_dir: Path
    session_path: Path
    audit_path: Path
    signal_path: Path
    freshness_path: Path
    drift_path: Path | None
    mlflow_candidate_review_path: Path | None


def run_offline_paper_session(
    *,
    source_csv: str | Path,
    start: str,
    end: str,
    reference_features: str | Path | None = None,
    output_dir: str | Path = "reports/tmp/paper_session/latest",
    config: str | Path = "configs/universe.yml",
    risk: str | Path = "configs/risk.yml",
    signal_model: str | Path = "models/latest_model.json",
    as_of_date: str | date = "today",
    signal_threshold: float = 0.5,
    max_age_days: int = 5,
    max_feature_age_days: int = 5,
    backtest_report: str | Path | None = None,
    promotion_report: str | Path | None = None,
    reconciliation_report: str | Path | None = None,
    review_mlflow_paper_candidate: bool = False,
    mlflow_registry_dir: str | Path = "reports/registry",
    mlflow_tracking_uri: str | Path = "reports/mlruns",
    mlflow_registered_model_name: str = "approved-data-logistic-baseline",
    mlflow_alias: str = "paper-candidate",
) -> PaperSessionResult:
    resolved_as_of_date = _resolve_as_of_date(as_of_date)
    universe = load_universe_config(config)
    risk_limits = load_risk_config(risk)
    model = load_model(str(signal_model))

    provider = ApprovedLocalMarketDataProvider(source_csv)
    raw_records = provider.load(MarketDataRequest(symbols=universe.symbols, start=start, end=end))
    validation = validate_ohlcv_records(raw_records)
    if not validation.valid:
        raise ValueError("source CSV failed OHLCV validation: " + "; ".join(validation.errors))

    feature_records = build_features(raw_records) if raw_records else []
    latest_rows = latest_valid_feature_rows(
        feature_records,
        feature_names=model.feature_names,
        allowlist=universe.symbols,
    )
    freshness = evaluate_ohlcv_freshness(
        latest_rows.values(),
        expected_symbols=universe.symbols,
        as_of_date=resolved_as_of_date,
        max_age_days=max_age_days,
    )

    root = Path(output_dir)
    fresh_dir = root / "fresh_data"
    monitoring_dir = root / "monitoring"
    paper_dir = root / "paper"
    audit_dir = root / "audit"
    for directory in (fresh_dir, paper_dir, audit_dir):
        directory.mkdir(parents=True, exist_ok=True)

    raw_path = fresh_dir / "raw.csv"
    features_path = fresh_dir / "features.csv"
    raw_manifest_path = fresh_dir / "raw_manifest.json"
    features_manifest_path = fresh_dir / "features_manifest.json"
    freshness_path = fresh_dir / "freshness.json"
    signal_path = paper_dir / "paper_signal_order.json"
    mlflow_candidate_review_path = root / "mlflow" / "paper_candidate_review.json"
    mlflow_candidate_review_markdown_path = root / "mlflow" / "paper_candidate_review.md"
    audit_path = audit_dir / "paper_audit.json"
    audit_markdown_path = audit_dir / "paper_audit.md"
    session_path = root / "session.json"
    session_markdown_path = root / "session.md"

    if raw_records:
        write_records(raw_records, raw_path)
    if feature_records:
        write_records(feature_records, features_path)
    _write_json(
        _dataset_manifest(
            raw_records,
            source=str(source_csv),
            dataset_path=raw_path,
            request={"symbols": list(universe.symbols), "start": start, "end": end},
        ),
        raw_manifest_path,
    )
    _write_json(
        _dataset_manifest(
            feature_records,
            source=str(raw_path),
            dataset_path=features_path,
            request={"symbols": list(universe.symbols), "start": start, "end": end},
        ),
        features_manifest_path,
    )
    freshness_payload = freshness.to_dict()
    freshness_payload["model_path"] = str(signal_model)
    freshness_payload["feature_names"] = list(model.feature_names)
    freshness_payload["raw_path"] = str(raw_path)
    freshness_payload["features_path"] = str(features_path)
    _write_json(freshness_payload, freshness_path)

    drift_payload = None
    drift_path = None
    if reference_features is not None:
        monitoring_dir.mkdir(parents=True, exist_ok=True)
        drift_path = monitoring_dir / "drift.json"
        drift_markdown_path = monitoring_dir / "drift.md"
        reference_rows = read_records(reference_features)
        drift_report = evaluate_feature_drift(
            reference_rows,
            feature_records,
            feature_names=model.feature_names,
            sources={
                "reference_features": str(reference_features),
                "current_features": str(features_path),
            },
        )
        drift_payload = drift_report.to_dict()
        _write_json(drift_payload, drift_path)
        drift_markdown_path.write_text(render_feature_drift_markdown(drift_report), encoding="utf-8")

    signal_payload = _build_signal_order_report(
        feature_records=feature_records,
        model=model,
        allowlist=universe.symbols,
        risk_limits=risk_limits,
        freshness_allowed=freshness.allowed,
        signal_threshold=signal_threshold,
        as_of_date=resolved_as_of_date,
        max_feature_age_days=max_feature_age_days,
    )
    _write_json(signal_payload, signal_path)

    mlflow_candidate_review_payload = None
    active_mlflow_candidate_review_path = None
    if review_mlflow_paper_candidate:
        active_mlflow_candidate_review_path = mlflow_candidate_review_path
        mlflow_candidate_review_payload = _run_or_write_mlflow_candidate_review(
            feature_records=feature_records,
            feature_names=model.feature_names,
            features_path=features_path,
            config=config,
            registry_dir=mlflow_registry_dir,
            tracking_uri=mlflow_tracking_uri,
            registered_model_name=mlflow_registered_model_name,
            alias=mlflow_alias,
            output_path=mlflow_candidate_review_path,
            markdown_path=mlflow_candidate_review_markdown_path,
        )

    optional_reconciliation = _read_optional_json_report(reconciliation_report)
    optional_backtest = _read_optional_json_report(backtest_report)
    optional_promotion = _read_optional_json_report(promotion_report)
    audit_sources = {
        "freshness_report": str(freshness_path),
        "signal_report": str(signal_path),
    }
    if reconciliation_report is not None:
        audit_sources["reconciliation_report"] = str(reconciliation_report)
    if backtest_report is not None:
        audit_sources["backtest_report"] = str(backtest_report)
    if promotion_report is not None:
        audit_sources["promotion_report"] = str(promotion_report)
    if drift_path is not None:
        audit_sources["drift_report"] = str(drift_path)
    if active_mlflow_candidate_review_path is not None:
        audit_sources["mlflow_candidate_review_report"] = str(active_mlflow_candidate_review_path)

    audit_report = evaluate_paper_audit(
        freshness_report=freshness_payload,
        signal_report=signal_payload,
        reconciliation_report=optional_reconciliation,
        backtest_report=optional_backtest,
        promotion_report=optional_promotion,
        drift_report=drift_payload,
        mlflow_candidate_review_report=mlflow_candidate_review_payload,
        sources=audit_sources,
        as_of_date=resolved_as_of_date.isoformat(),
    )
    audit_payload = audit_report.to_dict()
    _write_json(audit_payload, audit_path)
    audit_markdown_path.write_text(
        render_paper_audit_markdown(
            audit_report,
            freshness_report=freshness_payload,
            signal_report=signal_payload,
        ),
        encoding="utf-8",
    )

    exit_code = 0 if audit_report.ready_for_paper_review else 1
    session_payload = _build_session_payload(
        output_dir=root,
        as_of_date=resolved_as_of_date,
        source_csv=source_csv,
        config=config,
        risk=risk,
        signal_model=signal_model,
        start=start,
        end=end,
        freshness_path=freshness_path,
        signal_path=signal_path,
        audit_path=audit_path,
        drift_path=drift_path,
        mlflow_candidate_review_path=active_mlflow_candidate_review_path,
        freshness_report=freshness_payload,
        signal_report=signal_payload,
        audit_report=audit_payload,
        mlflow_candidate_review_report=mlflow_candidate_review_payload,
        exit_code=exit_code,
    )
    _write_json(session_payload, session_path)
    session_markdown_path.write_text(_render_session_markdown(session_payload), encoding="utf-8")

    return PaperSessionResult(
        exit_code=exit_code,
        ready_for_paper_review=audit_report.ready_for_paper_review,
        output_dir=root,
        session_path=session_path,
        audit_path=audit_path,
        signal_path=signal_path,
        freshness_path=freshness_path,
        drift_path=drift_path,
        mlflow_candidate_review_path=active_mlflow_candidate_review_path,
    )


def _build_signal_order_report(
    *,
    feature_records: list[dict[str, object]],
    model,
    allowlist: tuple[str, ...],
    risk_limits,
    freshness_allowed: bool,
    signal_threshold: float,
    as_of_date: date,
    max_feature_age_days: int,
) -> dict[str, object]:
    broker = AlpacaPaperBroker(client=None, allowlist=allowlist, risk_limits=risk_limits, dry_run=True)
    signals = generate_model_signals(
        feature_records,
        model=model,
        allowlist=allowlist,
        threshold=signal_threshold,
    )
    selected_signal = _select_signal_to_submit(signals)
    order_intent = None
    order_result: PaperOrderResult | None = None
    submitted = False
    order = None
    client_order_id = None
    if selected_signal is not None:
        client_order_id = _signal_client_order_id(selected_signal)
        order = PaperOrder(
            symbol=selected_signal.symbol,
            side="buy",
            notional=PAPER_SIGNAL_ORDER_NOTIONAL,
            client_order_id=client_order_id,
        )
        order_intent = _paper_order_intent_to_dict(order)

    open_orders = broker.list_orders(status="open")
    positions = broker.read_positions()
    preflight = evaluate_paper_preflight(
        signal=selected_signal,
        client_order_id=client_order_id,
        open_orders=open_orders,
        positions=positions,
        as_of_date=as_of_date,
        max_feature_age_days=max_feature_age_days,
    )
    if freshness_allowed and order is not None and preflight.allowed:
        order_result = broker.submit_order(order)
        submitted = order_result.accepted

    return {
        "mode": "dry-run",
        "broker": "alpaca",
        "freshness_allowed": freshness_allowed,
        "preflight": _paper_preflight_to_dict(preflight),
        "open_orders": [],
        "positions": [],
        "submitted": submitted,
        "signals": [_model_signal_to_dict(signal) for signal in signals],
        "selected_signal": _model_signal_to_dict(selected_signal) if selected_signal is not None else None,
        "order_intent": order_intent,
        "order_result": _paper_order_result_to_dict(order_result) if order_result is not None else None,
        "account": _paper_account_to_dict(broker.read_account()),
    }


def _build_session_payload(
    *,
    output_dir: Path,
    as_of_date: date,
    source_csv: str | Path,
    config: str | Path,
    risk: str | Path,
    signal_model: str | Path,
    start: str,
    end: str,
    freshness_path: Path,
    signal_path: Path,
    audit_path: Path,
    drift_path: Path | None,
    mlflow_candidate_review_path: Path | None,
    freshness_report: Mapping[str, object],
    signal_report: Mapping[str, object],
    audit_report: Mapping[str, object],
    mlflow_candidate_review_report: Mapping[str, object] | None,
    exit_code: int,
) -> dict[str, object]:
    audit_summary = _mapping_or_empty(audit_report.get("summary"))
    return {
        "schema_version": SCHEMA_VERSION,
        "output_dir": str(output_dir),
        "as_of_date": as_of_date.isoformat(),
        "ready_for_paper_review": audit_report.get("ready_for_paper_review") is True,
        "exit_code": exit_code,
        "inputs": {
            "source_csv": _resolved_path_text(source_csv),
            "config": _resolved_path_text(config),
            "risk": _resolved_path_text(risk),
            "signal_model": _resolved_path_text(signal_model),
            "from": start,
            "to": end,
        },
        "paths": {
            "freshness_report": _session_path_text(freshness_path, output_dir=output_dir),
            "signal_report": _session_path_text(signal_path, output_dir=output_dir),
            "audit_report": _session_path_text(audit_path, output_dir=output_dir),
            "drift_report": _session_path_text(drift_path, output_dir=output_dir),
            "mlflow_candidate_review": (
                _session_path_text(mlflow_candidate_review_path, output_dir=output_dir)
            ),
        },
        "stages": {
            "refresh_data": {
                "status": "allowed" if freshness_report.get("allowed") is True else "blocked",
                "freshness_allowed": freshness_report.get("allowed") is True,
                "reasons": list(_object_list(freshness_report.get("reasons"))),
            },
            "drift_report": {
                "status": "skipped" if drift_path is None else "completed",
                "drift_detected": audit_summary.get("drift_detected"),
            },
            "mlflow_candidate_review": {
                "status": _mlflow_candidate_review_stage_status(
                    mlflow_candidate_review_report,
                    mlflow_candidate_review_path=mlflow_candidate_review_path,
                ),
                "passed": audit_summary.get("mlflow_candidate_review_passed"),
            },
            "paper_signal_order": {
                "status": "submitted" if signal_report.get("submitted") is True else "blocked",
                "preflight_allowed": _mapping_or_empty(signal_report.get("preflight")).get("allowed"),
                "submitted": signal_report.get("submitted") is True,
            },
            "paper_audit": {
                "status": "ready" if audit_report.get("ready_for_paper_review") is True else "blocked",
                "ready_for_paper_review": audit_report.get("ready_for_paper_review") is True,
            },
        },
        "summary": {
            "fail_count": audit_summary.get("fail_count", 0),
            "warn_count": audit_summary.get("warn_count", 0),
            "info_count": audit_summary.get("info_count", 0),
            "freshness_allowed": audit_summary.get("freshness_allowed"),
            "preflight_allowed": audit_summary.get("preflight_allowed"),
            "selected_symbol": audit_summary.get("selected_symbol"),
            "submitted": audit_summary.get("submitted"),
            "order_accepted": audit_summary.get("order_accepted"),
            "drift_detected": audit_summary.get("drift_detected"),
            "drifted_feature_count": audit_summary.get("drifted_feature_count"),
            "mlflow_candidate_review_passed": audit_summary.get("mlflow_candidate_review_passed"),
            "mlflow_registry_run_id": audit_summary.get("mlflow_registry_run_id"),
            "mlflow_model_version": audit_summary.get("mlflow_model_version"),
            "mlflow_alias": audit_summary.get("mlflow_alias"),
        },
    }


def _resolved_path_text(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _session_path_text(value: Path | None, *, output_dir: Path) -> str | None:
    if value is None:
        return None
    try:
        return str(value.relative_to(output_dir))
    except ValueError:
        return str(value)


def _render_session_markdown(session: Mapping[str, object]) -> str:
    summary = _mapping_or_empty(session.get("summary"))
    paths = _mapping_or_empty(session.get("paths"))
    stages = _mapping_or_empty(session.get("stages"))
    mlflow_stage = _mapping_or_empty(stages.get("mlflow_candidate_review"))
    status = "READY" if session.get("ready_for_paper_review") is True else "BLOCKED"
    lines = [
        "# Paper Session",
        "",
        f"Status: **{status}**",
        "",
        f"As of date: `{session.get('as_of_date')}`",
        f"Exit code: `{session.get('exit_code')}`",
        (
            "Findings: "
            f"{summary.get('fail_count', 0)} fail, "
            f"{summary.get('warn_count', 0)} warn, "
            f"{summary.get('info_count', 0)} info"
        ),
        f"Drift detected: `{summary.get('drift_detected')}`",
        f"MLflow paper-candidate review: `{mlflow_stage.get('status') or 'skipped'}`",
        "",
        "## Artifacts",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
    ]
    for name, path in sorted(paths.items()):
        lines.append(f"| `{_escape_markdown(name)}` | `{_escape_markdown(path)}` |")
    lines.append("")
    return "\n".join(lines)


def _run_or_write_mlflow_candidate_review(
    *,
    feature_records: list[dict[str, object]],
    feature_names: tuple[str, ...],
    features_path: Path,
    config: str | Path,
    registry_dir: str | Path,
    tracking_uri: str | Path,
    registered_model_name: str,
    alias: str,
    output_path: Path,
    markdown_path: Path,
) -> Mapping[str, object]:
    if not feature_records:
        payload = _failed_mlflow_candidate_review_payload(
            registered_model_name=registered_model_name,
            alias=alias,
            feature_names=feature_names,
            feature_source=features_path,
            failures=[f"feature source contains no rows: {features_path}"],
        )
        _write_mlflow_candidate_review(payload, output_path=output_path, markdown_path=markdown_path)
        return payload

    from trading_ai.evaluation.mlflow_paper_candidate_review import (
        MlflowPaperCandidateValidationError,
        review_mlflow_paper_candidate as run_mlflow_paper_candidate_review,
    )

    try:
        result = run_mlflow_paper_candidate_review(
            registry_dir=registry_dir,
            tracking_uri=tracking_uri,
            registered_model_name=registered_model_name,
            alias=alias,
            features=features_path,
            config=config,
            output=output_path,
            markdown_output=markdown_path,
        )
        return result.report
    except MlflowPaperCandidateValidationError as exc:
        return exc.result.report


def _failed_mlflow_candidate_review_payload(
    *,
    registered_model_name: str,
    alias: str,
    feature_names: tuple[str, ...],
    feature_source: Path,
    failures: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "FAILED",
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
        "feature_names": list(feature_names),
        "feature_source": str(feature_source),
        "prediction_sample": [],
        "failures": list(failures),
        "warnings": [],
    }


def _write_mlflow_candidate_review(
    payload: Mapping[str, object],
    *,
    output_path: Path,
    markdown_path: Path,
) -> None:
    from trading_ai.evaluation.mlflow_paper_candidate_review import (
        render_mlflow_paper_candidate_review_markdown,
    )

    _write_json(payload, output_path)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_mlflow_paper_candidate_review_markdown(payload), encoding="utf-8")


def _dataset_manifest(
    records: list[dict[str, object]],
    *,
    source: str,
    dataset_path: Path,
    request: dict[str, object],
) -> dict[str, object]:
    manifest = build_dataset_manifest(records, source=source)
    manifest["dataset_path"] = str(dataset_path)
    manifest["request"] = request
    return manifest


def _select_signal_to_submit(signals: tuple[ModelSignal, ...]) -> ModelSignal | None:
    buy_signals = [signal for signal in signals if signal.action == "buy"]
    if not buy_signals:
        return None
    return max(buy_signals, key=lambda signal: (signal.probability, signal.symbol))


def _signal_client_order_id(signal: ModelSignal) -> str:
    compact_timestamp = "".join(character for character in signal.timestamp if character.isalnum())
    return f"signal-{signal.symbol.lower()}-{compact_timestamp[:16]}"


def _paper_order_intent_to_dict(order: PaperOrder) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": order.symbol.upper(),
        "side": order.side.lower(),
        "client_order_id": order.client_order_id,
        "type": "market",
        "time_in_force": "day",
    }
    if order.quantity is not None:
        payload["quantity"] = order.quantity
    if order.notional is not None:
        payload["notional"] = order.notional
    return payload


def _paper_order_result_to_dict(result: PaperOrderResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "accepted": result.accepted,
        "status": result.status,
        "reasons": list(result.reasons),
        "dry_run": result.dry_run,
        "broker_response": None,
    }


def _paper_preflight_to_dict(decision: PaperPreflightDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "max_feature_age_days": decision.max_feature_age_days,
    }


def _model_signal_to_dict(signal: ModelSignal) -> dict[str, object]:
    return {
        "timestamp": signal.timestamp,
        "symbol": signal.symbol,
        "probability": signal.probability,
        "threshold": signal.threshold,
        "action": signal.action,
    }


def _paper_account_to_dict(account) -> dict[str, object]:
    return {
        "account_id": account.account_id,
        "status": account.status,
        "cash": account.cash,
        "equity": account.equity,
        "buying_power": account.buying_power,
        "dry_run": account.status == "DRY_RUN",
    }


def _read_optional_json_report(path: str | Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_as_of_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    if value == "today":
        return date.today()
    return date.fromisoformat(value)


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mlflow_candidate_review_stage_status(
    report: Mapping[str, object] | None,
    *,
    mlflow_candidate_review_path: Path | None,
) -> str:
    if mlflow_candidate_review_path is None:
        return "skipped"
    if report is not None and str(report.get("status") or "").upper() == "PASSED":
        return "passed"
    return "blocked"


def _object_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
