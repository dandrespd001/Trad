"""Offline audit journal for paper signal-order sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class PaperAuditFinding:
    severity: str
    code: str
    message: str
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }


@dataclass(frozen=True)
class PaperAuditReport:
    schema_version: str
    generated_at: str
    ready_for_paper_review: bool
    findings: tuple[PaperAuditFinding, ...]
    sources: dict[str, str]
    summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "ready_for_paper_review": self.ready_for_paper_review,
            "findings": [finding.to_dict() for finding in self.findings],
            "sources": dict(self.sources),
            "summary": dict(self.summary),
        }


def evaluate_paper_audit(
    *,
    freshness_report: Mapping[str, object],
    signal_report: Mapping[str, object],
    reconciliation_report: Mapping[str, object] | None = None,
    backtest_report: Mapping[str, object] | None = None,
    promotion_report: Mapping[str, object] | None = None,
    drift_report: Mapping[str, object] | None = None,
    mlflow_candidate_review_report: Mapping[str, object] | None = None,
    sources: Mapping[str, str] | None = None,
    as_of_date: str | None = None,
    generated_at: str | None = None,
) -> PaperAuditReport:
    findings: list[PaperAuditFinding] = []

    freshness_allowed = freshness_report.get("allowed") is True
    if not freshness_allowed:
        reasons = _string_list(freshness_report.get("reasons"))
        suffix = f": {', '.join(reasons)}" if reasons else ""
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="freshness_blocked",
                message=f"Freshness gate did not allow the paper session{suffix}.",
                source="freshness_report",
            )
        )

    preflight = _mapping_or_none(signal_report.get("preflight"))
    preflight_allowed = preflight.get("allowed") is True if preflight is not None else None
    if preflight is None:
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="missing_preflight",
                message="Signal report does not contain a preflight decision.",
                source="signal_report",
            )
        )
    elif not preflight_allowed:
        reasons = _string_list(preflight.get("reasons"))
        suffix = f": {', '.join(reasons)}" if reasons else ""
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="preflight_blocked",
                message=f"Paper preflight did not allow submission{suffix}.",
                source="signal_report",
            )
        )

    selected_signal = _mapping_or_none(signal_report.get("selected_signal"))
    signal_action = str(selected_signal.get("action", "")).lower() if selected_signal is not None else ""
    selected_symbol = str(selected_signal.get("symbol", "")).upper() if selected_signal is not None else None
    signal_timestamp = str(selected_signal.get("timestamp", "")) if selected_signal is not None else None
    if selected_signal is None or signal_action != "buy":
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="no_buy_signal",
                message="Signal report does not contain a selected buy signal.",
                source="signal_report",
            )
        )

    order_intent = _mapping_or_none(signal_report.get("order_intent"))
    submitted = signal_report.get("submitted") is True
    if order_intent is not None and not submitted:
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="order_not_submitted",
                message="Signal report contains an order intent but submitted is false.",
                source="signal_report",
            )
        )

    order_result = _mapping_or_none(signal_report.get("order_result"))
    order_accepted = order_result.get("accepted") is True if order_result is not None else None
    if order_result is not None and not order_accepted:
        reasons = _string_list(order_result.get("reasons"))
        suffix = f": {', '.join(reasons)}" if reasons else ""
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="order_rejected",
                message=f"Order result was not accepted{suffix}.",
                source="signal_report",
            )
        )

    reconciliation = _reconciliation_status(reconciliation_report)
    if reconciliation_report is not None and reconciliation.get("matched") is False:
        differences = _string_list(reconciliation.get("differences"))
        suffix = f": {', '.join(differences)}" if differences else ""
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="reconciliation_unmatched",
                message=f"Optional reconciliation report did not match{suffix}.",
                source="reconciliation_report",
            )
        )

    promotion_approved = promotion_report.get("approved") is True if promotion_report is not None else None
    if promotion_report is None:
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="promotion_missing",
                message="Promotion report was not provided.",
                source="promotion_report",
            )
        )
    elif not promotion_approved:
        reasons = _string_list(promotion_report.get("reasons"))
        suffix = f": {', '.join(reasons)}" if reasons else ""
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="promotion_not_approved",
                message=f"Promotion report is not approved{suffix}.",
                source="promotion_report",
            )
        )

    drift_detected = drift_report.get("drift_detected") is True if drift_report is not None else None
    drifted_feature_count = _drifted_feature_count(drift_report)
    drift_warning_count = _drift_warning_count(drift_report)
    if drift_report is None:
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="drift_report_missing",
                message="Feature drift report was not provided.",
                source="drift_report",
            )
        )
    elif drift_detected:
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="feature_drift_detected",
                message=f"Feature drift report detected drift in {drifted_feature_count} feature(s).",
                source="drift_report",
            )
        )

    metrics = _mapping_or_none(backtest_report.get("metrics")) if backtest_report is not None else None
    backtest_metrics_available = bool(metrics)
    if backtest_report is None:
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="backtest_missing",
                message="Backtest report was not provided.",
                source="backtest_report",
            )
        )
    elif not backtest_metrics_available:
        findings.append(
            PaperAuditFinding(
                severity="warn",
                code="backtest_metrics_missing",
                message="Backtest report does not contain metrics.",
                source="backtest_report",
            )
        )

    mlflow_candidate_review_passed = _mlflow_candidate_review_passed(mlflow_candidate_review_report)
    if mlflow_candidate_review_report is not None and not mlflow_candidate_review_passed:
        reasons = _string_list(mlflow_candidate_review_report.get("failures"))
        if not reasons:
            status = mlflow_candidate_review_report.get("status")
            reasons = [f"status={status or '<missing>'}"]
        findings.append(
            PaperAuditFinding(
                severity="fail",
                code="mlflow_candidate_review_failed",
                message="MLflow paper-candidate review did not pass: " + ", ".join(reasons),
                source="mlflow_candidate_review_report",
            )
        )

    findings.extend(
        _info_findings(
            freshness_report=freshness_report,
            signal_report=signal_report,
            reconciliation_report=reconciliation_report,
            selected_symbol=selected_symbol,
            signal_timestamp=signal_timestamp,
        )
    )

    fail_count = _count(findings, "fail")
    warn_count = _count(findings, "warn")
    info_count = _count(findings, "info")
    summary: dict[str, object] = {
        "as_of_date": as_of_date,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "info_count": info_count,
        "freshness_allowed": freshness_allowed,
        "preflight_allowed": preflight_allowed,
        "broker": signal_report.get("broker"),
        "mode": signal_report.get("mode"),
        "selected_symbol": selected_symbol,
        "signal_timestamp": signal_timestamp,
        "signal_action": signal_action or None,
        "submitted": submitted,
        "order_accepted": order_accepted,
        "reconciliation_matched": reconciliation.get("matched") if reconciliation_report is not None else None,
        "promotion_approved": promotion_approved,
        "backtest_metrics_available": backtest_metrics_available,
        "drift_detected": drift_detected,
        "drifted_feature_count": drifted_feature_count,
        "drift_warning_count": drift_warning_count,
    }
    if mlflow_candidate_review_report is not None:
        summary.update(
            {
                "mlflow_candidate_review_passed": mlflow_candidate_review_passed,
                "mlflow_registry_run_id": _non_empty_string_or_none(
                    mlflow_candidate_review_report.get("registry_run_id")
                ),
                "mlflow_model_version": _non_empty_string_or_none(mlflow_candidate_review_report.get("model_version")),
                "mlflow_alias": _non_empty_string_or_none(mlflow_candidate_review_report.get("alias")),
            }
        )
    return PaperAuditReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        ready_for_paper_review=fail_count == 0,
        findings=tuple(findings),
        sources={str(key): str(value) for key, value in (sources or {}).items()},
        summary=summary,
    )


def render_paper_audit_markdown(
    report: PaperAuditReport,
    *,
    freshness_report: Mapping[str, object],
    signal_report: Mapping[str, object],
) -> str:
    status = "READY" if report.ready_for_paper_review else "BLOCKED"
    selected_signal = _mapping_or_none(signal_report.get("selected_signal")) or {}
    order_intent = _mapping_or_none(signal_report.get("order_intent")) or {}
    order_result = _mapping_or_none(signal_report.get("order_result")) or {}
    preflight = _mapping_or_none(signal_report.get("preflight")) or {}

    lines = [
        "# Paper Session Audit",
        "",
        f"Status: **{status}**",
        "",
        f"Generated at: `{report.generated_at}`",
        f"As of date: `{report.summary.get('as_of_date') or ''}`",
        (
            "Findings: "
            f"{report.summary.get('fail_count', 0)} fail, "
            f"{report.summary.get('warn_count', 0)} warn, "
            f"{report.summary.get('info_count', 0)} info"
        ),
        "",
        "## Findings",
        "",
        "| Severity | Code | Message | Source |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(_markdown_finding_row(finding) for finding in report.findings)
    lines.extend(
        [
            "",
            "## Sources",
            "",
            "| Source | Path |",
            "| --- | --- |",
        ]
    )
    if report.sources:
        for name, path in sorted(report.sources.items()):
            lines.append(f"| `{_escape_markdown(name)}` | `{_escape_markdown(path)}` |")
    else:
        lines.append("| none |  |")

    lines.extend(
        [
            "",
            "## Signal / Order / Preflight",
            "",
            f"- Broker: `{signal_report.get('broker') or ''}`",
            f"- Mode: `{signal_report.get('mode') or ''}`",
            f"- Selected symbol: `{selected_signal.get('symbol') or ''}`",
            f"- Signal timestamp: `{selected_signal.get('timestamp') or ''}`",
            f"- Signal action: `{selected_signal.get('action') or ''}`",
            f"- Preflight allowed: `{preflight.get('allowed')}`",
            f"- Preflight reasons: `{', '.join(_string_list(preflight.get('reasons')))}`",
            f"- Submitted: `{signal_report.get('submitted')}`",
            f"- Order accepted: `{order_result.get('accepted')}`",
            f"- Client order ID: `{order_intent.get('client_order_id') or ''}`",
            f"- Order notional: `{order_intent.get('notional') or ''}`",
            "",
            "## Freshness",
            "",
            f"- Freshness allowed: `{freshness_report.get('allowed')}`",
            f"- Freshness reasons: `{', '.join(_string_list(freshness_report.get('reasons')))}`",
        ]
    )
    for symbol, detail in sorted(_freshness_symbols(freshness_report).items()):
        lines.append(
            "- "
            f"{symbol}: `{detail.get('status') or ''}` "
            f"timestamp=`{detail.get('timestamp') or ''}` "
            f"age_days=`{detail.get('age_days')}`"
        )
    lines.extend(
        [
            "",
            "## Drift",
            "",
            f"- Drift detected: `{report.summary.get('drift_detected')}`",
            f"- Drifted features: `{report.summary.get('drifted_feature_count')}`",
            f"- Drift warnings: `{report.summary.get('drift_warning_count')}`",
        ]
    )
    if "mlflow_candidate_review_passed" in report.summary:
        lines.extend(
            [
                "",
                "## MLflow Paper Candidate",
                "",
                f"- Review passed: `{report.summary.get('mlflow_candidate_review_passed')}`",
                f"- Registry run: `{report.summary.get('mlflow_registry_run_id') or ''}`",
                f"- Model version: `{report.summary.get('mlflow_model_version') or ''}`",
                f"- Alias: `{report.summary.get('mlflow_alias') or ''}`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _info_findings(
    *,
    freshness_report: Mapping[str, object],
    signal_report: Mapping[str, object],
    reconciliation_report: Mapping[str, object] | None,
    selected_symbol: str | None,
    signal_timestamp: str | None,
) -> tuple[PaperAuditFinding, ...]:
    findings = [
        PaperAuditFinding(
            severity="info",
            code="paper_mode",
            message=(
                f"Broker {signal_report.get('broker') or 'unknown'} "
                f"in {signal_report.get('mode') or 'unknown'} mode."
            ),
            source="signal_report",
        )
    ]
    if selected_symbol or signal_timestamp:
        findings.append(
            PaperAuditFinding(
                severity="info",
                code="selected_signal",
                message=f"Selected signal {selected_symbol or 'unknown'} at {signal_timestamp or 'unknown'}.",
                source="signal_report",
            )
        )
    freshness_summary = _freshness_summary(freshness_report)
    if freshness_summary:
        findings.append(
            PaperAuditFinding(
                severity="info",
                code="freshness_universe",
                message=f"Freshness by universe: {freshness_summary}.",
                source="freshness_report",
            )
        )
    if reconciliation_report is not None:
        reconciliation = _reconciliation_status(reconciliation_report)
        matched = reconciliation.get("matched")
        differences = _string_list(reconciliation.get("differences"))
        suffix = f" differences={', '.join(differences)}" if differences else ""
        findings.append(
            PaperAuditFinding(
                severity="info",
                code="reconciliation_state",
                message=f"Reconciliation matched={matched}.{suffix}",
                source="reconciliation_report",
            )
        )
    return tuple(findings)


def _reconciliation_status(report: Mapping[str, object] | None) -> Mapping[str, object]:
    if report is None:
        return {}
    nested = _mapping_or_none(report.get("reconciliation"))
    if nested is not None:
        return nested
    return report


def _freshness_summary(report: Mapping[str, object]) -> str:
    parts: list[str] = []
    for symbol, detail in sorted(_freshness_symbols(report).items()):
        status = detail.get("status") or "unknown"
        timestamp = detail.get("timestamp")
        timestamp_suffix = f"@{timestamp}" if timestamp else ""
        parts.append(f"{symbol}={status}{timestamp_suffix}")
    return ", ".join(parts)


def _freshness_symbols(report: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    symbols = _mapping_or_none(report.get("symbols")) or {}
    return {str(symbol): detail for symbol, detail in symbols.items() if isinstance(detail, Mapping)}


def _drifted_feature_count(report: Mapping[str, object] | None) -> int:
    if report is None:
        return 0
    summary = _mapping_or_none(report.get("summary")) or {}
    summary_value = _int_or_none(summary.get("drifted_feature_count"))
    if summary_value is not None:
        return summary_value
    metrics = report.get("metrics")
    if isinstance(metrics, list):
        return sum(1 for metric in metrics if isinstance(metric, Mapping) and metric.get("drifted") is True)
    return 0


def _drift_warning_count(report: Mapping[str, object] | None) -> int:
    if report is None:
        return 0
    summary = _mapping_or_none(report.get("summary")) or {}
    summary_value = _int_or_none(summary.get("warn_count"))
    if summary_value is not None:
        return summary_value
    findings = report.get("findings")
    if isinstance(findings, list):
        return sum(1 for finding in findings if isinstance(finding, Mapping) and finding.get("severity") == "warn")
    return 0


def _mlflow_candidate_review_passed(report: Mapping[str, object] | None) -> bool | None:
    if report is None:
        return None
    return str(report.get("status") or "").upper() == "PASSED"


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if value is None or value == "":
        return []
    return [str(value)]


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _non_empty_string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _count(findings: list[PaperAuditFinding], severity: str) -> int:
    return sum(1 for finding in findings if finding.severity == severity)


def _markdown_finding_row(finding: PaperAuditFinding) -> str:
    return (
        f"| `{_escape_markdown(finding.severity)}` "
        f"| `{_escape_markdown(finding.code)}` "
        f"| {_escape_markdown(finding.message)} "
        f"| `{_escape_markdown(finding.source)}` |"
    )


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
