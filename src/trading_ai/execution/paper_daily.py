"""Daily paper-trading operator orchestration."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_yaml_file
from trading_ai.execution.paper_close_session import PaperCloseOperationalError, run_paper_close_session
from trading_ai.execution.paper_common import (
    read_json_artifact,
    redact_secrets,
)
from trading_ai.execution.paper_common import (
    write_json_artifact as _write_json,
)
from trading_ai.execution.paper_common import (
    write_text_artifact as _write_text,
)
from trading_ai.execution.paper_execute_session import PaperExecuteOperationalError, run_paper_execute_session
from trading_ai.execution.paper_monitor import PaperMonitorOperationalError, PaperMonitorResult, run_paper_monitor
from trading_ai.execution.paper_observability import (
    PaperObservabilityReport,
    append_paper_ledger_event,
    build_paper_observability_report,
    write_paper_observability_report,
)
from trading_ai.execution.paper_session import PaperSessionResult, run_offline_paper_session

SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG_PATH = "configs/paper_daily.yml"
DEFAULT_DAILY_OUTPUT = "reports/tmp/paper_daily/latest.json"
DEFAULT_DAILY_MARKDOWN_OUTPUT = "reports/tmp/paper_daily/latest.md"
DEFAULT_SESSION_DIR = "reports/tmp/paper_session/daily/{as_of_date}"
DEFAULT_SESSIONS_ROOT = "reports/tmp/paper_session"
DEFAULT_OBSERVABILITY_OUTPUT = "reports/tmp/paper_observability/latest.json"
DEFAULT_OBSERVABILITY_MARKDOWN_OUTPUT = "reports/tmp/paper_observability/latest.md"
DEFAULT_MONITOR_OUTPUT = "reports/tmp/paper_monitor/latest.json"
DEFAULT_MONITOR_MARKDOWN_OUTPUT = "reports/tmp/paper_monitor/latest.md"


class PaperDailyOperationalError(RuntimeError):
    """Raised for malformed daily operator inputs or runtime failures."""


@dataclass(frozen=True)
class PaperDailyConfig:
    source_csv: Path
    start: str
    end: str
    as_of_date: str
    session_dir: Path
    sessions_root: Path
    output: Path
    markdown_output: Path
    observability_output: Path
    observability_markdown_output: Path
    monitor_output: Path
    monitor_markdown_output: Path
    ledger_output: Path | None
    universe_config: Path
    risk_config: Path
    signal_model: Path
    reference_features: Path | None = None
    signal_threshold: float = 0.5
    max_age_days: int = 5
    max_feature_age_days: int = 5
    backtest_report: Path | None = None
    promotion_report: Path | None = None
    reconciliation_report: Path | None = None
    review_mlflow_paper_candidate: bool = False
    mlflow_registry_dir: Path = Path("reports/registry")
    mlflow_tracking_uri: Path = Path("reports/mlruns")
    mlflow_registered_model_name: str = "approved-data-logistic-baseline"
    mlflow_alias: str = "paper-candidate"

    def redacted_dict(self) -> dict[str, object]:
        return {
            "source_csv": str(self.source_csv),
            "from": self.start,
            "to": self.end,
            "as_of_date": self.as_of_date,
            "session_dir": str(self.session_dir),
            "sessions_root": str(self.sessions_root),
            "output": str(self.output),
            "markdown_output": str(self.markdown_output),
            "observability_output": str(self.observability_output),
            "observability_markdown_output": str(self.observability_markdown_output),
            "monitor_output": str(self.monitor_output),
            "monitor_markdown_output": str(self.monitor_markdown_output),
            "ledger_output": str(self.ledger_output) if self.ledger_output is not None else None,
            "config": str(self.universe_config),
            "risk": str(self.risk_config),
            "signal_model": str(self.signal_model),
            "reference_features": str(self.reference_features) if self.reference_features is not None else None,
            "signal_threshold": self.signal_threshold,
            "max_age_days": self.max_age_days,
            "max_feature_age_days": self.max_feature_age_days,
            "backtest_report": str(self.backtest_report) if self.backtest_report is not None else None,
            "promotion_report": str(self.promotion_report) if self.promotion_report is not None else None,
            "reconciliation_report": (
                str(self.reconciliation_report) if self.reconciliation_report is not None else None
            ),
            "review_mlflow_paper_candidate": self.review_mlflow_paper_candidate,
            "mlflow_registry_dir": str(self.mlflow_registry_dir),
            "mlflow_tracking_uri": str(self.mlflow_tracking_uri),
            "mlflow_registered_model_name": self.mlflow_registered_model_name,
            "mlflow_alias": self.mlflow_alias,
        }


@dataclass(frozen=True)
class PaperDailyResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class PaperDailyFromReadinessResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def load_paper_daily_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    source_csv: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    as_of_date: str | None = None,
    session_dir: str | Path | None = None,
    sessions_root: str | Path | None = None,
    ledger_output: str | Path | None = None,
    output: str | Path | None = None,
    markdown_output: str | Path | None = None,
    observability_output: str | Path | None = None,
    observability_markdown_output: str | Path | None = None,
    monitor_output: str | Path | None = None,
    monitor_markdown_output: str | Path | None = None,
) -> PaperDailyConfig:
    config_path = Path(path)
    payload = load_yaml_file(config_path)
    daily = payload.get("paper_daily", payload)
    if not isinstance(daily, Mapping):
        raise ConfigError("paper_daily config must be a mapping")

    raw: dict[str, object] = dict(daily)
    _apply_override(raw, "source_csv", source_csv)
    _apply_override(raw, "from", start)
    _apply_override(raw, "to", end)
    _apply_override(raw, "as_of_date", as_of_date)
    _apply_override(raw, "session_dir", session_dir)
    _apply_override(raw, "sessions_root", sessions_root)
    if ledger_output is not None:
        raw["ledger_output"] = ledger_output
    _apply_override(raw, "output", output)
    _apply_override(raw, "markdown_output", markdown_output)
    _apply_override(raw, "observability_output", observability_output)
    _apply_override(raw, "observability_markdown_output", observability_markdown_output)
    _apply_override(raw, "monitor_output", monitor_output)
    _apply_override(raw, "monitor_markdown_output", monitor_markdown_output)

    resolved_as_of_date = _required_string(raw, "as_of_date", default="today")
    run_id = _daily_run_id(resolved_as_of_date)
    tokens = {"as_of_date": _token_safe_date(resolved_as_of_date), "run_id": run_id}

    return PaperDailyConfig(
        source_csv=_required_existing_path(raw, "source_csv", config_path=config_path, tokens=tokens),
        start=_date_string(_required_string(raw, "from")),
        end=_date_string(_required_string(raw, "to")),
        as_of_date=resolved_as_of_date,
        session_dir=_path_value(raw, "session_dir", DEFAULT_SESSION_DIR, config_path=config_path, tokens=tokens),
        sessions_root=_path_value(raw, "sessions_root", DEFAULT_SESSIONS_ROOT, config_path=config_path, tokens=tokens),
        output=_path_value(raw, "output", DEFAULT_DAILY_OUTPUT, config_path=config_path, tokens=tokens),
        markdown_output=_path_value(
            raw,
            "markdown_output",
            DEFAULT_DAILY_MARKDOWN_OUTPUT,
            config_path=config_path,
            tokens=tokens,
        ),
        observability_output=_path_value(
            raw,
            "observability_output",
            DEFAULT_OBSERVABILITY_OUTPUT,
            config_path=config_path,
            tokens=tokens,
        ),
        observability_markdown_output=_path_value(
            raw,
            "observability_markdown_output",
            DEFAULT_OBSERVABILITY_MARKDOWN_OUTPUT,
            config_path=config_path,
            tokens=tokens,
        ),
        monitor_output=_path_value(
            raw,
            "monitor_output",
            DEFAULT_MONITOR_OUTPUT,
            config_path=config_path,
            tokens=tokens,
        ),
        monitor_markdown_output=_path_value(
            raw,
            "monitor_markdown_output",
            DEFAULT_MONITOR_MARKDOWN_OUTPUT,
            config_path=config_path,
            tokens=tokens,
        ),
        ledger_output=_optional_path(raw, "ledger_output", config_path=config_path, tokens=tokens),
        universe_config=_required_existing_path(
            raw,
            "config",
            default="configs/universe.yml",
            config_path=config_path,
            tokens=tokens,
        ),
        risk_config=_required_existing_path(
            raw,
            "risk",
            default="configs/risk.yml",
            config_path=config_path,
            tokens=tokens,
        ),
        signal_model=_required_existing_path(
            raw,
            "signal_model",
            default="models/latest_model.json",
            config_path=config_path,
            tokens=tokens,
        ),
        reference_features=_optional_existing_path(raw, "reference_features", config_path=config_path, tokens=tokens),
        signal_threshold=_float_value(raw, "signal_threshold", 0.5),
        max_age_days=_int_value(raw, "max_age_days", 5),
        max_feature_age_days=_int_value(raw, "max_feature_age_days", 5),
        backtest_report=_optional_existing_path(raw, "backtest_report", config_path=config_path, tokens=tokens),
        promotion_report=_optional_existing_path(raw, "promotion_report", config_path=config_path, tokens=tokens),
        reconciliation_report=_optional_existing_path(
            raw,
            "reconciliation_report",
            config_path=config_path,
            tokens=tokens,
        ),
        review_mlflow_paper_candidate=bool(raw.get("review_mlflow_paper_candidate", False)),
        mlflow_registry_dir=_path_value(
            raw,
            "mlflow_registry_dir",
            "reports/registry",
            config_path=config_path,
            tokens=tokens,
        ),
        mlflow_tracking_uri=_path_value(
            raw,
            "mlflow_tracking_uri",
            "reports/mlruns",
            config_path=config_path,
            tokens=tokens,
        ),
        mlflow_registered_model_name=_string_value(
            raw,
            "mlflow_registered_model_name",
            "approved-data-logistic-baseline",
        ),
        mlflow_alias=_string_value(raw, "mlflow_alias", "paper-candidate"),
    )


def run_paper_daily(
    *,
    config: PaperDailyConfig,
    confirm_paper: bool = False,
    confirm_auto_close: bool = False,
    confirm_auto_submit: bool = False,
    send_telegram: bool = False,
    telegram_dry_run: bool = False,
    telegram_send_warnings: bool = False,
    telegram_env: Mapping[str, str] | None = None,
) -> PaperDailyResult:
    generated_at = _utc_now()
    run_id = _daily_run_id(config.as_of_date, generated_at=generated_at)
    steps: list[dict[str, object]] = []
    broker_actions: list[dict[str, object]] = []
    reasons: list[str] = []
    operational_errors: list[str] = []
    gate_blocked = False
    session_result: PaperSessionResult | None = None
    final_observability: PaperObservabilityReport | None = None
    final_monitor: PaperMonitorResult | None = None

    try:
        initial_observability = build_paper_observability_report(
            sessions_root=config.sessions_root,
            session_dirs=(),
            ledger_inputs=(),
        )
        previous_open_executions = _open_submitted_executions(initial_observability)
        steps.append(
            _step(
                "detect_previous_open_executions",
                "COMPLETED",
                count=len(previous_open_executions),
                artifacts={"sessions_root": str(config.sessions_root)},
            )
        )

        for execution in previous_open_executions:
            action = _close_previous_execution(
                execution,
                config=config,
                confirm_paper=confirm_paper,
                confirm_auto_close=confirm_auto_close,
            )
            broker_actions.append(action)
            if action["status"] == "ERROR":
                operational_errors.extend(_string_list(action.get("reasons")))
            elif action["status"] in {"PENDING", "UNMATCHED", "BLOCKED", "SKIPPED"}:
                reasons.extend(_string_list(action.get("reasons")) or [str(action["status"]).lower()])

        session_result = run_offline_paper_session(
            source_csv=config.source_csv,
            start=config.start,
            end=config.end,
            reference_features=config.reference_features,
            output_dir=config.session_dir,
            config=config.universe_config,
            risk=config.risk_config,
            signal_model=config.signal_model,
            as_of_date=config.as_of_date,
            signal_threshold=config.signal_threshold,
            max_age_days=config.max_age_days,
            max_feature_age_days=config.max_feature_age_days,
            backtest_report=config.backtest_report,
            promotion_report=config.promotion_report,
            reconciliation_report=config.reconciliation_report,
            review_mlflow_paper_candidate=config.review_mlflow_paper_candidate,
            mlflow_registry_dir=config.mlflow_registry_dir,
            mlflow_tracking_uri=config.mlflow_tracking_uri,
            mlflow_registered_model_name=config.mlflow_registered_model_name,
            mlflow_alias=config.mlflow_alias,
        )
        steps.append(
            _step(
                "paper_session",
                "READY" if session_result.ready_for_paper_review else "BLOCKED",
                exit_code=session_result.exit_code,
                artifacts={
                    "session": str(session_result.session_path),
                    "audit": str(session_result.audit_path),
                    "signal": str(session_result.signal_path),
                    "freshness": str(session_result.freshness_path),
                },
            )
        )
        if not session_result.ready_for_paper_review:
            gate_blocked = True
            reasons.append("paper_session_not_ready")

        observability_before_submit = _write_observability(config)
        steps.append(
            _step(
                "observability_before_submit",
                "COMPLETED",
                artifacts={
                    "json": str(config.observability_output),
                    "markdown": str(config.observability_markdown_output),
                },
            )
        )
        open_before_submit = _open_submitted_executions(observability_before_submit)
        monitor_before_submit = _run_monitor(config, send_telegram=False)
        steps.append(
            _step(
                "monitor_before_submit",
                monitor_before_submit.status,
                exit_code=monitor_before_submit.exit_code,
                artifacts={
                    "json": str(monitor_before_submit.output_path),
                    "markdown": str(monitor_before_submit.markdown_path),
                },
            )
        )
        if monitor_before_submit.status == "CRITICAL":
            reasons.append("monitor_before_submit_critical")
        if monitor_before_submit.exit_code == 2:
            gate_blocked = True
            operational_errors.append("monitor_before_submit_operational_error")
        if open_before_submit:
            gate_blocked = True
            reasons.append("open_execution_without_closed_closeout")

        submit_action = _submit_new_session(
            config=config,
            session_result=session_result,
            confirm_paper=confirm_paper,
            confirm_auto_submit=confirm_auto_submit,
            blocked=gate_blocked or monitor_before_submit.status == "CRITICAL",
        )
        broker_actions.append(submit_action)
        if submit_action["status"] == "ERROR":
            operational_errors.extend(_string_list(submit_action.get("reasons")))
        elif submit_action["status"] == "BLOCKED":
            gate_blocked = True
            reasons.extend(_string_list(submit_action.get("reasons")) or ["paper_submit_blocked"])

        close_new_action = _close_new_execution(
            submit_action,
            config=config,
            confirm_paper=confirm_paper,
            confirm_auto_close=confirm_auto_close,
        )
        if close_new_action is not None:
            broker_actions.append(close_new_action)
            if close_new_action["status"] == "ERROR":
                operational_errors.extend(_string_list(close_new_action.get("reasons")))
            elif close_new_action["status"] in {"PENDING", "UNMATCHED", "BLOCKED"}:
                gate_blocked = True
                reasons.extend(_string_list(close_new_action.get("reasons")) or ["paper_closeout_blocked"])

        final_observability = _write_observability(config)
        steps.append(
            _step(
                "observability_final",
                "COMPLETED",
                artifacts={
                    "json": str(config.observability_output),
                    "markdown": str(config.observability_markdown_output),
                },
            )
        )
        final_monitor = _run_monitor(
            config,
            send_telegram=send_telegram,
            telegram_dry_run=telegram_dry_run,
            telegram_send_warnings=telegram_send_warnings,
            telegram_env=telegram_env,
        )
        steps.append(
            _step(
                "monitor_final",
                final_monitor.status,
                exit_code=final_monitor.exit_code,
                artifacts={
                    "json": str(final_monitor.output_path),
                    "markdown": str(final_monitor.markdown_path),
                },
            )
        )
        if final_monitor.exit_code == 2:
            operational_errors.append("paper_monitor_operational_error")
        elif final_monitor.status == "CRITICAL":
            reasons.append("final_monitor_critical")

    except (
        ConfigError,
        OSError,
        ValueError,
        PaperCloseOperationalError,
        PaperExecuteOperationalError,
        PaperMonitorOperationalError,
    ) as exc:
        error = redact_secrets(str(exc), env=telegram_env)
        operational_errors.append(error)
        steps.append(_step("paper_daily", "ERROR", exit_code=2, reasons=[error]))
        try:
            final_observability = _write_observability(config)
            final_monitor = _run_monitor(config, send_telegram=False)
        except Exception as monitor_exc:  # pragma: no cover - defensive best effort
            operational_errors.append(f"final_monitor_unavailable: {monitor_exc}")

    status, exit_code = _final_status_and_exit_code(
        operational_errors=operational_errors,
        gate_blocked=gate_blocked,
        final_monitor=final_monitor,
    )
    if operational_errors:
        reasons.extend(operational_errors)
    reasons = _dedupe_strings(reasons)
    payload = _daily_payload(
        config=config,
        generated_at=generated_at,
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        confirm_paper=confirm_paper,
        confirm_auto_close=confirm_auto_close,
        confirm_auto_submit=confirm_auto_submit,
        steps=steps,
        broker_actions=broker_actions,
        final_monitor=final_monitor,
        final_observability=final_observability,
        session_result=session_result,
        reasons=reasons,
    )
    _write_daily_artifacts(payload, output=config.output, markdown_output=config.markdown_output)
    append_paper_ledger_event(config.ledger_output, _paper_daily_ledger_event(payload))
    return PaperDailyResult(
        exit_code=exit_code,
        status=status,
        output_path=config.output,
        markdown_path=config.markdown_output,
        payload=payload,
    )


def run_paper_daily_from_readiness(
    *,
    readiness_path: str | Path,
    confirm_readiness: bool = False,
    confirm_paper: bool = False,
    confirm_auto_close: bool = False,
    confirm_auto_submit: bool = False,
    require_clean_state: bool = False,
    output_dir: str | Path | None = None,
    ledger_output: str | Path | None = None,
) -> PaperDailyFromReadinessResult:
    """Run broker-confirmed paper daily only from an approved readiness report."""

    resolved_readiness_path = _resolve_config_path(str(readiness_path))
    broker_dir = _broker_confirmed_output_dir(resolved_readiness_path, output_dir=output_dir)
    report_output = broker_dir / "broker_run.json"
    report_markdown = broker_dir / "broker_run.md"
    confirmations = {
        "confirm_readiness": confirm_readiness,
        "confirm_paper": confirm_paper,
        "confirm_auto_close": confirm_auto_close,
        "confirm_auto_submit": confirm_auto_submit,
        "require_clean_state": require_clean_state,
    }
    broker_paths = _broker_confirmed_path_payload(broker_dir)

    missing_confirmations = _missing_readiness_confirmations(
        confirm_readiness=confirm_readiness,
        confirm_paper=confirm_paper,
        confirm_auto_close=confirm_auto_close,
        confirm_auto_submit=confirm_auto_submit,
        require_clean_state=require_clean_state,
    )
    if missing_confirmations:
        reasons = [f"missing_confirmation:{flag}" for flag in missing_confirmations]
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            paper_daily_config_path=None,
            status="ERROR",
            exit_code=2,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=reasons,
        )

    try:
        readiness = _load_readiness_payload(resolved_readiness_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            paper_daily_config_path=None,
            status="ERROR",
            exit_code=2,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=[f"invalid_readiness:{exc}"],
        )

    as_of_date = str(readiness.get("as_of_date") or "{as_of_date}")
    broker_paths = _broker_confirmed_path_payload(broker_dir, as_of_date=as_of_date)
    gate_reasons = _readiness_gate_reasons(readiness)
    if gate_reasons:
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            readiness=readiness,
            paper_daily_config_path=_readiness_config_path(readiness),
            status="BLOCKED",
            exit_code=1,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=_dedupe_strings(
                [
                    *gate_reasons,
                    *_string_list(readiness.get("reasons")),
                    *_string_list(_mapping_or_empty(readiness.get("offline_smoke")).get("reasons")),
                ]
            ),
        )

    config_path = _readiness_config_path(readiness)
    if config_path is None:
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            readiness=readiness,
            paper_daily_config_path=None,
            status="ERROR",
            exit_code=2,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=["missing_paper_daily_config_path"],
        )
    if not config_path.exists():
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            readiness=readiness,
            paper_daily_config_path=config_path,
            status="ERROR",
            exit_code=2,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=[f"paper_daily_config_missing:{config_path}"],
        )

    try:
        config = load_paper_daily_config(
            config_path,
            session_dir=broker_dir / "sessions" / "daily" / "{as_of_date}",
            sessions_root=broker_dir / "sessions",
            ledger_output=ledger_output,
            output=broker_dir / "daily.json",
            markdown_output=broker_dir / "daily.md",
            observability_output=broker_dir / "observability.json",
            observability_markdown_output=broker_dir / "observability.md",
            monitor_output=broker_dir / "monitor.json",
            monitor_markdown_output=broker_dir / "monitor.md",
        )
        broker_confirmed_config_reasons = _broker_confirmed_config_reasons(config)
        if broker_confirmed_config_reasons:
            return _write_broker_run_result(
                readiness_path=resolved_readiness_path,
                readiness=readiness,
                paper_daily_config_path=config_path,
                status="ERROR",
                exit_code=2,
                confirmations=confirmations,
                broker_paths=broker_paths,
                output_path=report_output,
                markdown_path=report_markdown,
                reasons=broker_confirmed_config_reasons,
            )
        broker_paths = _broker_confirmed_path_payload(broker_dir, as_of_date=config.as_of_date)
        result = run_paper_daily(
            config=config,
            confirm_paper=True,
            confirm_auto_close=True,
            confirm_auto_submit=True,
            send_telegram=False,
        )
    except (ConfigError, PaperDailyOperationalError, OSError, ValueError) as exc:
        return _write_broker_run_result(
            readiness_path=resolved_readiness_path,
            readiness=readiness,
            paper_daily_config_path=config_path,
            status="ERROR",
            exit_code=2,
            confirmations=confirmations,
            broker_paths=broker_paths,
            output_path=report_output,
            markdown_path=report_markdown,
            reasons=[str(exc)],
        )

    return _write_broker_run_result(
        readiness_path=resolved_readiness_path,
        readiness=readiness,
        paper_daily_config_path=config_path,
        status=result.status,
        exit_code=result.exit_code,
        confirmations=confirmations,
        broker_paths=broker_paths,
        output_path=report_output,
        markdown_path=report_markdown,
        reasons=_string_list(result.payload.get("reasons")),
        paper_daily_result=result,
    )


def _close_previous_execution(
    execution: Mapping[str, object],
    *,
    config: PaperDailyConfig,
    confirm_paper: bool,
    confirm_auto_close: bool,
) -> dict[str, object]:
    session_dir = str(execution.get("session_dir") or "")
    base = {
        "action": "close_previous_execution",
        "session_dir": session_dir,
        "client_order_id": execution.get("client_order_id"),
        "symbol": execution.get("symbol"),
        "notional": execution.get("notional"),
    }
    if not confirm_paper or not confirm_auto_close:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_auto_close:
            missing.append("--confirm-auto-close")
        return {**base, "status": "SKIPPED", "reasons": [f"missing_confirmation:{','.join(missing)}"]}
    if not session_dir:
        return {**base, "status": "ERROR", "exit_code": 2, "reasons": ["missing_session_dir"]}
    try:
        result = run_paper_close_session(
            session_dir=session_dir,
            confirm_paper=confirm_paper,
        )
    except PaperCloseOperationalError as exc:
        return {**base, "status": "ERROR", "exit_code": 2, "reasons": [redact_secrets(str(exc))]}
    return {
        **base,
        "status": result.status,
        "exit_code": result.exit_code,
        "artifacts": {
            "json": str(result.json_path) if result.json_path is not None else None,
            "markdown": str(result.markdown_path) if result.markdown_path is not None else None,
        },
        "reasons": list(result.reasons),
    }


def _submit_new_session(
    *,
    config: PaperDailyConfig,
    session_result: PaperSessionResult | None,
    confirm_paper: bool,
    confirm_auto_submit: bool,
    blocked: bool,
) -> dict[str, object]:
    base = {
        "action": "submit_new_session",
        "session_dir": str(config.session_dir),
    }
    if session_result is None:
        return {**base, "status": "SKIPPED", "reasons": ["paper_session_unavailable"]}
    if blocked:
        return {**base, "status": "SKIPPED", "reasons": ["paper_gate_blocked"]}
    if not session_result.ready_for_paper_review:
        return {**base, "status": "SKIPPED", "reasons": ["paper_session_not_ready"]}
    if not confirm_paper or not confirm_auto_submit:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_auto_submit:
            missing.append("--confirm-auto-submit")
        return {**base, "status": "SKIPPED", "reasons": [f"missing_confirmation:{','.join(missing)}"]}
    try:
        result = run_paper_execute_session(
            session_dir=config.session_dir,
            confirm_paper=confirm_paper,
            confirm_submit=confirm_auto_submit,
            as_of_date=config.as_of_date,
            max_feature_age_days=config.max_feature_age_days,
        )
    except Exception as exc:
        return {**base, "status": "ERROR", "exit_code": 2, "reasons": [redact_secrets(str(exc))]}
    return {
        **base,
        "status": result.status,
        "exit_code": result.exit_code,
        "artifacts": {
            "json": str(result.json_path) if result.json_path is not None else None,
            "markdown": str(result.markdown_path) if result.markdown_path is not None else None,
        },
        "reasons": list(result.reasons),
    }


def _close_new_execution(
    submit_action: Mapping[str, object],
    *,
    config: PaperDailyConfig,
    confirm_paper: bool,
    confirm_auto_close: bool,
) -> dict[str, object] | None:
    base = {
        "action": "close_new_execution",
        "session_dir": str(config.session_dir),
    }
    if submit_action.get("status") != "SUBMITTED":
        return None
    if not confirm_paper or not confirm_auto_close:
        missing = []
        if not confirm_paper:
            missing.append("--confirm-paper")
        if not confirm_auto_close:
            missing.append("--confirm-auto-close")
        return {**base, "status": "SKIPPED", "reasons": [f"missing_confirmation:{','.join(missing)}"]}
    try:
        result = run_paper_close_session(
            session_dir=config.session_dir,
            confirm_paper=confirm_paper,
        )
    except PaperCloseOperationalError as exc:
        return {**base, "status": "ERROR", "exit_code": 2, "reasons": [redact_secrets(str(exc))]}
    return {
        **base,
        "status": result.status,
        "exit_code": result.exit_code,
        "artifacts": {
            "json": str(result.json_path) if result.json_path is not None else None,
            "markdown": str(result.markdown_path) if result.markdown_path is not None else None,
        },
        "reasons": list(result.reasons),
    }


def _write_observability(config: PaperDailyConfig) -> PaperObservabilityReport:
    report = build_paper_observability_report(
        sessions_root=config.sessions_root,
        session_dirs=[config.session_dir],
        ledger_inputs=(),
    )
    write_paper_observability_report(
        report,
        output=config.observability_output,
        markdown_output=config.observability_markdown_output,
    )
    return report


def _run_monitor(
    config: PaperDailyConfig,
    *,
    send_telegram: bool,
    telegram_dry_run: bool = False,
    telegram_send_warnings: bool = False,
    telegram_env: Mapping[str, str] | None = None,
) -> PaperMonitorResult:
    return run_paper_monitor(
        sessions_root=config.sessions_root,
        session_dirs=[config.session_dir],
        ledger_inputs=(),
        output=config.monitor_output,
        markdown_output=config.monitor_markdown_output,
        as_of_date=config.as_of_date,
        ledger_output=None,
        send_telegram=send_telegram,
        telegram_dry_run=telegram_dry_run,
        telegram_send_warnings=telegram_send_warnings,
        env=telegram_env,
    )


def _open_submitted_executions(report: PaperObservabilityReport) -> list[dict[str, object]]:
    events = list(report.events)
    closeouts = [
        event
        for event in events
        if event.get("event_type") == "paper_closeout" and str(event.get("status") or "").upper() == "CLOSED"
    ]
    result: list[dict[str, object]] = []
    for event in events:
        if event.get("event_type") != "paper_execution":
            continue
        if str(event.get("status") or "").upper() != "SUBMITTED":
            continue
        if _has_matching_event(event, closeouts):
            continue
        result.append(event)
    return result


def _has_matching_event(event: Mapping[str, object], candidates: Iterable[Mapping[str, object]]) -> bool:
    session_dir = event.get("session_dir")
    client_order_id = event.get("client_order_id")
    for candidate in candidates:
        if session_dir and candidate.get("session_dir") == session_dir:
            return True
        if client_order_id and candidate.get("client_order_id") == client_order_id:
            return True
    return False


def _daily_payload(
    *,
    config: PaperDailyConfig,
    generated_at: str,
    run_id: str,
    status: str,
    exit_code: int,
    confirm_paper: bool,
    confirm_auto_close: bool,
    confirm_auto_submit: bool,
    steps: list[dict[str, object]],
    broker_actions: list[dict[str, object]],
    final_monitor: PaperMonitorResult | None,
    final_observability: PaperObservabilityReport | None,
    session_result: PaperSessionResult | None,
    reasons: list[str],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": config.as_of_date,
        "status": status,
        "exit_code": exit_code,
        "run_id": run_id,
        "mode": "alpaca-paper-only",
        "config": config.redacted_dict(),
        "confirmations": {
            "confirm_paper": confirm_paper,
            "confirm_auto_close": confirm_auto_close,
            "confirm_auto_submit": confirm_auto_submit,
        },
        "steps": steps,
        "broker_actions": broker_actions,
        "final_monitor": _monitor_summary(final_monitor),
        "artifacts": {
            "daily_json": str(config.output),
            "daily_markdown": str(config.markdown_output),
            "session_dir": str(session_result.output_dir) if session_result is not None else str(config.session_dir),
            "session_json": str(session_result.session_path) if session_result is not None else None,
            "observability_json": str(config.observability_output),
            "observability_markdown": str(config.observability_markdown_output),
            "monitor_json": str(final_monitor.output_path) if final_monitor is not None else str(config.monitor_output),
            "monitor_markdown": (
                str(final_monitor.markdown_path) if final_monitor is not None else str(config.monitor_markdown_output)
            ),
        },
        "final_observability": (
            {
                "generated_at": final_observability.generated_at,
                "summary": dict(final_observability.summary),
            }
            if final_observability is not None
            else None
        ),
        "reasons": reasons,
    }


def _monitor_summary(result: PaperMonitorResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    dashboard = result.dashboard
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "output": str(result.output_path),
        "markdown_output": str(result.markdown_path),
        "monitor_summary": dashboard.get("monitor_summary"),
        "alerts": dashboard.get("alerts"),
        "telegram": dashboard.get("telegram"),
    }


def _write_daily_artifacts(
    payload: Mapping[str, object],
    *,
    output: Path,
    markdown_output: Path,
) -> None:
    _write_json(payload, output)
    _write_text(render_paper_daily_markdown(payload), markdown_output)


def render_paper_daily_markdown(payload: Mapping[str, object]) -> str:
    final_monitor = _mapping_or_empty(payload.get("final_monitor"))
    monitor_summary = _mapping_or_empty(final_monitor.get("monitor_summary"))
    steps = _object_list(payload.get("steps"))
    broker_actions = _object_list(payload.get("broker_actions"))
    alerts = _object_list(final_monitor.get("alerts"))
    reasons = _string_list(payload.get("reasons"))
    lines = [
        "# Paper Daily Operator",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        "",
        f"Run ID: `{payload.get('run_id') or ''}`",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Exit code: `{payload.get('exit_code')}`",
        f"Final monitor: `{final_monitor.get('status') or ''}`",
        f"Action: `{monitor_summary.get('action_required') or ''}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Exit |",
        "| --- | --- | ---: |",
    ]
    if steps:
        for step in steps:
            if isinstance(step, Mapping):
                lines.append(
                    "| "
                    f"`{_escape_markdown(step.get('name') or '')}` "
                    f"| `{_escape_markdown(step.get('status') or '')}` "
                    f"| `{_escape_markdown(step.get('exit_code') if 'exit_code' in step else '')}` |"
                )
    else:
        lines.append("| none |  |  |")
    lines.extend(
        [
            "",
            "## Broker Paper Actions",
            "",
            "| Action | Status | Session | Reasons |",
            "| --- | --- | --- | --- |",
        ]
    )
    if broker_actions:
        for action in broker_actions:
            if isinstance(action, Mapping):
                lines.append(
                    "| "
                    f"`{_escape_markdown(action.get('action') or '')}` "
                    f"| `{_escape_markdown(action.get('status') or '')}` "
                    f"| `{_escape_markdown(action.get('session_dir') or '')}` "
                    f"| `{_escape_markdown(', '.join(_string_list(action.get('reasons'))))}` |"
                )
    else:
        lines.append("| none |  |  |  |")
    lines.extend(
        [
            "",
            "## Final Alerts",
            "",
            "| Severity | Code | Message |",
            "| --- | --- | --- |",
        ]
    )
    if alerts:
        for alert in alerts:
            if isinstance(alert, Mapping):
                lines.append(
                    "| "
                    f"`{_escape_markdown(alert.get('severity') or '')}` "
                    f"| `{_escape_markdown(alert.get('code') or '')}` "
                    f"| {_escape_markdown(alert.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | No final monitor alerts. |")
    lines.extend(
        [
            "",
            "## Action Criteria",
            "",
            "- `OK` or `WARN` without blocked paper gates: continue the paper-only daily flow.",
            "- `CRITICAL`: stop new paper submissions until evidence gaps or closeouts are resolved.",
            "- `ERROR`: fix the operational failure before rerunning.",
            "",
        ]
    )
    if reasons:
        lines.extend(["## Reasons", ""])
        lines.extend(f"- `{_escape_markdown(reason)}`" for reason in reasons)
        lines.append("")
    return "\n".join(lines)


def render_paper_daily_from_readiness_markdown(payload: Mapping[str, object]) -> str:
    confirmations = _mapping_or_empty(payload.get("confirmations"))
    broker_paths = _mapping_or_empty(payload.get("broker_confirmed_paths"))
    paper_daily = _mapping_or_empty(payload.get("paper_daily"))
    reasons = _string_list(payload.get("reasons"))
    lines = [
        "# Paper Daily Broker-Confirmed From Readiness",
        "",
        f"Status: **{payload.get('status') or 'UNKNOWN'}**",
        "",
        f"Exit code: `{payload.get('exit_code')}`",
        f"Readiness: `{payload.get('readiness_path') or ''}`",
        f"Generated config: `{payload.get('paper_daily_config_path') or ''}`",
        f"Output dir: `{broker_paths.get('output_dir') or ''}`",
        "",
        "## Confirmations",
        "",
        "| Confirmation | Value |",
        "| --- | --- |",
    ]
    for name in (
        "confirm_readiness",
        "confirm_paper",
        "confirm_auto_close",
        "confirm_auto_submit",
        "require_clean_state",
    ):
        lines.append(f"| `{name}` | `{confirmations.get(name) is True}` |")
    lines.extend(
        [
            "",
            "## Broker-Confirmed Paths",
            "",
            "| Artifact | Path |",
            "| --- | --- |",
        ]
    )
    for name, path in sorted(broker_paths.items()):
        lines.append(f"| `{_escape_markdown(name)}` | `{_escape_markdown(path)}` |")
    if paper_daily:
        lines.extend(
            [
                "",
                "## Paper Daily Summary",
                "",
                f"- Status: `{paper_daily.get('status') or ''}`",
                f"- Exit code: `{paper_daily.get('exit_code')}`",
                f"- Run ID: `{paper_daily.get('run_id') or ''}`",
                f"- Report: `{paper_daily.get('output_path') or ''}`",
                f"- Markdown: `{paper_daily.get('markdown_path') or ''}`",
            ]
        )
    if reasons:
        lines.extend(["", "## Reasons", ""])
        lines.extend(f"- `{_escape_markdown(reason)}`" for reason in reasons)
    lines.append("")
    return "\n".join(lines)


def _write_broker_run_result(
    *,
    readiness_path: Path,
    status: str,
    exit_code: int,
    confirmations: Mapping[str, object],
    broker_paths: Mapping[str, object],
    output_path: Path,
    markdown_path: Path,
    reasons: Iterable[object],
    readiness: Mapping[str, object] | None = None,
    paper_daily_config_path: Path | None = None,
    paper_daily_result: PaperDailyResult | None = None,
) -> PaperDailyFromReadinessResult:
    payload = _broker_run_payload(
        readiness_path=readiness_path,
        readiness=readiness,
        paper_daily_config_path=paper_daily_config_path,
        status=status,
        exit_code=exit_code,
        confirmations=confirmations,
        broker_paths=broker_paths,
        output_path=output_path,
        markdown_path=markdown_path,
        reasons=reasons,
        paper_daily_result=paper_daily_result,
    )
    _write_json(payload, output_path)
    _write_text(render_paper_daily_from_readiness_markdown(payload), markdown_path)
    return PaperDailyFromReadinessResult(
        exit_code=exit_code,
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def _broker_run_payload(
    *,
    readiness_path: Path,
    readiness: Mapping[str, object] | None,
    paper_daily_config_path: Path | None,
    status: str,
    exit_code: int,
    confirmations: Mapping[str, object],
    broker_paths: Mapping[str, object],
    output_path: Path,
    markdown_path: Path,
    reasons: Iterable[object],
    paper_daily_result: PaperDailyResult | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "mode": "alpaca-paper-only",
        "readiness_path": str(readiness_path),
        "paper_daily_config_path": str(paper_daily_config_path) if paper_daily_config_path is not None else None,
        "status": status,
        "exit_code": exit_code,
        "confirmations": dict(confirmations),
        "broker_confirmed_paths": dict(broker_paths),
        "artifacts": {
            "broker_run_json": str(output_path),
            "broker_run_markdown": str(markdown_path),
            "daily_json": broker_paths.get("daily_json"),
            "daily_markdown": broker_paths.get("daily_markdown"),
            "session_dir": broker_paths.get("session_dir"),
            "observability_json": broker_paths.get("observability_json"),
            "observability_markdown": broker_paths.get("observability_markdown"),
            "monitor_json": broker_paths.get("monitor_json"),
            "monitor_markdown": broker_paths.get("monitor_markdown"),
        },
        "readiness": _readiness_summary(readiness),
        "paper_daily": _paper_daily_result_summary(paper_daily_result),
        "reasons": _dedupe_strings(reasons),
        "safety": {
            "paper_only": True,
            "live_trading": False,
            "send_telegram": False,
            "requires_readiness_ready": True,
            "requires_offline_smoke": True,
            "requires_clean_state_confirmation": True,
            "preserves_offline_smoke_artifacts": True,
        },
    }
    return payload


def _readiness_summary(readiness: Mapping[str, object] | None) -> dict[str, object] | None:
    if readiness is None:
        return None
    offline_smoke = _mapping_or_empty(readiness.get("offline_smoke"))
    return {
        "status": readiness.get("status"),
        "ready_for_paper_daily": readiness.get("ready_for_paper_daily"),
        "exit_code": readiness.get("exit_code"),
        "as_of_date": readiness.get("as_of_date"),
        "offline_smoke": {
            "requested": offline_smoke.get("requested"),
            "ran": offline_smoke.get("ran"),
            "status": offline_smoke.get("status"),
            "exit_code": offline_smoke.get("exit_code"),
            "config_path": offline_smoke.get("config_path"),
            "artifacts": dict(_mapping_or_empty(offline_smoke.get("artifacts"))),
        },
        "reasons": _string_list(readiness.get("reasons")),
    }


def _paper_daily_result_summary(result: PaperDailyResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    payload = result.payload
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "output_path": str(result.output_path),
        "markdown_path": str(result.markdown_path),
        "run_id": payload.get("run_id"),
        "final_monitor": _monitor_summary_from_payload(payload),
        "artifacts": dict(_mapping_or_empty(payload.get("artifacts"))),
        "broker_actions": [
            {
                "action": action.get("action"),
                "status": action.get("status"),
                "session_dir": action.get("session_dir"),
                "reasons": _string_list(action.get("reasons")),
            }
            for action in _object_list(payload.get("broker_actions"))
            if isinstance(action, Mapping)
        ],
        "reasons": _string_list(payload.get("reasons")),
    }


def _monitor_summary_from_payload(payload: Mapping[str, object]) -> dict[str, object] | None:
    final_monitor = _mapping_or_empty(payload.get("final_monitor"))
    if not final_monitor:
        return None
    return {
        "status": final_monitor.get("status"),
        "exit_code": final_monitor.get("exit_code"),
        "output": final_monitor.get("output"),
        "markdown_output": final_monitor.get("markdown_output"),
    }


def _load_readiness_payload(path: Path) -> dict[str, object]:
    return read_json_artifact(path)


def _readiness_gate_reasons(readiness: Mapping[str, object]) -> list[str]:
    offline_smoke = _mapping_or_empty(readiness.get("offline_smoke"))
    reasons: list[str] = []
    if readiness.get("status") != "READY":
        reasons.append("readiness_status_not_ready")
    if readiness.get("ready_for_paper_daily") is not True:
        reasons.append("readiness_not_ready_for_paper_daily")
    if _int_or_none(readiness.get("exit_code")) != 0:
        reasons.append("readiness_exit_code_not_zero")
    if offline_smoke.get("requested") is not True:
        reasons.append("offline_smoke_not_requested")
    if offline_smoke.get("ran") is not True:
        reasons.append("offline_smoke_not_ran")
    if _int_or_none(offline_smoke.get("exit_code")) != 0:
        reasons.append("offline_smoke_exit_code_not_zero")
    return _dedupe_strings(reasons)


def _broker_confirmed_config_reasons(config: PaperDailyConfig) -> list[str]:
    reasons: list[str] = []
    if config.as_of_date == "today":
        reasons.append("broker_confirmed_as_of_date_must_be_explicit")
    if config.start == "today":
        reasons.append("broker_confirmed_from_must_be_explicit")
    if config.end == "today":
        reasons.append("broker_confirmed_to_must_be_explicit")
    return reasons


def _readiness_config_path(readiness: Mapping[str, object]) -> Path | None:
    value = readiness.get("paper_daily_config_path")
    if value in {None, ""}:
        return None
    return _resolve_config_path(str(value))


def _missing_readiness_confirmations(
    *,
    confirm_readiness: bool,
    confirm_paper: bool,
    confirm_auto_close: bool,
    confirm_auto_submit: bool,
    require_clean_state: bool,
) -> list[str]:
    missing = []
    if not confirm_readiness:
        missing.append("--confirm-readiness")
    if not confirm_paper:
        missing.append("--confirm-paper")
    if not confirm_auto_close:
        missing.append("--confirm-auto-close")
    if not confirm_auto_submit:
        missing.append("--confirm-auto-submit")
    if not require_clean_state:
        missing.append("--require-clean-state")
    return missing


def _broker_confirmed_output_dir(readiness_path: Path, *, output_dir: str | Path | None) -> Path:
    if output_dir is not None:
        return _resolve_config_path(str(output_dir))
    return readiness_path.parent / "paper_daily" / "broker_confirmed"


def _broker_confirmed_path_payload(
    broker_dir: Path,
    *,
    as_of_date: str | None = None,
) -> dict[str, object]:
    date_token = as_of_date or "{as_of_date}"
    sessions_root = broker_dir / "sessions"
    session_dir = sessions_root / "daily" / date_token
    return {
        "output_dir": str(broker_dir),
        "daily_json": str(broker_dir / "daily.json"),
        "daily_markdown": str(broker_dir / "daily.md"),
        "sessions_root": str(sessions_root),
        "session_dir": str(session_dir),
        "observability_json": str(broker_dir / "observability.json"),
        "observability_markdown": str(broker_dir / "observability.md"),
        "monitor_json": str(broker_dir / "monitor.json"),
        "monitor_markdown": str(broker_dir / "monitor.md"),
    }


def _paper_daily_ledger_event(payload: Mapping[str, object]) -> dict[str, object]:
    artifacts = _mapping_or_empty(payload.get("artifacts"))
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": "paper_daily",
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "exit_code": payload.get("exit_code"),
        "session_dir": artifacts.get("session_dir"),
        "output_path": artifacts.get("daily_json"),
        "reasons": _string_list(payload.get("reasons")),
    }


def _final_status_and_exit_code(
    *,
    operational_errors: list[str],
    gate_blocked: bool,
    final_monitor: PaperMonitorResult | None,
) -> tuple[str, int]:
    if operational_errors:
        return "ERROR", 2
    if final_monitor is not None and final_monitor.status == "CRITICAL":
        return "CRITICAL", 1
    if gate_blocked:
        return "BLOCKED", 1
    if final_monitor is not None and final_monitor.status == "WARN":
        return "WARN", 0
    return "OK", 0


def _step(
    name: str,
    status: str,
    *,
    exit_code: int | None = None,
    count: int | None = None,
    artifacts: Mapping[str, object] | None = None,
    reasons: Iterable[object] = (),
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "status": status}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if count is not None:
        payload["count"] = count
    if artifacts is not None:
        payload["artifacts"] = dict(artifacts)
    reason_list = _dedupe_strings(reasons)
    if reason_list:
        payload["reasons"] = reason_list
    return payload


def _apply_override(raw: dict[str, object], key: str, value: object) -> None:
    if value is not None:
        raw[key] = value


def _required_string(raw: Mapping[str, object], key: str, default: str | None = None) -> str:
    value = raw.get(key, default)
    if value in {None, ""}:
        raise ConfigError(f"paper_daily.{key} is required")
    return str(value)


def _string_value(raw: Mapping[str, object], key: str, default: str) -> str:
    value = raw.get(key, default)
    if value in {None, ""}:
        return default
    return str(value)


def _path_value(
    raw: Mapping[str, object],
    key: str,
    default: str,
    *,
    config_path: Path,
    tokens: Mapping[str, str],
) -> Path:
    value = raw.get(key, default)
    if value in {None, ""}:
        value = default
    return _resolve_config_path(str(value).format(**tokens))


def _optional_path(
    raw: Mapping[str, object],
    key: str,
    *,
    config_path: Path,
    tokens: Mapping[str, str],
) -> Path | None:
    value = raw.get(key)
    if value in {None, ""}:
        return None
    return _resolve_config_path(str(value).format(**tokens))


def _required_existing_path(
    raw: Mapping[str, object],
    key: str,
    *,
    config_path: Path,
    tokens: Mapping[str, str],
    default: str | None = None,
) -> Path:
    value = raw.get(key, default)
    if value in {None, ""}:
        raise ConfigError(f"paper_daily.{key} is required")
    path = _resolve_config_path(str(value).format(**tokens))
    if not path.exists():
        raise ConfigError(f"paper_daily.{key} does not exist: {path}")
    return path


def _optional_existing_path(
    raw: Mapping[str, object],
    key: str,
    *,
    config_path: Path,
    tokens: Mapping[str, str],
) -> Path | None:
    value = raw.get(key)
    if value in {None, ""}:
        return None
    path = _resolve_config_path(str(value).format(**tokens))
    if not path.exists():
        raise ConfigError(f"paper_daily.{key} does not exist: {path}")
    return path


def _resolve_config_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _float_value(raw: Mapping[str, object], key: str, default: float) -> float:
    try:
        return float(str(raw.get(key, default)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"paper_daily.{key} must be a number") from exc


def _int_value(raw: Mapping[str, object], key: str, default: int) -> int:
    try:
        value = int(float(str(raw.get(key, default))))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"paper_daily.{key} must be an integer") from exc
    if value < 0:
        raise ConfigError(f"paper_daily.{key} must be non-negative")
    return value


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _daily_run_id(as_of_date: str, *, generated_at: str | None = None) -> str:
    generated = generated_at or _utc_now()
    compact = "".join(character for character in generated if character.isdigit())
    return f"paper-daily-{_token_safe_date(as_of_date)}-{compact[:14]}"


def _date_string(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    return value


def _token_safe_date(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    return value.replace("/", "-").replace(":", "-")


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in {None, ""}]
    if value in {None, ""}:
        return []
    return [str(value)]


def _dedupe_strings(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in {None, ""}:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
