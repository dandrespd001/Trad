"""Offline futures research scaffold generation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.config import ConfigError
from trading_ai.execution.futures_readiness import DEFAULT_CONFIG, build_futures_readiness_report
from trading_ai.execution.paper_common import paper_exit_code, write_json_artifact, write_text_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/futures_research"


class FuturesResearchOperationalError(RuntimeError):
    """Raised when futures research scaffold cannot be generated."""


@dataclass(frozen=True)
class FuturesResearchScaffoldResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_futures_research_scaffold(
    *,
    config: str | Path = DEFAULT_CONFIG,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    as_of_date: str,
    generated_at: str | None = None,
) -> FuturesResearchScaffoldResult:
    report = build_futures_research_scaffold(
        config=config,
        as_of_date=as_of_date,
        generated_at=generated_at,
    )
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "research_manifest.json"
    markdown_path = output_root / "research_manifest.md"
    write_json_artifact(report, output_path)
    write_text_artifact(render_futures_research_scaffold_markdown(report), markdown_path)
    status = str(report.get("status") or "BLOCKED")
    return FuturesResearchScaffoldResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=report,
    )


def build_futures_research_scaffold(
    *,
    config: str | Path = DEFAULT_CONFIG,
    as_of_date: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    try:
        readiness = build_futures_readiness_report(config=config, generated_at=generated_at)
    except ConfigError:
        raise
    except Exception as exc:
        raise FuturesResearchOperationalError(f"cannot build futures readiness evidence: {exc}") from exc
    status = str(readiness.get("status") or "BLOCKED")
    contracts = [
        _research_contract(item) for item in _object_list(readiness.get("contracts")) if isinstance(item, Mapping)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "as_of_date": as_of_date,
        "status": status,
        "config_path": str(config),
        "readiness": {
            "status": status,
            "platform_decision": readiness.get("platform_decision"),
            "summary": readiness.get("summary"),
        },
        "platform_decision": readiness.get("platform_decision"),
        "contracts": contracts,
        "data_requirements": {
            "minimum_history": "research_defined_before_execution",
            "intraday_bars": True,
            "roll_adjusted_history": True,
            "calendar_required": True,
            "cost_model_required": True,
        },
        "warnings": list(_object_list(readiness.get("warnings"))),
        "blockers": list(_object_list(readiness.get("blockers"))),
        "safety": {
            "read_only": True,
            "research_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_enabled": False,
            "live_trading_allowed": False,
            "live_trading_authorized": False,
        },
    }


def render_futures_research_scaffold_markdown(report: Mapping[str, object]) -> str:
    contracts = _object_list(report.get("contracts"))
    lines = [
        "# Futures Research Scaffold",
        "",
        f"Status: **{report.get('status') or 'UNKNOWN'}**",
        f"As of date: `{report.get('as_of_date') or ''}`",
        "",
        "## Contracts",
        "",
        "| Symbol | Tick Size | Tick Value | Session | Roll Rule |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    if contracts:
        for contract in contracts:
            if not isinstance(contract, Mapping):
                continue
            lines.append(
                "| "
                f"`{contract.get('symbol') or ''}` "
                f"| `{contract.get('tick_size') or ''}` "
                f"| `{contract.get('tick_value') or ''}` "
                f"| `{_mapping(contract.get('sessions')).get('session') or ''}` "
                f"| `{_mapping(contract.get('roll_rules')).get('rule') or ''}` |"
            )
    else:
        lines.append("| none |  |  |  |  |")
    lines.extend(["", "Live trading allowed: `False`", "Orders enabled: `False`", ""])
    return "\n".join(lines)


def _research_contract(contract: Mapping[str, object]) -> dict[str, object]:
    return {
        "symbol": contract.get("symbol"),
        "exchange": contract.get("exchange"),
        "name": contract.get("name"),
        "tick_size": contract.get("tick_size"),
        "tick_value": contract.get("tick_value"),
        "margin_placeholder": dict(_mapping(contract.get("margin"))),
        "sessions": dict(_mapping(contract.get("calendar"))),
        "roll_rules": dict(_mapping(contract.get("roll"))),
        "costs": dict(_mapping(contract.get("costs"))),
        "ready": contract.get("ready") is True,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
