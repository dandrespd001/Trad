"""Governed local AI event extraction for signal features."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from trading_ai.config import ConfigError, load_universe_config, load_yaml_file
from trading_ai.execution.paper_common import redact_secrets, write_json_artifact, write_text_artifact

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/ai_events"
STATUS_OK = "OK"
STATUS_BLOCKED = "BLOCKED"
PROVIDER_MANUAL = "manual_jsonl"
PROVIDER_EXTERNAL_API = "external_api"


class AiEventOperationalError(RuntimeError):
    """Raised when governed AI events cannot be produced."""


@dataclass(frozen=True)
class AiEventExtractionResult:
    exit_code: int
    status: str
    events_path: Path
    manifest_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_ai_event_extract(
    *,
    as_of_date: str,
    input_jsonl: str | Path,
    config: str | Path,
    provider_config: str | Path,
    provider: str = PROVIDER_MANUAL,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> AiEventExtractionResult:
    output_root = Path(output_dir) / as_of_date
    events_path = output_root / "events.jsonl"
    manifest_path = output_root / "events_manifest.json"
    markdown_path = output_root / "events.md"
    output_root.mkdir(parents=True, exist_ok=True)
    blockers = _provider_blockers(provider=provider, provider_config=provider_config)
    events: list[dict[str, object]] = []
    if not blockers:
        universe = load_universe_config(config)
        events, blockers = _read_valid_events(
            input_jsonl=input_jsonl,
            allowlist=universe.symbols,
            as_of_date=as_of_date,
        )
    status = STATUS_OK if events and not blockers else STATUS_BLOCKED
    _write_jsonl(events_path, events if status == STATUS_OK else [])
    manifest = _manifest(
        status=status,
        as_of_date=as_of_date,
        input_jsonl=input_jsonl,
        events_path=events_path,
        provider=provider,
        event_count=len(events) if status == STATUS_OK else 0,
        blockers=blockers,
    )
    write_json_artifact(manifest, manifest_path)
    write_text_artifact(_render_markdown(manifest), markdown_path)
    return AiEventExtractionResult(
        exit_code=0 if status == STATUS_OK else 1,
        status=status,
        events_path=events_path,
        manifest_path=manifest_path,
        markdown_path=markdown_path,
        payload=manifest,
    )


def read_ai_events(path: str | Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AiEventOperationalError(f"invalid_jsonl_line:{line_number}:{exc}") from exc
        if not isinstance(payload, dict):
            raise AiEventOperationalError(f"event_line_not_object:{line_number}")
        events.append(payload)
    return events


def _read_valid_events(
    *,
    input_jsonl: str | Path,
    allowlist: tuple[str, ...],
    as_of_date: str,
) -> tuple[list[dict[str, object]], list[str]]:
    allowed = {symbol.upper() for symbol in allowlist}
    parsed_as_of = date.fromisoformat(as_of_date)
    events: list[dict[str, object]] = []
    blockers: list[str] = []
    source_path = Path(input_jsonl)
    for line_number, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            blockers.append(f"invalid_json:{line_number}")
            continue
        if not isinstance(raw, dict):
            blockers.append(f"event_not_object:{line_number}")
            continue
        event = _normalize_event(raw)
        blockers.extend(_event_blockers(event, line_number=line_number, allowlist=allowed, as_of_date=parsed_as_of))
        events.append(event)
    if blockers:
        return [], blockers
    if not events:
        return [], ["no_valid_events"]
    return events, []


def _normalize_event(raw: Mapping[str, object]) -> dict[str, object]:
    canonical = {
        "timestamp": str(raw.get("timestamp") or ""),
        "symbol": str(raw.get("symbol") or "").upper(),
        "source": str(raw.get("source") or "manual_jsonl"),
        "event_type": str(raw.get("event_type") or "unknown"),
        "sentiment_score": _float(raw.get("sentiment_score")),
        "event_risk_score": _float(raw.get("event_risk_score")),
        "confidence": _float(raw.get("confidence")),
        "valid_until": str(raw.get("valid_until") or raw.get("timestamp") or ""),
        "model_id": str(raw.get("model_id") or "manual_jsonl"),
        "source_sha256": str(raw.get("source_sha256") or _stable_hash(raw)),
        "llm_authority": str(raw.get("llm_authority") or "none"),
    }
    thesis = raw.get("thesis")
    if thesis not in {None, ""}:
        canonical["thesis"] = redact_secrets(str(thesis), env={})
    return canonical


def _event_blockers(
    event: Mapping[str, object],
    *,
    line_number: int,
    allowlist: set[str],
    as_of_date: date,
) -> list[str]:
    blockers: list[str] = []
    symbol = str(event.get("symbol") or "")
    if symbol not in allowlist:
        blockers.append(f"symbol_not_allowlisted:{line_number}")
    if event.get("llm_authority") != "none":
        blockers.append(f"llm_authority_not_none:{line_number}")
    timestamp = _parse_date(event.get("timestamp"))
    valid_until = _parse_date(event.get("valid_until"))
    if timestamp is None:
        blockers.append(f"invalid_timestamp:{line_number}")
    elif timestamp > as_of_date:
        blockers.append(f"event_timestamp_after_as_of_date:{line_number}")
    if valid_until is None:
        blockers.append(f"invalid_valid_until:{line_number}")
    elif timestamp is not None and valid_until < timestamp:
        blockers.append(f"valid_until_before_timestamp:{line_number}")
    for key, lower, upper in (
        ("sentiment_score", -1.0, 1.0),
        ("event_risk_score", 0.0, 1.0),
        ("confidence", 0.0, 1.0),
    ):
        value = event.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not lower <= float(value) <= upper:
            blockers.append(f"invalid_{key}:{line_number}")
    return blockers


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
    input_jsonl: str | Path,
    events_path: Path,
    provider: str,
    event_count: int,
    blockers: list[str],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "as_of_date": as_of_date,
        "provider": provider,
        "input_jsonl": str(Path(input_jsonl)),
        "events_path": str(events_path),
        "event_count": event_count,
        "blockers": blockers,
        "authority": {"llm_authority": "none", "orders_submitted": False, "risk_changed": False},
        "safety": _safety(provider=provider),
    }


def _safety(*, provider: str) -> dict[str, object]:
    return {
        "paper_only": True,
        "broker_client_built": False,
        "credentials_read": False,
        "orders_submitted": False,
        "live_trading_authorized": False,
        "live_trading_allowed": False,
        "external_api_used": False,
        "external_api_requested": provider == PROVIDER_EXTERNAL_API,
    }


def _render_markdown(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# AI Events",
            "",
            f"Status: **{payload.get('status')}**",
            f"As of date: `{payload.get('as_of_date')}`",
            f"Events: `{payload.get('event_count')}`",
            "",
        ]
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True))
            handle.write("\n")


def _stable_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _float(value: object) -> float:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return float("nan")


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
