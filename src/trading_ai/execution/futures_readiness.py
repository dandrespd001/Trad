"""Read-only futures expansion readiness report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.config import ConfigError, load_yaml_file
from trading_ai.execution.paper_common import paper_exit_code, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG = "configs/futures_micro.yml"
DEFAULT_OUTPUT = "reports/tmp/futures_readiness/latest.json"
DEFAULT_MARKDOWN_OUTPUT = "reports/tmp/futures_readiness/latest.md"


class FuturesReadinessOperationalError(RuntimeError):
    """Raised when futures readiness cannot be evaluated."""


@dataclass(frozen=True)
class FuturesReadinessReportResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_futures_readiness_report(
    *,
    config: str | Path = DEFAULT_CONFIG,
    output: str | Path = DEFAULT_OUTPUT,
    markdown_output: str | Path = DEFAULT_MARKDOWN_OUTPUT,
    generated_at: str | None = None,
) -> FuturesReadinessReportResult:
    report = build_futures_readiness_report(config=config, generated_at=generated_at)
    output_path = Path(output)
    markdown_path = Path(markdown_output)
    write_json_artifact(report, output_path)
    write_text_artifact(render_futures_readiness_markdown(report), markdown_path)
    status = str(report["status"])
    return FuturesReadinessReportResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_futures_readiness_report(
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
        raise FuturesReadinessOperationalError(f"cannot load futures readiness config: {config_path}") from exc
    permissions = _mapping(payload.get("permissions"))
    futures = _mapping(payload.get("futures"))
    contracts = futures.get("contracts")
    if not isinstance(contracts, list):
        contracts = []
    blockers: list[dict[str, object]] = []
    warnings: list[str] = []
    contract_reports: list[dict[str, object]] = []
    for contract in contracts:
        if not isinstance(contract, Mapping):
            blockers.append(_blocker("UNKNOWN", "invalid_contract", "contract entry must be a mapping"))
            continue
        report, contract_blockers = _contract_report(contract)
        contract_reports.append(report)
        blockers.extend(contract_blockers)
    if bool(permissions.get("live_trading_allowed", False)):
        blockers.append(_blocker("permissions", "live_trading_allowed_true", "futures readiness must remain read-only"))
    platform_decision = _platform_decision_report(futures.get("platform_decision"), warnings=warnings, blockers=blockers)
    if blockers or not contract_reports:
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
            "contract_count": len(contract_reports),
            "ready_contracts": [item["symbol"] for item in contract_reports if item.get("ready") is True],
            "blocked_contracts": [item["symbol"] for item in contract_reports if item.get("ready") is not True],
        },
        "contracts": contract_reports,
        "warnings": warnings,
        "blockers": blockers,
        "decision_note": "LEAN/IBKR integration remains research-only; Alpaca paper is the only operational broker.",
        "safety": {
            "read_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_enabled": False,
            "live_trading_allowed": False,
            "live_trading_authorized": False,
        },
    }


def render_futures_readiness_markdown(report: Mapping[str, object]) -> str:
    summary = _mapping(report.get("summary"))
    platform = _mapping(report.get("platform_decision"))
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    lines = [
        "# Futures Readiness",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"Generated at: `{report.get('generated_at') or ''}`",
        f"Contract count: `{summary.get('contract_count', 0)}`",
        f"Ready contracts: `{', '.join(str(item) for item in summary.get('ready_contracts', []))}`",
        "",
        "## Platform Decision",
        "",
        f"Status: `{platform.get('status') or ''}`",
        f"Selected: `{platform.get('selected') or ''}`",
        f"Read only: `{platform.get('read_only')}`",
        "",
        "## Blockers",
        "",
        "| Contract | Code | Message |",
        "| --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    f"| `{blocker.get('contract') or ''}` | `{blocker.get('code') or ''}` | {blocker.get('message') or ''} |"
                )
    else:
        lines.append("|  | none | No readiness blockers. |")
    lines.extend(["", "Live trading allowed: `False`", "Orders enabled: `False`", ""])
    return "\n".join(lines)


def _contract_report(contract: Mapping[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    symbol = str(contract.get("symbol") or "").upper()
    blockers: list[dict[str, object]] = []
    required_mapping_fields = ("margin", "calendar", "roll", "costs")
    if not symbol:
        blockers.append(_blocker(symbol, "missing_symbol", "contract symbol is required"))
    for field in ("exchange", "tick_size", "tick_value"):
        if contract.get(field) in {None, ""}:
            blockers.append(_blocker(symbol, f"missing_{field}", f"contract {field} is required"))
    for field in required_mapping_fields:
        if not isinstance(contract.get(field), Mapping) or not contract.get(field):
            blockers.append(_blocker(symbol, f"missing_{field}", f"contract {field} placeholder is required"))
    return (
        {
            "symbol": symbol,
            "exchange": contract.get("exchange"),
            "name": contract.get("name"),
            "tick_size": contract.get("tick_size"),
            "tick_value": contract.get("tick_value"),
            "margin": dict(_mapping(contract.get("margin"))),
            "calendar": dict(_mapping(contract.get("calendar"))),
            "roll": dict(_mapping(contract.get("roll"))),
            "costs": dict(_mapping(contract.get("costs"))),
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
        blockers.append(_blocker("platform_decision", "platform_not_read_only", "futures platform decision must remain read-only"))
    return {
        "status": "DECIDED" if selected and read_only else "INCOMPLETE",
        "selected": selected or None,
        "rationale": value.get("rationale"),
        "alternatives": _string_list(value.get("alternatives")),
        "read_only": read_only,
        "orders_enabled": False,
    }


def _blocker(contract: str, code: str, message: str) -> dict[str, object]:
    return {"severity": "CRITICAL", "contract": contract, "code": code, "message": message}


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
    return datetime.now(timezone.utc).isoformat()
