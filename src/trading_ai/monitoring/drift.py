"""Offline feature drift monitoring for feature CSV snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import inf, isfinite, sqrt
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0"
DEFAULT_EXCLUDED_COLUMNS = frozenset({"timestamp", "symbol", "open", "high", "low", "close", "volume"})


@dataclass(frozen=True)
class FeatureDriftMetric:
    feature: str
    reference_count: int
    current_count: int
    reference_missing_rate: float
    current_missing_rate: float
    missing_delta: float
    reference_mean: float | None
    current_mean: float | None
    mean_delta: float | None
    mean_z: float | None
    reference_std: float | None
    current_std: float | None
    std_ratio: float | None
    drifted: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "reference_count": self.reference_count,
            "current_count": self.current_count,
            "reference_missing_rate": self.reference_missing_rate,
            "current_missing_rate": self.current_missing_rate,
            "missing_delta": self.missing_delta,
            "reference_mean": self.reference_mean,
            "current_mean": self.current_mean,
            "mean_delta": self.mean_delta,
            "mean_z": _json_number(self.mean_z),
            "reference_std": self.reference_std,
            "current_std": self.current_std,
            "std_ratio": _json_number(self.std_ratio),
            "drifted": self.drifted,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class FeatureDriftFinding:
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
class FeatureDriftReport:
    schema_version: str
    generated_at: str
    drift_detected: bool
    findings: tuple[FeatureDriftFinding, ...]
    sources: dict[str, str]
    summary: dict[str, object]
    metrics: tuple[FeatureDriftMetric, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "drift_detected": self.drift_detected,
            "findings": [finding.to_dict() for finding in self.findings],
            "sources": dict(self.sources),
            "summary": dict(self.summary),
            "metrics": [metric.to_dict() for metric in self.metrics],
        }


def evaluate_feature_drift(
    reference_rows: Sequence[Mapping[str, object]],
    current_rows: Sequence[Mapping[str, object]],
    *,
    feature_names: Iterable[str] | None = None,
    mean_z_threshold: float = 2.0,
    missing_delta_threshold: float = 0.10,
    std_ratio_threshold: float = 2.0,
    min_samples: int = 20,
    sources: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> FeatureDriftReport:
    _validate_thresholds(
        mean_z_threshold=mean_z_threshold,
        missing_delta_threshold=missing_delta_threshold,
        std_ratio_threshold=std_ratio_threshold,
        min_samples=min_samples,
    )
    features = _select_feature_names(reference_rows, current_rows, feature_names=feature_names)
    if not features:
        raise ValueError("no numeric feature columns are monitorable")

    metrics = tuple(
        _evaluate_feature(
            feature,
            reference_rows,
            current_rows,
            mean_z_threshold=mean_z_threshold,
            missing_delta_threshold=missing_delta_threshold,
            std_ratio_threshold=std_ratio_threshold,
            min_samples=min_samples,
        )
        for feature in features
    )
    findings = _build_findings(
        metrics,
        reference_row_count=len(reference_rows),
        current_row_count=len(current_rows),
    )
    warn_count = sum(1 for finding in findings if finding.severity == "warn")
    info_count = sum(1 for finding in findings if finding.severity == "info")
    drifted_feature_count = sum(1 for metric in metrics if metric.drifted)
    summary: dict[str, object] = {
        "reference_row_count": len(reference_rows),
        "current_row_count": len(current_rows),
        "feature_count": len(metrics),
        "drifted_feature_count": drifted_feature_count,
        "warn_count": warn_count,
        "info_count": info_count,
        "mean_z_threshold": mean_z_threshold,
        "missing_delta_threshold": missing_delta_threshold,
        "std_ratio_threshold": std_ratio_threshold,
        "min_samples": min_samples,
    }
    return FeatureDriftReport(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        drift_detected=drifted_feature_count > 0,
        findings=findings,
        sources={str(key): str(value) for key, value in (sources or {}).items()},
        summary=summary,
        metrics=metrics,
    )


def render_feature_drift_markdown(report: FeatureDriftReport) -> str:
    status = "DRIFT DETECTED" if report.drift_detected else "STABLE"
    lines = [
        "# Feature Drift Report",
        "",
        f"Status: **{status}**",
        "",
        f"Generated at: `{report.generated_at}`",
        f"Reference rows: `{report.summary.get('reference_row_count')}`",
        f"Current rows: `{report.summary.get('current_row_count')}`",
        f"Features evaluated: `{report.summary.get('feature_count')}`",
        f"Features drifted: `{report.summary.get('drifted_feature_count')}`",
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
            "## Metrics",
            "",
            "| Feature | Drifted | Reference Count | Current Count | Missing Delta | Mean Z | Std Ratio | Warnings |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for metric in report.metrics:
        lines.append(
            "| "
            f"`{_escape_markdown(metric.feature)}` "
            f"| `{metric.drifted}` "
            f"| {metric.reference_count} "
            f"| {metric.current_count} "
            f"| {_format_number(metric.missing_delta)} "
            f"| {_format_number(metric.mean_z)} "
            f"| {_format_number(metric.std_ratio)} "
            f"| `{_escape_markdown(', '.join(metric.warnings))}` |"
        )
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
    lines.append("")
    return "\n".join(lines)


def _evaluate_feature(
    feature: str,
    reference_rows: Sequence[Mapping[str, object]],
    current_rows: Sequence[Mapping[str, object]],
    *,
    mean_z_threshold: float,
    missing_delta_threshold: float,
    std_ratio_threshold: float,
    min_samples: int,
) -> FeatureDriftMetric:
    reference_values, reference_missing_rate = _numeric_values(reference_rows, feature)
    current_values, current_missing_rate = _numeric_values(current_rows, feature)
    reference_mean = _mean(reference_values)
    current_mean = _mean(current_values)
    reference_std = _std(reference_values, reference_mean)
    current_std = _std(current_values, current_mean)
    mean_delta = (
        current_mean - reference_mean
        if reference_mean is not None and current_mean is not None
        else None
    )
    mean_z = _mean_z(mean_delta, reference_std) if mean_delta is not None else None
    std_ratio = _std_ratio(reference_std, current_std)
    missing_delta = current_missing_rate - reference_missing_rate

    warnings: list[str] = []
    if len(reference_values) < min_samples or len(current_values) < min_samples:
        warnings.append("low_sample_count")
    if abs(missing_delta) > missing_delta_threshold:
        warnings.append("missingness_shift")
    if mean_z is not None and abs(mean_z) > mean_z_threshold:
        warnings.append("mean_shift")
    if _std_ratio_outside_threshold(std_ratio, std_ratio_threshold):
        warnings.append("std_shift")

    return FeatureDriftMetric(
        feature=feature,
        reference_count=len(reference_values),
        current_count=len(current_values),
        reference_missing_rate=reference_missing_rate,
        current_missing_rate=current_missing_rate,
        missing_delta=missing_delta,
        reference_mean=reference_mean,
        current_mean=current_mean,
        mean_delta=mean_delta,
        mean_z=mean_z,
        reference_std=reference_std,
        current_std=current_std,
        std_ratio=std_ratio,
        drifted=bool(warnings),
        warnings=tuple(warnings),
    )


def _build_findings(
    metrics: Sequence[FeatureDriftMetric],
    *,
    reference_row_count: int,
    current_row_count: int,
) -> tuple[FeatureDriftFinding, ...]:
    findings: list[FeatureDriftFinding] = [
        FeatureDriftFinding(
            severity="info",
            code="row_count",
            message=f"Reference rows={reference_row_count}; current rows={current_row_count}.",
            source="feature_drift",
        ),
        FeatureDriftFinding(
            severity="info",
            code="features_evaluated",
            message=f"Evaluated {len(metrics)} numeric feature(s).",
            source="feature_drift",
        ),
    ]
    drifted_features = [metric.feature for metric in metrics if metric.drifted]
    findings.append(
        FeatureDriftFinding(
            severity="info",
            code="features_drifted",
            message=f"Drifted feature count={len(drifted_features)}.",
            source="feature_drift",
        )
    )
    for metric in metrics:
        for warning in metric.warnings:
            findings.append(
                FeatureDriftFinding(
                    severity="warn",
                    code=warning,
                    message=_warning_message(metric, warning),
                    source=metric.feature,
                )
            )
    return tuple(findings)


def _warning_message(metric: FeatureDriftMetric, warning: str) -> str:
    if warning == "low_sample_count":
        return (
            f"{metric.feature} has low valid samples: "
            f"reference={metric.reference_count}, current={metric.current_count}."
        )
    if warning == "missingness_shift":
        return f"{metric.feature} missingness changed by {_format_number(metric.missing_delta)}."
    if warning == "mean_shift":
        return f"{metric.feature} mean shifted by z={_format_number(metric.mean_z)}."
    if warning == "std_shift":
        return f"{metric.feature} standard deviation ratio is {_format_number(metric.std_ratio)}."
    return f"{metric.feature} drift warning: {warning}."


def _select_feature_names(
    reference_rows: Sequence[Mapping[str, object]],
    current_rows: Sequence[Mapping[str, object]],
    *,
    feature_names: Iterable[str] | None,
) -> tuple[str, ...]:
    if feature_names is not None:
        requested = tuple(dict.fromkeys(str(name).strip() for name in feature_names if str(name).strip()))
        return tuple(
            name
            for name in requested
            if _is_monitorable_feature(name, reference_rows, current_rows)
        )
    reference_columns = _columns(reference_rows)
    current_columns = _columns(current_rows)
    candidates = sorted((reference_columns & current_columns) - DEFAULT_EXCLUDED_COLUMNS)
    return tuple(
        name
        for name in candidates
        if _is_monitorable_feature(name, reference_rows, current_rows)
    )


def _is_monitorable_feature(
    feature: str,
    reference_rows: Sequence[Mapping[str, object]],
    current_rows: Sequence[Mapping[str, object]],
) -> bool:
    if feature in DEFAULT_EXCLUDED_COLUMNS:
        return False
    reference_has_numeric = any(_to_float(row.get(feature)) is not None for row in reference_rows)
    current_has_numeric = any(_to_float(row.get(feature)) is not None for row in current_rows)
    return reference_has_numeric and current_has_numeric


def _columns(rows: Sequence[Mapping[str, object]]) -> set[str]:
    names: set[str] = set()
    for row in rows:
        names.update(str(name) for name in row.keys())
    return names


def _numeric_values(rows: Sequence[Mapping[str, object]], feature: str) -> tuple[tuple[float, ...], float]:
    values = tuple(
        numeric
        for row in rows
        for numeric in [_to_float(row.get(feature))]
        if numeric is not None
    )
    missing_rate = 0.0 if not rows else (len(rows) - len(values)) / len(rows)
    return values, missing_rate


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(numeric):
        return None
    return numeric


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _std(values: Sequence[float], mean: float | None) -> float | None:
    if mean is None or not values:
        return None
    return sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _mean_z(mean_delta: float, reference_std: float | None) -> float:
    if reference_std is None:
        return 0.0 if mean_delta == 0 else inf
    if reference_std == 0:
        return 0.0 if mean_delta == 0 else inf
    return mean_delta / reference_std


def _std_ratio(reference_std: float | None, current_std: float | None) -> float | None:
    if reference_std is None or current_std is None:
        return None
    if reference_std == 0:
        return 1.0 if current_std == 0 else inf
    return current_std / reference_std


def _std_ratio_outside_threshold(std_ratio: float | None, threshold: float) -> bool:
    if std_ratio is None:
        return False
    return std_ratio < (1 / threshold) or std_ratio > threshold


def _validate_thresholds(
    *,
    mean_z_threshold: float,
    missing_delta_threshold: float,
    std_ratio_threshold: float,
    min_samples: int,
) -> None:
    if mean_z_threshold <= 0:
        raise ValueError("mean_z_threshold must be positive")
    if missing_delta_threshold < 0:
        raise ValueError("missing_delta_threshold must be non-negative")
    if std_ratio_threshold <= 1:
        raise ValueError("std_ratio_threshold must be greater than 1")
    if min_samples < 1:
        raise ValueError("min_samples must be at least 1")


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    if value == inf:
        return "inf"
    if value == -inf:
        return "-inf"
    return f"{value:.6g}"


def _json_number(value: float | None) -> float | None:
    if value is None or not isfinite(value):
        return None
    return value


def _markdown_finding_row(finding: FeatureDriftFinding) -> str:
    return (
        f"| `{_escape_markdown(finding.severity)}` "
        f"| `{_escape_markdown(finding.code)}` "
        f"| {_escape_markdown(finding.message)} "
        f"| `{_escape_markdown(finding.source)}` |"
    )


def _escape_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
