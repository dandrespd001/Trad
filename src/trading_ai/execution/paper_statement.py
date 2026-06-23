"""Manual broker statement validation and normalization for paper trading."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from trading_ai.execution.paper_common import paper_exit_code, redact_secrets, write_json_artifact, write_text_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_statements"
REQUIRED_FIELDS = (
    "client_order_id",
    "symbol",
    "side",
    "quantity",
    "filled_avg_price",
    "filled_at",
    "realized_pnl",
)
FIELD_ALIASES = {
    "client_order_id": (
        "client_order_id",
        "clientOrderId",
        "client order id",
        "client-order-id",
        "order_id",
        "order id",
        "clordid",
        "id",
    ),
    "symbol": ("symbol", "asset", "ticker", "contract", "instrument"),
    "side": ("side", "order_side", "order side", "action", "transaction type", "buy sell"),
    "quantity": (
        "quantity",
        "qty",
        "filled_quantity",
        "filled quantity",
        "filled_qty",
        "filled qty",
        "fill quantity",
        "shares",
        "contracts",
    ),
    "filled_avg_price": (
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
    ),
    "filled_at": (
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
    ),
    "realized_pnl": (
        "realized_pnl",
        "realized pnl",
        "realized p&l",
        "realized p/l",
        "pnl",
        "p&l",
        "profit_loss",
        "profit loss",
        "profit/loss",
    ),
}
NUMERIC_FIELDS = ("quantity", "filled_avg_price", "realized_pnl")


class PaperStatementOperationalError(RuntimeError):
    """Raised when a statement cannot be validated or written."""


@dataclass(frozen=True)
class PaperStatementValidateResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_statement_validate(
    *,
    statement: str | Path,
    as_of_date: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperStatementValidateResult:
    report = build_paper_statement_validation(
        statement=statement,
        as_of_date=as_of_date,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "statement.normalized.json"
    markdown_path = output_root / "statement.normalized.md"
    write_json_artifact(report, output_path)
    write_text_artifact(render_paper_statement_markdown(report), markdown_path)
    status = str(report.get("status") or "ERROR")
    return PaperStatementValidateResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_paper_statement_validation(
    *,
    statement: str | Path,
    as_of_date: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    path = Path(statement)
    errors: list[dict[str, object]] = []
    fills: list[dict[str, object]] = []
    try:
        rows = _read_statement_rows(path)
    except (OSError, json.JSONDecodeError, csv.Error, ValueError) as exc:
        rows = []
        errors.append(_error("invalid_statement", str(exc), source_path=path))
    warnings: list[dict[str, object]] = []
    seen_client_ids: set[str] = set()
    for index, row in enumerate(rows):
        normalized = _normalize_statement_row(row)
        row_errors, row_warnings = _row_issues(normalized, row, index=index, as_of_date=as_of_date)
        client_order_id = str(normalized.get("client_order_id") or "")
        if client_order_id:
            if client_order_id in seen_client_ids:
                row_errors.append(_error("duplicate_client_order_id", "duplicate client_order_id", row=index))
            seen_client_ids.add(client_order_id)
        errors.extend(row_errors)
        warnings.extend(row_warnings)
        fills.append(normalized)
    status = "ERROR" if errors else "WARN" if warnings else "OK"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": status,
        "as_of_date": as_of_date,
        "source_path": str(path),
        "fill_count": len(fills),
        "fills": fills,
        "errors": errors,
        "warnings": warnings,
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_statement_markdown(report: Mapping[str, object]) -> str:
    raw_errors = report.get("errors")
    raw_warnings = report.get("warnings")
    errors = raw_errors if isinstance(raw_errors, list) else []
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    lines = [
        "# Paper Broker Statement",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        f"Fill count: `{report.get('fill_count') or 0}`",
        "",
        "## Errors",
        "",
        "| Code | Message |",
        "| --- | --- |",
    ]
    if errors:
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"| `{_escape(error.get('code') or '')}` | {_escape(error.get('message') or '')} |")
    else:
        lines.append("| none | Statement normalized successfully. |")
    lines.extend(["", "## Warnings", "", "| Code | Message |", "| --- | --- |"])
    if warnings:
        for warning in warnings:
            if isinstance(warning, Mapping):
                lines.append(f"| `{_escape(warning.get('code') or '')}` | {_escape(warning.get('message') or '')} |")
    else:
        lines.append("| none | No statement warnings. |")
    lines.extend(["", "Live trading allowed: `False`", "Credentials read: `False`", ""])
    return "\n".join(lines)


def _read_statement_rows(path: Path) -> list[Mapping[str, object]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        rows = payload.get("fills") or payload.get("orders") or payload.get("rows")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("statement must contain a fills list")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"statement row {index} must be an object")
    return rows


def _normalize_statement_row(row: Mapping[str, object]) -> dict[str, object]:
    raw = {str(key): _redact_raw_value(value) for key, value in row.items()}
    return {
        "client_order_id": _field_value(row, "client_order_id"),
        "symbol": _upper_or_none(_field_value(row, "symbol")),
        "side": _lower_or_none(_field_value(row, "side")),
        "quantity": _float_or_none(_field_value(row, "quantity")),
        "filled_avg_price": _float_or_none(_field_value(row, "filled_avg_price")),
        "filled_at": _field_value(row, "filled_at"),
        "realized_pnl": _float_or_none(_field_value(row, "realized_pnl")),
        "raw": raw,
    }


def _row_issues(
    row: Mapping[str, object],
    raw_row: Mapping[str, object],
    *,
    index: int,
    as_of_date: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    for field in REQUIRED_FIELDS:
        raw_value = _field_value(raw_row, field)
        if raw_value in {None, ""}:
            errors.append(_error(f"missing_{field}", f"{field} is required", row=index))
        elif field in NUMERIC_FIELDS and row.get(field) is None:
            errors.append(_error(f"invalid_{field}", f"{field} must be numeric", row=index))
        elif field == "filled_at":
            parsed = _parse_datetime(raw_value)
            if parsed is None:
                errors.append(_error("invalid_filled_at", "filled_at must be an ISO-like date/time", row=index))
            else:
                if parsed.tzinfo is None:
                    warnings.append(
                        _warning("filled_at_missing_timezone", "filled_at has no timezone offset", row=index)
                    )
                as_of = _parse_date(as_of_date)
                if as_of is not None and parsed.date() != as_of:
                    warnings.append(
                        _warning(
                            "filled_at_outside_as_of_date",
                            "filled_at date does not match as_of_date",
                            row=index,
                        )
                    )
    return errors, warnings


def _error(code: str, message: str, *, row: int | None = None, source_path: object = None) -> dict[str, object]:
    payload: dict[str, object] = {"code": code, "message": message}
    if row is not None:
        payload["row"] = row
    if source_path not in {None, ""}:
        payload["source_path"] = str(source_path)
    return payload


def _warning(code: str, message: str, *, row: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"code": code, "message": message}
    if row is not None:
        payload["row"] = row
    return payload


def _first_value(row: Mapping[str, object], *keys: str) -> object:
    normalized = {_normalize_key(key): value for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value in {None, ""}:
            value = normalized.get(_normalize_key(key))
        if value not in {None, ""}:
            return value
    return None


def _field_value(row: Mapping[str, object], field: str) -> object:
    return _first_value(row, *FIELD_ALIASES[field])


def _float_or_none(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _upper_or_none(value: object) -> str | None:
    return str(value).upper() if value not in {None, ""} else None


def _lower_or_none(value: object) -> str | None:
    return str(value).lower() if value not in {None, ""} else None


def _redact_raw_value(value: object) -> object:
    if isinstance(value, str):
        redacted = redact_secrets(value, env={})
        return "[redacted]" if redacted != value and "[redacted" in redacted else redacted
    return value


def _parse_datetime(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: object) -> date | None:
    if value in {None, ""}:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
