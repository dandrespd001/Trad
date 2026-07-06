"""Build versioned AI-derived feature columns from governed local events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trading_ai.ai.events import PROVIDER_EXTERNAL_API, PROVIDER_MANUAL, read_ai_events
from trading_ai.config import ConfigError, load_universe_config, load_yaml_file
from trading_ai.data.io import read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/ai_features"
AI_FEATURE_COLUMNS = (
    "ai_sentiment_1d",
    "ai_risk_1d",
    "ai_event_count_1d",
    "ai_confidence_1d",
    "ai_sentiment_5d",
    "ai_risk_5d",
)


class AiFeatureOperationalError(RuntimeError):
    """Raised when AI features cannot be produced."""


@dataclass(frozen=True)
class AiFeatureBuildResult:
    exit_code: int
    status: str
    features_path: Path
    manifest_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_ai_feature_build(
    *,
    as_of_date: str,
    features: str | Path,
    events: str | Path,
    config: str | Path,
    provider_config: str | Path,
    provider: str = PROVIDER_MANUAL,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> AiFeatureBuildResult:
    output_root = Path(output_dir) / as_of_date
    features_path = output_root / "ai_features.csv"
    manifest_path = output_root / "ai_features_manifest.json"
    markdown_path = output_root / "ai_features.md"
    output_root.mkdir(parents=True, exist_ok=True)
    blockers = _provider_blockers(provider=provider, provider_config=provider_config)
    rows: list[dict[str, object]] = []
    status = "BLOCKED" if blockers else "OK"
    if not blockers:
        universe = load_universe_config(config)
        rows = build_ai_feature_rows(
            feature_rows=read_records(features),
            events=read_ai_events(events),
            allowlist=universe.symbols,
            as_of_date=as_of_date,
        )
        write_records(rows, features_path)
    else:
        base_rows = read_records(features)
        rows = [dict(row) for row in base_rows]
        if rows:
            write_records(rows, features_path)
    manifest = _manifest(
        status=status,
        as_of_date=as_of_date,
        rows=rows,
        features=features,
        events=events,
        output=features_path,
        provider=provider,
        blockers=blockers,
    )
    write_json_artifact(manifest, manifest_path)
    write_text_artifact(_render_markdown(manifest), markdown_path)
    return AiFeatureBuildResult(
        exit_code=0 if status == "OK" else 1,
        status=status,
        features_path=features_path,
        manifest_path=manifest_path,
        markdown_path=markdown_path,
        payload=manifest,
    )


def build_ai_feature_rows(
    *,
    feature_rows: list[dict[str, object]],
    events: list[dict[str, object]],
    allowlist: tuple[str, ...],
    as_of_date: str,
) -> list[dict[str, object]]:
    allowed = {symbol.upper() for symbol in allowlist}
    parsed_as_of = date.fromisoformat(as_of_date)
    valid_events = [
        event
        for event in events
        if str(event.get("symbol") or "").upper() in allowed
        and str(event.get("llm_authority") or "none") == "none"
        and _event_date(event, "timestamp") is not None
        and _event_date(event, "valid_until") is not None
        and _event_date(event, "timestamp") <= parsed_as_of
    ]
    output: list[dict[str, object]] = []
    for row in feature_rows:
        enriched = dict(row)
        row_date = date.fromisoformat(str(row["timestamp"]))
        symbol = str(row["symbol"]).upper()
        active = [
            event
            for event in valid_events
            if str(event.get("symbol") or "").upper() == symbol
            and _event_date(event, "timestamp") <= row_date <= _event_date(event, "valid_until")
        ]
        aggregates = _aggregate(active)
        enriched.update(aggregates)
        output.append(enriched)
    return output


def _aggregate(events: list[Mapping[str, object]]) -> dict[str, object]:
    if not events:
        return {
            "ai_sentiment_1d": 0.0,
            "ai_risk_1d": 0.0,
            "ai_event_count_1d": 0,
            "ai_confidence_1d": 0.0,
            "ai_sentiment_5d": 0.0,
            "ai_risk_5d": 0.0,
        }
    sentiment = _mean([_float(event.get("sentiment_score")) for event in events])
    risk = _mean([_float(event.get("event_risk_score")) for event in events])
    confidence = _mean([_float(event.get("confidence")) for event in events])
    return {
        "ai_sentiment_1d": sentiment,
        "ai_risk_1d": risk,
        "ai_event_count_1d": len(events),
        "ai_confidence_1d": confidence,
        "ai_sentiment_5d": sentiment,
        "ai_risk_5d": risk,
    }


def _provider_blockers(*, provider: str, provider_config: str | Path) -> list[str]:
    try:
        payload = load_yaml_file(provider_config)
    except ConfigError as exc:
        return [f"invalid_provider_config:{exc}"]
    provider_payload = payload.get(provider)
    if not isinstance(provider_payload, Mapping):
        return [f"provider_not_configured:{provider}"]
    if provider == PROVIDER_EXTERNAL_API and provider_payload.get("enabled") is not True:
        return ["external_api_disabled"]
    if provider != PROVIDER_MANUAL:
        return [f"provider_not_available:{provider}"]
    if provider_payload.get("enabled") is not True:
        return [f"provider_disabled:{provider}"]
    return []


def _manifest(
    *,
    status: str,
    as_of_date: str,
    rows: list[dict[str, object]],
    features: str | Path,
    events: str | Path,
    output: Path,
    provider: str,
    blockers: list[str],
) -> dict[str, object]:
    dataset_manifest = build_dataset_manifest(rows, source=str(output)) if rows else {"dataset_hash": None}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "as_of_date": as_of_date,
        "features": str(Path(features)),
        "events": str(Path(events)),
        "output": str(output),
        "provider": provider,
        "row_count": len(rows),
        "dataset_hash": dataset_manifest.get("dataset_hash"),
        "columns": list(AI_FEATURE_COLUMNS),
        "blockers": blockers,
        "authority": {"llm_authority": "none", "orders_submitted": False, "risk_changed": False},
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
            "external_api_used": False,
            "external_api_requested": provider == PROVIDER_EXTERNAL_API,
        },
    }


def _render_markdown(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# AI Features",
            "",
            f"Status: **{payload.get('status')}**",
            f"As of date: `{payload.get('as_of_date')}`",
            f"Rows: `{payload.get('row_count')}`",
            "",
        ]
    )


def _event_date(event: Mapping[str, object], key: str) -> date | None:
    try:
        return date.fromisoformat(str(event.get(key) or ""))
    except ValueError:
        return None


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
