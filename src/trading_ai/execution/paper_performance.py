"""Paper-only performance and paper-vs-backtest gap reporting."""

from __future__ import annotations

import csv
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_auto_sessions import summarize_paper_auto_sessions
from trading_ai.execution.paper_common import (
    paper_exit_code,
    read_json_artifact,
    write_json_artifact,
    write_text_artifact,
)
from trading_ai.execution.paper_observability import build_paper_observability_report

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT = "reports/tmp/paper_performance/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/paper_performance/latest.md"
DEFAULT_MIN_STABLE_SESSIONS = 60
DEFAULT_MIN_STABLE_FILLS = 60


class PaperPerformanceOperationalError(RuntimeError):
    """Raised when the paper performance report cannot be produced."""


@dataclass(frozen=True)
class PaperPerformanceReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_performance_report(
    *,
    sessions_root: str | Path = "reports/tmp/paper_session",
    session_dirs: Iterable[str | Path] = (),
    ledger_inputs: Iterable[str | Path] = (),
    backtest_report: str | Path | None = None,
    broker_statement: str | Path | None = None,
    min_stable_sessions: int = DEFAULT_MIN_STABLE_SESSIONS,
    min_stable_fills: int = DEFAULT_MIN_STABLE_FILLS,
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    generated_at: str | None = None,
) -> PaperPerformanceReportResult:
    generated = generated_at or _utc_now()
    report = build_paper_performance_report(
        sessions_root=sessions_root,
        session_dirs=session_dirs,
        ledger_inputs=ledger_inputs,
        backtest_report=backtest_report,
        broker_statement=broker_statement,
        min_stable_sessions=min_stable_sessions,
        min_stable_fills=min_stable_fills,
        generated_at=generated,
    )
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_json_artifact(report, output_path)
    write_text_artifact(render_paper_performance_markdown(report), markdown_path)
    return PaperPerformanceReportResult(
        exit_code=paper_exit_code(str(report["status"])),
        status=str(report["status"]),
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_paper_performance_report(
    *,
    sessions_root: str | Path = "reports/tmp/paper_session",
    session_dirs: Iterable[str | Path] = (),
    ledger_inputs: Iterable[str | Path] = (),
    backtest_report: str | Path | None = None,
    broker_statement: str | Path | None = None,
    min_stable_sessions: int = DEFAULT_MIN_STABLE_SESSIONS,
    min_stable_fills: int = DEFAULT_MIN_STABLE_FILLS,
    generated_at: str | None = None,
) -> dict[str, object]:
    if min_stable_sessions < 1:
        raise ValueError("min_stable_sessions must be >= 1")
    if min_stable_fills < 1:
        raise ValueError("min_stable_fills must be >= 1")
    generated = generated_at or _utc_now()
    observability = build_paper_observability_report(
        sessions_root=sessions_root,
        session_dirs=session_dirs,
        ledger_inputs=ledger_inputs,
        generated_at=generated,
    )
    session_roots = _discover_session_dirs(Path(sessions_root), [Path(path) for path in session_dirs])
    closeouts, diagnostics = _read_closeouts(session_roots)
    warnings: list[str] = [str(item["reason"]) for item in diagnostics]
    blockers: list[str] = []
    metrics = _paper_metrics(observability.summary, closeouts, warnings=warnings, blockers=blockers)
    statement = _statement_reconciliation(broker_statement, closeouts, warnings=warnings, blockers=blockers)
    paper_auto_sessions = summarize_paper_auto_sessions(ledger_inputs, min_clean_sessions=20)
    _extend_blockers_from_paper_auto(paper_auto_sessions, blockers)
    closeout_coverage = _closeout_coverage(metrics, paper_auto_sessions)
    statement_summary = _statement_status(statement, metrics, paper_auto_sessions, broker_statement=broker_statement)
    if statement_summary.get("status") in {"DIFFERENCES", "MISMATCH", "ERROR", "MISSING"}:
        blockers.append("statement_mismatch")
    if statement_summary.get("status") == "STATEMENT_PENDING" and _int_value(
        statement_summary.get("local_fills")
    ) > 0:
        blockers.append("statement_pending")
    if _int_value(statement_summary.get("unreconciled_fills")) > 0:
        blockers.append("fills_unreconciled")
    if statement.get("pnl_source") == "broker_statement":
        pnl = _mapping(metrics.get("pnl"))
        metrics["pnl"] = {
            **dict(pnl),
            "source": "broker_statement",
            "broker_statement": True,
            "realized_pnl": statement.get("realized_pnl"),
            "certification_eligible": True,
        }
    else:
        pnl = _mapping(metrics.get("pnl"))
        proxy_reasons = pnl.get("attribution_reasons")
        if isinstance(proxy_reasons, list) and proxy_reasons:
            blockers.append("proxy_pnl_unattributable")
            warnings.extend(str(reason) for reason in proxy_reasons)
    gap = _paper_vs_backtest(backtest_report, metrics, warnings=warnings)
    stability_requirements = _stability_requirements(
        metrics,
        min_stable_sessions=min_stable_sessions,
        min_stable_fills=min_stable_fills,
        warnings=warnings,
    )
    metrics["performance_stable"] = (
        bool(metrics.get("performance_stable"))
        and not warnings
        and bool(gap.get("backtest_available"))
        and not blockers
        and bool(stability_requirements.get("met"))
    )
    if statement.get("status") == "ERROR":
        status = "ERROR"
    elif blockers or warnings:
        status = "WARN"
    else:
        status = "OK"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "status": status,
        "sources": {
            "sessions_root": str(Path(sessions_root)),
            "session_dirs": [str(path) for path in session_dirs],
            "ledger_inputs": [str(path) for path in ledger_inputs],
            "backtest_report": str(backtest_report) if backtest_report is not None else None,
            "broker_statement": str(broker_statement) if broker_statement is not None else None,
        },
        "paper_metrics": metrics,
        "stability_requirements": stability_requirements,
        "paper_auto_sessions": paper_auto_sessions,
        "closeout_coverage": closeout_coverage,
        "statement_status": statement_summary,
        "paper_vs_backtest": gap,
        "statement_reconciliation": statement,
        "observability_summary": dict(observability.summary),
        "warnings": _dedupe(warnings),
        "blockers": _dedupe(blockers),
        "diagnostics": diagnostics,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_performance_markdown(report: Mapping[str, object]) -> str:
    metrics = _mapping(report.get("paper_metrics"))
    pnl = _mapping(metrics.get("pnl"))
    gap = _mapping(report.get("paper_vs_backtest"))
    statement = _mapping(report.get("statement_reconciliation"))
    statement_status = _mapping(report.get("statement_status"))
    closeout_coverage = _mapping(report.get("closeout_coverage"))
    paper_auto = _mapping(report.get("paper_auto_sessions"))
    stability = _mapping(report.get("stability_requirements"))
    symbols_value = metrics.get("symbols")
    symbols = symbols_value if isinstance(symbols_value, (list, tuple, set)) else ()
    lines = [
        "# Paper Performance",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{report.get('generated_at') or ''}`",
        "",
        "## Paper Metrics",
        "",
        f"Complete sessions: `{metrics.get('complete_sessions', 0)}`",
        f"Submits: `{metrics.get('submits', 0)}`",
        f"Fills: `{metrics.get('fills', 0)}`",
        f"Pending closeouts: `{metrics.get('pending_closeouts', 0)}`",
        f"Unmatched closeouts: `{metrics.get('unmatched_closeouts', 0)}`",
        f"Rejected orders: `{metrics.get('rejections', 0)}`",
        f"Symbols: `{', '.join(str(symbol) for symbol in symbols)}`",
        f"Date range: `{_mapping(metrics.get('dates')).get('start') or ''}` "
        f"to `{_mapping(metrics.get('dates')).get('end') or ''}`",
        f"PnL source: `{pnl.get('source') or ''}`",
        f"Performance stable: `{metrics.get('performance_stable')}`",
        f"Stability requirements met: `{stability.get('met')}`",
        "Stable sessions required: "
        f"`{stability.get('complete_sessions', 0)}` / `{stability.get('min_complete_sessions', 0)}`",
        f"Stable fills required: `{stability.get('fills', 0)}` / `{stability.get('min_fills', 0)}`",
        "",
        "## Paper Auto Sessions",
        "",
        f"State: `{paper_auto.get('state') or ''}`",
        f"Clean sessions: `{paper_auto.get('clean_sessions', 0)}`",
        "",
        "## Closeout Coverage",
        "",
        f"Closed: `{closeout_coverage.get('closed', 0)}`",
        f"Pending: `{closeout_coverage.get('pending', 0)}`",
        f"Unmatched: `{closeout_coverage.get('unmatched', 0)}`",
        "",
        "## Paper vs Backtest",
        "",
        f"Backtest available: `{gap.get('backtest_available')}`",
        f"Backtest trade count: `{_mapping(gap.get('backtest_metrics')).get('trade_count', '')}`",
        "",
        "## Broker Statement",
        "",
        f"Statement status: `{statement_status.get('status') or statement.get('status') or 'not_requested'}`",
        f"Matched fills: `{statement.get('matched_fills', 0)}`",
        f"Missing fills: `{statement.get('missing_fills', 0)}`",
        "",
    ]
    return "\n".join(lines)


def _paper_metrics(
    observability_summary: Mapping[str, object],
    closeouts: list[Mapping[str, object]],
    *,
    warnings: list[str],
    blockers: list[str],
) -> dict[str, object]:
    symbols: set[str] = set()
    dates: list[str] = []
    fills = 0
    pending = 0
    unmatched = 0
    rejected = 0
    notional_deltas: list[float] = []
    for closeout in closeouts:
        status = str(closeout.get("status") or "").upper()
        session = _mapping(closeout.get("session"))
        expected = _mapping(closeout.get("expected_order"))
        broker_order = _mapping(closeout.get("broker_order"))
        symbol = str(expected.get("symbol") or broker_order.get("symbol") or "").upper()
        if symbol:
            symbols.add(symbol)
        if session.get("as_of_date"):
            dates.append(str(session["as_of_date"]))
        if str(broker_order.get("status") or "").lower() == "rejected":
            rejected += 1
        if status == "PENDING":
            pending += 1
            blockers.append("closeout_pending")
        elif status == "UNMATCHED":
            unmatched += 1
            blockers.append("closeout_unmatched")
        elif status == "CLOSED":
            fills += 1
            expected_notional = _positive_float_or_none(expected.get("notional"))
            fill_price = _positive_float_or_none(broker_order.get("filled_avg_price"))
            fill_quantity = _positive_float_or_none(
                _first_present_value(broker_order, ("filled_quantity", "quantity"))
            )
            if fill_price is None:
                warnings.append("missing_fill_price")
            if fill_quantity is None:
                warnings.append("missing_fill_quantity")
            if expected_notional is not None and fill_price is not None and fill_quantity is not None:
                filled_notional = fill_price * fill_quantity
                notional_deltas.append(filled_notional - expected_notional)
    proxy_pnl, proxy_reasons = _attributed_proxy_pnl(closeouts)
    closeout_closed = _int_value(observability_summary.get("closeouts_closed"))
    executions_submitted = _int_value(observability_summary.get("executions_submitted"))
    performance_stable = bool(fills) and not pending and not unmatched and not rejected and not warnings
    return {
        "complete_sessions": closeout_closed,
        "submits": executions_submitted,
        "fills": fills,
        "pending_closeouts": pending,
        "unmatched_closeouts": unmatched,
        "rejections": rejected + _int_value(observability_summary.get("executions_blocked")),
        "slippage_proxy": {
            "source": "filled_notional_minus_expected_notional",
            "average_notional_delta": sum(notional_deltas) / len(notional_deltas) if notional_deltas else None,
            "sample_count": len(notional_deltas),
        },
        "symbols": sorted(symbols),
        "dates": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
        "signal_drift": {
            "source": "paper_observability",
            "submitted_sessions": executions_submitted,
            "filled_sessions": fills,
            "drift_count": max(executions_submitted - fills, 0),
        },
        "pnl": {
            "source": "proxy" if proxy_pnl is not None else "unavailable",
            "proxy_unrealized_pnl": proxy_pnl,
            "broker_statement": False,
            "certification_eligible": proxy_pnl is not None,
            "attribution_reasons": proxy_reasons,
        },
        "performance_stable": performance_stable,
    }


def _stability_requirements(
    metrics: Mapping[str, object],
    *,
    min_stable_sessions: int,
    min_stable_fills: int,
    warnings: list[str],
) -> dict[str, object]:
    complete_sessions = _int_value(metrics.get("complete_sessions"))
    fills = _int_value(metrics.get("fills"))
    reasons: list[str] = []
    if complete_sessions < min_stable_sessions:
        reasons.append("stable_sessions_below_minimum")
    if fills < min_stable_fills:
        reasons.append("fills_below_minimum")
    warnings.extend(reasons)
    return {
        "min_complete_sessions": min_stable_sessions,
        "min_fills": min_stable_fills,
        "complete_sessions": complete_sessions,
        "fills": fills,
        "met": not reasons,
        "reasons": reasons,
    }


def _paper_vs_backtest(
    backtest_report: str | Path | None,
    metrics: Mapping[str, object],
    *,
    warnings: list[str],
) -> dict[str, object]:
    if backtest_report is None:
        warnings.append("missing_backtest_report")
        return {
            "backtest_available": False,
            "warnings": ["missing_backtest_report"],
            "stable_for_risk_expansion": False,
        }
    path = Path(backtest_report)
    try:
        payload = read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        warnings.append("invalid_backtest_report")
        return {
            "backtest_available": False,
            "warnings": [f"invalid_backtest_report:{exc}"],
            "stable_for_risk_expansion": False,
        }
    backtest_metrics = _mapping(payload.get("metrics"))
    paper_trade_count = _float_or_none(metrics.get("fills"))
    backtest_trade_count = _float_or_none(backtest_metrics.get("trade_count"))
    return {
        "backtest_available": True,
        "backtest_metrics": {
            "trade_count": backtest_metrics.get("trade_count"),
            "turnover": backtest_metrics.get("turnover"),
            "estimated_costs": backtest_metrics.get("estimated_costs"),
            "sharpe": backtest_metrics.get("sharpe"),
            "max_drawdown": backtest_metrics.get("max_drawdown"),
        },
        "paper_trade_count": metrics.get("fills", 0),
        "trade_count_gap": paper_trade_count - backtest_trade_count
        if paper_trade_count is not None and backtest_trade_count is not None
        else None,
        "stable_for_risk_expansion": False,
    }


def _statement_reconciliation(
    broker_statement: str | Path | None,
    closeouts: list[Mapping[str, object]],
    *,
    warnings: list[str],
    blockers: list[str],
) -> dict[str, object]:
    local_fills = _local_fill_records(closeouts)
    if broker_statement is None:
        return {
            "status": "NOT_REQUESTED",
            "source_path": None,
            "local_fills": len(local_fills),
            "statement_fills": 0,
            "matched_fills": 0,
            "missing_fills": 0,
            "extra_fills": 0,
            "mismatches": [],
            "realized_pnl": None,
            "pnl_source": "proxy",
        }
    path = Path(broker_statement)
    if not path.exists():
        warnings.append("missing_broker_statement")
        return {
            "status": "MISSING",
            "source_path": str(path),
            "local_fills": len(local_fills),
            "statement_fills": 0,
            "matched_fills": 0,
            "missing_fills": len(local_fills),
            "extra_fills": 0,
            "mismatches": [],
            "realized_pnl": None,
            "pnl_source": "proxy",
        }
    try:
        statement_fills = _read_statement_fills(path)
    except (OSError, json.JSONDecodeError, ValueError, csv.Error) as exc:
        blockers.append("invalid_broker_statement")
        return {
            "status": "ERROR",
            "source_path": str(path),
            "local_fills": len(local_fills),
            "statement_fills": 0,
            "matched_fills": 0,
            "missing_fills": len(local_fills),
            "extra_fills": 0,
            "mismatches": [{"code": "invalid_broker_statement", "message": str(exc)}],
            "realized_pnl": None,
            "pnl_source": "proxy",
        }
    mismatches: list[dict[str, object]] = []
    local_by_client_id = _fills_by_client_id(
        local_fills,
        source="local",
        mismatches=mismatches,
        blockers=blockers,
    )
    statement_by_client_id = _fills_by_client_id(
        statement_fills,
        source="statement",
        mismatches=mismatches,
        blockers=blockers,
    )
    matched = 0
    realized_pnl = 0.0
    for client_order_id, local_group in local_by_client_id.items():
        if len(local_group) != 1:
            continue
        statement_group = statement_by_client_id.get(client_order_id, [])
        if not statement_group:
            blockers.append("statement_missing_fill")
            mismatches.append({"code": "statement_missing_fill", "client_order_id": client_order_id})
            continue
        if len(statement_group) != 1:
            continue
        local = local_group[0]
        statement = statement_group[0]
        if _append_fill_mismatches(local, statement, mismatches=mismatches, blockers=blockers):
            continue
        statement_pnl = _float_or_none(statement.get("realized_pnl"))
        if statement_pnl is None:
            blockers.append("statement_invalid_realized_pnl")
            mismatches.append(
                {"code": "statement_invalid_realized_pnl", "client_order_id": client_order_id}
            )
            continue
        matched += 1
        realized_pnl += statement_pnl
    for client_order_id, statement_group in statement_by_client_id.items():
        if client_order_id not in local_by_client_id:
            blockers.append("statement_extra_fill")
            mismatches.append(
                {
                    "code": "statement_extra_fill",
                    "client_order_id": client_order_id,
                    "count": len(statement_group),
                }
            )
    clean_bijection = (
        not mismatches
        and matched == len(local_fills)
        and matched == len(statement_fills)
    )
    return {
        "status": "MATCHED" if clean_bijection else "DIFFERENCES",
        "source_path": str(path),
        "local_fills": len(local_fills),
        "statement_fills": len(statement_fills),
        "matched_fills": matched,
        "missing_fills": sum(1 for item in mismatches if item.get("code") == "statement_missing_fill"),
        "extra_fills": sum(
            _int_value(item.get("count")) or 1
            for item in mismatches
            if item.get("code") == "statement_extra_fill"
        ),
        "mismatches": mismatches,
        "realized_pnl": realized_pnl if clean_bijection else None,
        "pnl_source": "broker_statement" if clean_bijection else "proxy",
    }


def _closeout_coverage(metrics: Mapping[str, object], paper_auto: Mapping[str, object]) -> dict[str, object]:
    classifications = _mapping(paper_auto.get("classifications"))
    closed = _int_value(metrics.get("complete_sessions"))
    pending = _int_value(metrics.get("pending_closeouts")) + _int_value(classifications.get("CLOSEOUT_PENDING"))
    unmatched = _int_value(metrics.get("unmatched_closeouts"))
    submitted_no_fill = _int_value(classifications.get("SUBMITTED_NO_FILL"))
    total = closed + pending + unmatched + submitted_no_fill
    return {
        "closed": closed,
        "pending": pending,
        "unmatched": unmatched,
        "submitted_no_fill": submitted_no_fill,
        "total_tracked": total,
        "coverage_ratio": (closed / total) if total else None,
    }


def _statement_status(
    statement: Mapping[str, object],
    metrics: Mapping[str, object],
    paper_auto: Mapping[str, object],
    *,
    broker_statement: str | Path | None,
) -> dict[str, object]:
    classifications = _mapping(paper_auto.get("classifications"))
    statement_status = str(statement.get("status") or "UNKNOWN").upper()
    local_fills = _int_value(statement.get("local_fills") or metrics.get("fills"))
    unreconciled = (
        _int_value(statement.get("missing_fills"))
        + _int_value(statement.get("extra_fills"))
        + _int_value(classifications.get("FILL_UNRECONCILED"))
    )
    if (
        broker_statement is None
        and (local_fills > 0 or _int_value(paper_auto.get("clean_sessions")) > 0)
        or _int_value(classifications.get("STATEMENT_PENDING")) > 0
    ):
        status = "STATEMENT_PENDING"
    else:
        status = statement_status
    return {
        "status": status,
        "statement_present": broker_statement is not None and statement_status not in {"MISSING", "NOT_REQUESTED"},
        "local_fills": local_fills,
        "statement_fills": _int_value(statement.get("statement_fills")),
        "matched_fills": _int_value(statement.get("matched_fills")),
        "extra_fills": _int_value(statement.get("extra_fills")),
        "unreconciled_fills": unreconciled,
        "source_path": statement.get("source_path"),
    }


def _extend_blockers_from_paper_auto(summary: Mapping[str, object], blockers: list[str]) -> None:
    histogram = summary.get("blocker_histogram")
    if not isinstance(histogram, Mapping):
        return
    for code in histogram:
        blockers.append(str(code))


def _read_statement_fills(path: Path) -> list[dict[str, object]]:
    normalized_as_of: str | None = None
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        raw_fills: object = rows
    else:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw_payload, Mapping):
            validation_status = str(raw_payload.get("status") or "").upper()
            if validation_status and validation_status != "OK":
                raise ValueError("broker statement validation status is not OK")
            raw_as_of = raw_payload.get("as_of_date")
            if not _is_missing(raw_as_of):
                normalized_as_of = _validate_iso_date(raw_as_of, field="broker statement as_of_date")
            raw_fills = _first_present_list(raw_payload, ("fills", "orders", "rows"))
        else:
            raw_fills = raw_payload
    if not isinstance(raw_fills, list):
        raise ValueError("broker statement must contain a fills list")
    fills: list[dict[str, object]] = []
    seen_client_ids: set[str] = set()
    for index, row in enumerate(raw_fills):
        if not isinstance(row, Mapping):
            raise ValueError(f"broker statement fill {index} must be an object")
        fill = _normalize_statement_fill(row)
        issues = _fill_identity_issues(fill, require_realized_pnl=True, require_aware_timestamp=True)
        if issues:
            raise ValueError(f"broker statement fill {index} is invalid: {','.join(issues)}")
        if normalized_as_of is not None:
            filled_at = _parse_aware_datetime(fill.get("filled_at"))
            if filled_at is None or filled_at.date().isoformat() != normalized_as_of:
                raise ValueError(f"broker statement fill {index} is outside as_of_date")
        client_order_id = str(fill["client_order_id"])
        if client_order_id in seen_client_ids:
            raise ValueError(f"broker statement fill {index} has duplicate client_order_id")
        seen_client_ids.add(client_order_id)
        fills.append(fill)
    return fills


def _first_present_list(payload: Mapping[str, object], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def _normalize_statement_fill(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "client_order_id": _text_or_none(
            _first_value(
                row,
                "client_order_id",
                "clientOrderId",
                "client order id",
                "client-order-id",
                "order_id",
                "order id",
                "clordid",
                "id",
            )
        ),
        "symbol": _upper_or_none(_first_value(row, "symbol", "asset", "ticker", "contract", "instrument")),
        "side": _lower_or_none(
            _first_value(row, "side", "order_side", "order side", "action", "transaction type", "buy sell")
        ),
        "quantity": _float_or_none(
            _first_value(
                row,
                "filled_quantity",
                "filled quantity",
                "filled_qty",
                "filled qty",
                "fill quantity",
                "quantity",
                "qty",
                "shares",
                "contracts",
            )
        ),
        "filled_avg_price": _float_or_none(
            _first_value(
                row,
                "filled_avg_price",
                "filled avg price",
                "filled average price",
                "avg_price",
                "avg price",
                "average fill price",
                "average price",
                "price",
                "fill_price",
                "fill price",
            )
        ),
        "filled_at": _text_or_none(
            _first_value(
                row,
                "filled_at",
                "filled at",
                "date",
                "timestamp",
                "filled_time",
                "filled time",
                "fill time",
                "execution time",
                "executed at",
                "trade date",
            )
        ),
        "realized_pnl": _float_or_none(
            _first_value(
                row,
                "realized_pnl",
                "realized pnl",
                "realized p&l",
                "realized p/l",
                "pnl",
                "p&l",
                "profit_loss",
                "profit loss",
                "profit/loss",
            )
        ),
    }


def _local_fill_records(closeouts: list[Mapping[str, object]]) -> list[dict[str, object]]:
    fills: list[dict[str, object]] = []
    for closeout in closeouts:
        if str(closeout.get("status") or "").upper() != "CLOSED":
            continue
        expected = _mapping(closeout.get("expected_order"))
        broker_order = _mapping(closeout.get("broker_order"))
        fills.append(
            {
                "client_order_id": _text_or_none(
                    _first_present_value(broker_order, ("client_order_id",))
                    if "client_order_id" in broker_order
                    else expected.get("client_order_id")
                ),
                "symbol": _upper_or_none(
                    _first_present_value(broker_order, ("symbol",))
                    if "symbol" in broker_order
                    else expected.get("symbol")
                ),
                "side": _lower_or_none(
                    _first_present_value(broker_order, ("side",))
                    if "side" in broker_order
                    else expected.get("side")
                ),
                "quantity": _float_or_none(
                    _first_present_value(broker_order, ("filled_quantity", "quantity"))
                ),
                "filled_avg_price": _float_or_none(broker_order.get("filled_avg_price")),
                "filled_at": _text_or_none(broker_order.get("filled_at")),
            }
        )
    return fills


def _fills_by_client_id(
    fills: list[dict[str, object]],
    *,
    source: str,
    mismatches: list[dict[str, object]],
    blockers: list[str],
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for index, fill in enumerate(fills):
        issues = _fill_identity_issues(
            fill,
            require_realized_pnl=source == "statement",
            require_aware_timestamp=True,
        )
        if issues:
            code = f"{source}_fill_partial_identity"
            blockers.append(code)
            mismatches.append({"code": code, "row": index, "fields": issues})
        client_order_id = _text_or_none(fill.get("client_order_id"))
        if client_order_id is None:
            continue
        grouped.setdefault(client_order_id, []).append(fill)
    for client_order_id, group in grouped.items():
        if len(group) <= 1:
            continue
        code = f"duplicate_{source}_client_order_id"
        blockers.append(code)
        mismatches.append(
            {
                "code": code,
                "client_order_id": client_order_id,
                "count": len(group),
            }
        )
    return grouped


def _fill_identity_issues(
    fill: Mapping[str, object],
    *,
    require_realized_pnl: bool,
    require_aware_timestamp: bool,
) -> list[str]:
    issues: list[str] = []
    if _text_or_none(fill.get("client_order_id")) is None:
        issues.append("client_order_id")
    if _text_or_none(fill.get("symbol")) is None:
        issues.append("symbol")
    if _lower_or_none(fill.get("side")) not in {"buy", "sell"}:
        issues.append("side")
    if _positive_float_or_none(fill.get("quantity")) is None:
        issues.append("quantity")
    if _positive_float_or_none(fill.get("filled_avg_price")) is None:
        issues.append("filled_avg_price")
    parsed_timestamp = _parse_aware_datetime(fill.get("filled_at"))
    if require_aware_timestamp and parsed_timestamp is None:
        issues.append("filled_at")
    if require_realized_pnl and _float_or_none(fill.get("realized_pnl")) is None:
        issues.append("realized_pnl")
    return issues


def _append_fill_mismatches(
    local: Mapping[str, object],
    statement: Mapping[str, object],
    *,
    mismatches: list[dict[str, object]],
    blockers: list[str],
) -> bool:
    before = len(mismatches)
    client_order_id = str(local.get("client_order_id") or "")
    comparisons = (
        ("symbol_mismatch", local.get("symbol"), statement.get("symbol")),
        ("side_mismatch", local.get("side"), statement.get("side")),
    )
    for code, expected, actual in comparisons:
        if _is_missing(expected) or _is_missing(actual):
            code = f"{code.removesuffix('_mismatch')}_identity_missing"
            blockers.append(code)
            mismatches.append(
                {
                    "code": code,
                    "client_order_id": client_order_id,
                    "local_present": not _is_missing(expected),
                    "statement_present": not _is_missing(actual),
                }
            )
        elif expected != actual:
            blockers.append(code)
            mismatches.append(
                {"code": code, "client_order_id": client_order_id, "local": expected, "statement": actual}
            )
    for code, field in (("qty_mismatch", "quantity"), ("price_mismatch", "filled_avg_price")):
        local_value = _float_or_none(local.get(field))
        statement_value = _float_or_none(statement.get(field))
        if local_value is None or statement_value is None:
            missing_code = f"{field}_identity_missing"
            blockers.append(missing_code)
            mismatches.append(
                {
                    "code": missing_code,
                    "client_order_id": client_order_id,
                    "local_present": local_value is not None,
                    "statement_present": statement_value is not None,
                }
            )
        elif abs(local_value - statement_value) > 1e-9:
            blockers.append(code)
            mismatches.append(
                {"code": code, "client_order_id": client_order_id, "local": local_value, "statement": statement_value}
            )
    local_date = _date_prefix(local.get("filled_at"))
    statement_date = _date_prefix(statement.get("filled_at"))
    if not local_date or not statement_date:
        blockers.append("date_identity_missing")
        mismatches.append(
            {
                "code": "date_identity_missing",
                "client_order_id": client_order_id,
                "local_present": bool(local_date),
                "statement_present": bool(statement_date),
            }
        )
    elif local_date != statement_date:
        blockers.append("date_mismatch")
        mismatches.append(
            {
                "code": "date_mismatch",
                "client_order_id": client_order_id,
                "local": local_date,
                "statement": statement_date,
            }
        )
    return len(mismatches) != before


def _attributed_proxy_pnl(closeouts: list[Mapping[str, object]]) -> tuple[float | None, list[str]]:
    closed = [closeout for closeout in closeouts if str(closeout.get("status") or "").upper() == "CLOSED"]
    if not closed:
        return 0.0, []
    symbols = [
        str(
            _mapping(closeout.get("broker_order")).get("symbol")
            or _mapping(closeout.get("expected_order")).get("symbol")
            or ""
        ).upper()
        for closeout in closed
    ]
    if any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        return None, ["proxy_pnl_symbol_attribution_ambiguous"]
    proxy_pnl = 0.0
    for closeout, symbol in zip(closed, symbols, strict=True):
        expected = _mapping(closeout.get("expected_order"))
        expected_notional = _positive_float_or_none(expected.get("notional"))
        positions = closeout.get("positions")
        if expected_notional is None or not isinstance(positions, list):
            return None, ["proxy_pnl_position_snapshot_incomplete"]
        matches = [
            position
            for position in positions
            if isinstance(position, Mapping) and str(position.get("symbol") or "").upper() == symbol
        ]
        if len(matches) != 1:
            return None, ["proxy_pnl_position_attribution_not_one_to_one"]
        market_value = _float_or_none(matches[0].get("market_value"))
        if market_value is None:
            return None, ["proxy_pnl_market_value_invalid"]
        proxy_pnl += market_value - expected_notional
    return proxy_pnl, []


def _read_closeouts(session_dirs: list[Path]) -> tuple[list[Mapping[str, object]], list[dict[str, object]]]:
    closeouts: list[Mapping[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for session_dir in session_dirs:
        path = session_dir / "closeout" / "paper_closeout.json"
        if not path.exists():
            continue
        try:
            closeouts.append(read_json_artifact(path))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            diagnostics.append({"reason": "invalid_closeout_json", "path": str(path), "message": str(exc)})
    return closeouts, diagnostics


def _discover_session_dirs(root: Path, explicit_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    if root.exists():
        if (root / "session.json").exists():
            paths.append(root)
        else:
            paths.extend(path.parent for path in root.rglob("session.json"))
    paths.extend(explicit_dirs)
    return _dedupe_paths(paths)


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _float_or_none(value: object) -> float | None:
    if _is_missing(value):
        return None
    try:
        numeric = float(str(value))
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _positive_float_or_none(value: object) -> float | None:
    numeric = _float_or_none(value)
    return numeric if numeric is not None and numeric > 0 else None


def _int_value(value: object) -> int:
    numeric = _float_or_none(value)
    return int(numeric) if numeric is not None else 0


def _first_value(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in row:
            return row.get(key)
        normalized_key = _normalize_key(key)
        for actual_key, value in row.items():
            if _normalize_key(actual_key) == normalized_key:
                return value
    return None


def _first_present_value(row: Mapping[str, object], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _text_or_none(value: object) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _upper_or_none(value: object) -> str | None:
    text = _text_or_none(value)
    return text.upper() if text is not None else None


def _lower_or_none(value: object) -> str | None:
    text = _text_or_none(value)
    return text.lower() if text is not None else None


def _date_prefix(value: object) -> str | None:
    parsed = _parse_aware_datetime(value)
    return parsed.date().isoformat() if parsed is not None else None


def _parse_aware_datetime(value: object) -> datetime | None:
    text = _text_or_none(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_iso_date(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format")
    return parsed.isoformat()


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Consolidated dashboard
# ---------------------------------------------------------------------------

def consolidated_dashboard(
    session_date: str,
    reports_root: Path,
) -> dict[str, object]:
    """Aggregate fills, risk state, and performance metrics into a single JSON-serializable dict.

    Missing or unparseable source files are represented as null fields with a WARNING log,
    never raising exceptions.
    """
    import logging  # noqa: PLC0415

    _dlog = logging.getLogger(__name__)

    def _safe_read(path: Path) -> dict[str, object] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _dlog.warning("consolidated_dashboard: could not read %s: %s", path, exc)
            return None

    risk_path = reports_root / "reports" / "tmp" / "paper_risk_state.json"
    performance_path = reports_root / "reports" / "tmp" / "paper_performance" / "latest.json"
    fills_path = reports_root / "reports" / "tmp" / "paper_session" / session_date / "fills.json"

    risk_data = _safe_read(risk_path)
    perf_data = _safe_read(performance_path)
    fills_data = _safe_read(fills_path)

    # Risk block
    risk_block: dict[str, object] = {
        "kill_switch_active": None,
        "current_drawdown_pct": None,
        "peak_equity": None,
    }
    if risk_data is not None:
        risk_block["kill_switch_active"] = bool(risk_data.get("kill_switch_active", False))
        opening_equity = risk_data.get("opening_equity")
        last_equity = risk_data.get("last_equity")
        peak_equity = risk_data.get("peak_equity")
        risk_block["peak_equity"] = float(str(peak_equity)) if peak_equity is not None else None
        try:
            oe = float(str(opening_equity)) if opening_equity is not None else 0.0
            le = float(str(last_equity)) if last_equity is not None else 0.0
            if oe > 0:
                drawdown = max(0.0, (oe - le) / oe) * 100.0
                risk_block["current_drawdown_pct"] = round(drawdown, 4)
        except (ValueError, TypeError):
            pass

    # Performance block
    perf_block: dict[str, object] = {
        "daily_pnl": None,
        "daily_return_pct": None,
        "sharpe_rolling_20d": None,
        "max_drawdown_pct": None,
    }
    if perf_data is not None:
        metrics_raw = perf_data.get("metrics")
        metrics: dict[str, object] = metrics_raw if isinstance(metrics_raw, dict) else {}
        pnl_raw = metrics.get("pnl")
        pnl: dict[str, object] = pnl_raw if isinstance(pnl_raw, dict) else {}
        perf_block["daily_pnl"] = pnl.get("proxy_unrealized_pnl")
        backtest_raw = perf_data.get("paper_vs_backtest")
        backtest: dict[str, object] = backtest_raw if isinstance(backtest_raw, dict) else {}
        perf_block["sharpe_rolling_20d"] = backtest.get("sharpe")
        perf_block["max_drawdown_pct"] = backtest.get("max_drawdown_pct")

    # Positions block
    positions_block: dict[str, object] = {
        "open_count": None,
        "symbols": None,
    }
    if perf_data is not None:
        metrics_raw2 = perf_data.get("metrics")
        metrics2: dict[str, object] = metrics_raw2 if isinstance(metrics_raw2, dict) else {}
        open_raw = metrics2.get("open_positions")
        open_positions: list[object] = open_raw if isinstance(open_raw, list) else []
        symbols = [
            str(p.get("symbol") if isinstance(p, dict) else p)
            for p in open_positions
            if p
        ]
        positions_block["open_count"] = len(symbols)
        positions_block["symbols"] = symbols

    # Fills count
    fills_count: int | None = None
    if fills_data is not None:
        fills_raw = fills_data.get("fills")
        raw_fills: list[object] = fills_raw if isinstance(fills_raw, list) else []
        fills_count = len(raw_fills)
    elif fills_data is None and fills_path.exists() is False:
        fills_count = None  # file missing — unknown

    return {
        "session_date": session_date,
        "generated_at_utc": datetime.now(UTC).isoformat() + "Z",
        "risk": risk_block,
        "performance": perf_block,
        "positions": positions_block,
        "fills_today": fills_count,
    }
