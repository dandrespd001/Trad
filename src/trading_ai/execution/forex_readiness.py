"""Read-only Forex expansion readiness report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import ConfigError, load_yaml_file
from trading_ai.execution.paper_common import paper_exit_code, write_json_artifact, write_text_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG = "configs/forex_major.yml"
DEFAULT_OUTPUT = "reports/tmp/forex_readiness/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/forex_readiness/latest.md"


class ForexReadinessOperationalError(RuntimeError):
    """Raised when Forex readiness cannot be evaluated."""


@dataclass(frozen=True)
class ForexReadinessReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_forex_readiness_report(
    *,
    config: str | Path = DEFAULT_CONFIG,
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    generated_at: str | None = None,
) -> ForexReadinessReportResult:
    report = build_forex_readiness_report(config=config, generated_at=generated_at)
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_json_artifact(report, output_path)
    write_text_artifact(render_forex_readiness_markdown(report), markdown_path)
    status = str(report["status"])
    return ForexReadinessReportResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_forex_readiness_report(
    *,
    config: str | Path = DEFAULT_CONFIG,
    generated_at: str | None = None,
) -> dict[str, object]:
    config_path = Path(config)
    try:
        payload = load_yaml_file(config_path)
    except ConfigError:
        raise
    except Exception as exc:
        raise ForexReadinessOperationalError(f"cannot load Forex readiness config: {config_path}") from exc

    permissions = _mapping(payload.get("permissions"))
    forex = _mapping(payload.get("forex"))
    pairs = forex.get("pairs")
    if not isinstance(pairs, list):
        pairs = []

    blockers: list[dict[str, object]] = []
    warnings: list[str] = []
    pair_reports: list[dict[str, object]] = []
    for pair in pairs:
        if not isinstance(pair, Mapping):
            blockers.append(_blocker("UNKNOWN", "invalid_pair", "pair entry must be a mapping"))
            continue
        report, pair_blockers = _pair_report(pair)
        pair_reports.append(report)
        blockers.extend(pair_blockers)

    if bool(permissions.get("live_trading_allowed", False)):
        blockers.append(_blocker("permissions", "live_trading_allowed_true", "Forex readiness must remain read-only"))

    platform_decision = _platform_decision_report(
        forex.get("platform_decision"), warnings=warnings, blockers=blockers
    )
    if blockers or not pair_reports:
        status = "BLOCKED"
    elif warnings:
        status = "WARN"
    else:
        status = "OK"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "status": status,
        "config_path": str(config_path),
        "permissions": {"live_trading_allowed": bool(permissions.get("live_trading_allowed", False))},
        "platform_decision": platform_decision,
        "summary": {
            "pair_count": len(pair_reports),
            "ready_pairs": [item["symbol"] for item in pair_reports if item.get("ready") is True],
            "blocked_pairs": [item["symbol"] for item in pair_reports if item.get("ready") is not True],
        },
        "pairs": pair_reports,
        "warnings": warnings,
        "blockers": blockers,
        "decision_note": "Forex integration remains research-only; Alpaca paper is the only operational broker.",
        "safety": {
            "read_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_enabled": False,
            "live_trading_allowed": False,
            "live_trading_authorized": False,
        },
    }


def render_forex_readiness_markdown(report: Mapping[str, object]) -> str:
    summary = _mapping(report.get("summary"))
    platform = _mapping(report.get("platform_decision"))
    raw_blockers = report.get("blockers")
    raw_ready_pairs = summary.get("ready_pairs")
    blockers = raw_blockers if isinstance(raw_blockers, list) else []
    ready_pairs = raw_ready_pairs if isinstance(raw_ready_pairs, list) else []
    lines = [
        "# Forex Readiness",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{report.get('generated_at') or ''}`",
        f"Pair count: `{summary.get('pair_count', 0)}`",
        f"Ready pairs: `{', '.join(str(item) for item in ready_pairs)}`",
        "",
        "## Platform Decision",
        "",
        f"Status: `{platform.get('status') or ''}`",
        f"Selected: `{platform.get('selected') or ''}`",
        f"Read only: `{platform.get('read_only')}`",
        "",
        "## Blockers",
        "",
        "| Pair | Code | Message |",
        "| --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    f"| `{blocker.get('pair') or ''}` | `{blocker.get('code') or ''}` | "
                    f"{blocker.get('message') or ''} |"
                )
    else:
        lines.append("|  | none | No readiness blockers. |")
    lines.extend(["", "Live trading allowed: `False`", "Orders enabled: `False`", ""])
    return "\n".join(lines)


def _pair_report(pair: Mapping[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    symbol = str(pair.get("symbol") or "").upper()
    blockers: list[dict[str, object]] = []
    for field in ("symbol", "base_currency", "quote_currency", "venue", "pip_size", "lot_size"):
        if pair.get(field) in {None, ""}:
            blockers.append(_blocker(symbol, f"missing_{field}", f"pair {field} is required"))
    for field in ("sessions", "liquidity", "costs"):
        if not isinstance(pair.get(field), Mapping) or not pair.get(field):
            blockers.append(_blocker(symbol, f"missing_{field}", f"pair {field} placeholder is required"))
    base = str(pair.get("base_currency") or "").upper()
    quote = str(pair.get("quote_currency") or "").upper()
    if symbol and base and quote and symbol != f"{base}{quote}":
        blockers.append(_blocker(symbol, "symbol_currency_mismatch", "pair symbol must match base and quote"))
    return (
        {
            "symbol": symbol,
            "base_currency": base,
            "quote_currency": quote,
            "venue": pair.get("venue"),
            "pip_size": pair.get("pip_size"),
            "lot_size": pair.get("lot_size"),
            "sessions": dict(_mapping(pair.get("sessions"))),
            "liquidity": dict(_mapping(pair.get("liquidity"))),
            "costs": dict(_mapping(pair.get("costs"))),
            "ready": not blockers,
        },
        blockers,
    )


def _platform_decision_report(
    value: object,
    *,
    warnings: list[str],
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value:
        warnings.append("missing_platform_decision")
        return {
            "status": "MISSING",
            "selected": None,
            "rationale": None,
            "alternatives": [],
            "read_only": True,
            "orders_enabled": False,
        }
    selected = str(value.get("selected") or "")
    read_only = value.get("read_only") is True
    if not selected:
        warnings.append("missing_platform_selected")
    if not read_only:
        blockers.append(
            _blocker("platform_decision", "platform_not_read_only", "Forex platform decision must remain read-only")
        )
    return {
        "status": "DECIDED" if selected and read_only else "INCOMPLETE",
        "selected": selected or None,
        "rationale": value.get("rationale"),
        "alternatives": _string_list(value.get("alternatives")),
        "read_only": read_only,
        "orders_enabled": False,
    }


def _blocker(pair: str, code: str, message: str) -> dict[str, object]:
    return {"severity": "CRITICAL", "pair": pair, "code": code, "message": message}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in {None, ""}]
    return [str(value)]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
