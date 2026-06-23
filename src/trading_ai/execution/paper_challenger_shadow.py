"""Shadow-only paper plan for reviewed challenger candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_ai.execution.paper_common import (
    read_json_artifact,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_challenger_shadow"
STATE_READY = "READY_FOR_SHADOW"
STATE_BLOCKED = "BLOCKED"


class PaperChallengerShadowOperationalError(RuntimeError):
    """Raised when a challenger shadow plan cannot be produced."""


@dataclass(frozen=True)
class PaperChallengerShadowResult:
    exit_code: int
    shadow_state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_challenger_shadow_plan(
    *,
    challenger_report: str | Path,
    review_decision: str | Path,
    latest_model: str | Path,
    approved_manifest: str | Path,
    feature_schema: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperChallengerShadowResult:
    payload = build_paper_challenger_shadow_plan(
        challenger_report=challenger_report,
        review_decision=review_decision,
        latest_model=latest_model,
        approved_manifest=approved_manifest,
        feature_schema=feature_schema,
        generated_at=generated_at or _utc_now(),
    )
    output_root = Path(output_dir)
    output_path = output_root / "shadow_plan.json"
    markdown_path = output_root / "shadow_plan.md"
    redacted = _redact_payload(payload)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_paper_challenger_shadow_markdown(redacted), markdown_path)
    state = str(redacted.get("shadow_state") or STATE_BLOCKED)
    return PaperChallengerShadowResult(
        exit_code=0 if state == STATE_READY else 1,
        shadow_state=state,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=redacted,
    )


def build_paper_challenger_shadow_plan(
    *,
    challenger_report: str | Path,
    review_decision: str | Path,
    latest_model: str | Path,
    approved_manifest: str | Path,
    feature_schema: str | Path,
    generated_at: str,
) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    challenger_path = Path(challenger_report)
    decision_path = Path(review_decision)
    latest_path = Path(latest_model)
    manifest_path = Path(approved_manifest)
    schema_path = Path(feature_schema)
    challenger = _read_json(challenger_path, blockers=blockers, code="challenger_report")
    decision = _read_json(decision_path, blockers=blockers, code="review_decision")
    manifest = _read_json(manifest_path, blockers=blockers, code="approved_manifest")
    schema = _read_json(schema_path, blockers=blockers, code="feature_schema")

    if str(challenger.get("status") or "").upper() != "REVIEWABLE":
        blockers.append(_blocker("CRITICAL", "challenger_not_reviewable", "challenger report must be REVIEWABLE"))
    authority = _mapping(challenger.get("authority"))
    if authority.get("mutates_latest_model") is True or authority.get("automatic_champion_replacement") is True:
        blockers.append(_blocker("CRITICAL", "challenger_promotes_model", "challenger report attempts model promotion"))
    decision_value = str(decision.get("decision") or "").upper()
    if decision_value != "DEFER":
        blockers.append(
            _blocker("CRITICAL", "review_decision_not_shadow", "review decision must defer to shadow-only review")
        )
    if not latest_path.exists():
        blockers.append(_blocker("CRITICAL", "missing_latest_model", "latest model artifact is missing", latest_path))
    if not _mapping(manifest).get("dataset_hash"):
        blockers.append(
            _blocker("CRITICAL", "missing_dataset_hash", "approved manifest needs dataset_hash", manifest_path)
        )
    if not (_mapping(schema).get("feature_names") or _mapping(schema).get("columns")):
        blockers.append(
            _blocker("CRITICAL", "missing_feature_schema", "feature schema needs feature_names or columns", schema_path)
        )

    state = STATE_BLOCKED if blockers else STATE_READY
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "shadow_state": state,
        "status": "OK" if state == STATE_READY else "BLOCKED",
        "champion": {
            "path": str(latest_path),
            "sha256": _sha256(latest_path) if latest_path.exists() else None,
            "fixed": True,
        },
        "challenger": {
            "shadow_only": True,
            "promotes_model": False,
            "source_report": str(challenger_path),
            "status": challenger.get("status"),
        },
        "dataset": {
            "manifest": str(manifest_path),
            "dataset_hash": _mapping(manifest).get("dataset_hash"),
            "symbols": _mapping(manifest).get("symbols", []),
        },
        "feature_schema": {"path": str(schema_path), "sha256": _sha256(schema_path) if schema_path.exists() else None},
        "review_decision": {"path": str(decision_path), "decision": decision_value},
        "blockers": _dedupe_blockers(blockers),
        "authority": {
            "mutates_latest_model": False,
            "automatic_champion_replacement": False,
            "orders_submitted": False,
            "broker_execution_allowed": False,
            "human_review_required": True,
        },
        "safety": {
            "paper_only": True,
            "shadow_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_paper_challenger_shadow_markdown(payload: Mapping[str, object]) -> str:
    blockers = _object_list(payload.get("blockers"))
    lines = [
        "# Paper Challenger Shadow Plan",
        "",
        f"Shadow state: **{payload.get('shadow_state') or STATE_BLOCKED}**",
        f"Champion: `{_mapping(payload.get('champion')).get('path') or ''}`",
        "",
        "## Blockers",
        "",
        "| Severity | Code | Message |",
        "| --- | --- | --- |",
    ]
    if blockers:
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                lines.append(
                    "| "
                    f"`{_escape(blocker.get('severity') or '')}` "
                    f"| `{_escape(blocker.get('code') or '')}` "
                    f"| {_escape(blocker.get('message') or '')} |"
                )
    else:
        lines.append("| OK | none | Challenger can be observed in shadow paper only. |")
    lines.extend(["", "Broker execution allowed: `False`", "Mutates latest model: `False`", ""])
    return "\n".join(lines)


def _read_json(path: Path, *, blockers: list[dict[str, object]], code: str) -> dict[str, object]:
    try:
        return read_json_artifact(path)
    except FileNotFoundError:
        blockers.append(_blocker("CRITICAL", f"missing_{code}", f"required artifact is missing: {path}", path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append(_blocker("ERROR", f"invalid_{code}", f"invalid artifact JSON: {exc}", path))
    return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blocker(severity: str, code: str, message: str, source_path: object = None) -> dict[str, object]:
    item: dict[str, object] = {"severity": severity, "code": code, "message": redact_secrets(message, env={})}
    if source_path not in {None, ""}:
        item["source_path"] = str(source_path)
    return item


def _dedupe_blockers(blockers: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        key = (str(blocker.get("code") or ""), str(blocker.get("source_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(blocker))
    return result


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _object_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _redact_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(_redact_value(payload)))


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env={}): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env={})
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
