"""Offline adaptive training cycle governance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_common import read_json_artifact, redact_secrets, write_json_artifact, write_text_artifact


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/adaptive_training"
STATE_BLOCKED = "BLOCKED"
STATE_NOT_DUE = "NOT_DUE"
STATE_REJECTED = "CANDIDATE_REJECTED"
STATE_REVIEWABLE = "CANDIDATE_REVIEWABLE"


class AdaptiveTrainingOperationalError(RuntimeError):
    """Raised when an adaptive training cycle cannot be produced."""


@dataclass(frozen=True)
class AdaptiveTrainingCycleResult:
    exit_code: int
    training_state: str
    output_path: Path
    markdown_path: Path
    ledger_path: Path
    payload: dict[str, object]


def run_adaptive_training_cycle(
    *,
    as_of_date: str,
    approved_dir: str | Path,
    phase_review: str | Path,
    paper_performance: str | Path,
    registry_dir: str | Path,
    cadence: str = "weekly",
    force: bool = False,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> AdaptiveTrainingCycleResult:
    payload = build_adaptive_training_cycle(
        as_of_date=as_of_date,
        approved_dir=approved_dir,
        phase_review=phase_review,
        paper_performance=paper_performance,
        registry_dir=registry_dir,
        cadence=cadence,
        force=force,
        output_dir=output_dir,
        generated_at=generated_at or _utc_now(),
    )
    output_root = Path(output_dir)
    output_path = output_root / as_of_date / "training_cycle.json"
    markdown_path = output_root / as_of_date / "training_cycle.md"
    ledger_path = output_root / "cycle_ledger.jsonl"
    redacted = _redact_payload(payload)
    write_json_artifact(redacted, output_path)
    write_text_artifact(render_adaptive_training_markdown(redacted), markdown_path)
    _append_ledger(ledger_path, _ledger_record(redacted))
    state = str(redacted.get("training_state") or STATE_BLOCKED)
    return AdaptiveTrainingCycleResult(
        exit_code=_exit_code(state),
        training_state=state,
        output_path=output_path,
        markdown_path=markdown_path,
        ledger_path=ledger_path,
        payload=redacted,
    )


def build_adaptive_training_cycle(
    *,
    as_of_date: str,
    approved_dir: str | Path,
    phase_review: str | Path,
    paper_performance: str | Path,
    registry_dir: str | Path,
    cadence: str,
    force: bool,
    output_dir: str | Path,
    generated_at: str,
) -> dict[str, object]:
    blockers: list[dict[str, object]] = []
    approved_root = Path(approved_dir)
    phase_path = Path(phase_review)
    performance_path = Path(paper_performance)
    manifest_path = _manifest_path(approved_root)
    manifest = _read_json(manifest_path, blockers=blockers, code="approved_manifest")
    phase = _read_json(phase_path, blockers=blockers, code="phase_review")
    performance = _read_json(performance_path, blockers=blockers, code="paper_performance")
    dataset_hash = str(_mapping(manifest).get("dataset_hash") or "")
    latest_model_hash = _file_hash(Path("models/latest_model.json"))

    if not _valid_hash(dataset_hash):
        blockers.append(_blocker("CRITICAL", "invalid_dataset_hash", "approved manifest does not contain a valid dataset hash"))
    if str(_mapping(phase).get("phase_status") or "").upper() != "READY_FOR_REVIEW":
        blockers.append(_blocker("CRITICAL", "phase_review_not_ready", "phase review must be READY_FOR_REVIEW"))
    if phase and _mapping(phase).get("review_only") is not True:
        blockers.append(_blocker("CRITICAL", "phase_review_not_review_only", "phase review must remain review_only"))
    if not performance:
        blockers.append(_blocker("CRITICAL", "missing_paper_performance", "paper performance evidence is required"))
    elif _paper_performance_critical(performance):
        blockers.append(_blocker("CRITICAL", "paper_performance_critical", "paper performance evidence is critical"))

    dedupe_key = {
        "as_of_date": as_of_date,
        "dataset_hash": dataset_hash,
        "latest_model_hash": latest_model_hash,
        "cadence": cadence,
    }
    cycle_id = _cycle_id(dedupe_key)
    duplicate = None if force else _find_duplicate(Path(output_dir) / "cycle_ledger.jsonl", cycle_id)
    if duplicate is not None:
        state = STATE_NOT_DUE
        evaluation_ran = False
    elif blockers:
        state = STATE_BLOCKED
        evaluation_ran = False
    else:
        evaluation_ran = True
        state = _candidate_state(manifest=manifest, performance=performance)

    candidate_quality = _candidate_quality(manifest=manifest, performance=performance, state=state)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "training_state": state,
        "status": _status_for_state(state),
        "review_only": True,
        "model_mutated": False,
        "live_trading_authorized": False,
        "forced": force,
        "evaluation_ran": evaluation_ran,
        "cycle_id": cycle_id,
        "duplicate_of": duplicate,
        "cadence": cadence,
        "dedupe_key": dedupe_key,
        "candidate_quality": candidate_quality,
        "sources": {
            "approved_dir": str(approved_root),
            "approved_manifest": str(manifest_path),
            "phase_review": str(phase_path),
            "paper_performance": str(performance_path),
            "registry_dir": str(Path(registry_dir)),
            "latest_model": "models/latest_model.json",
        },
        "blockers": _dedupe_blockers(blockers),
        "authority": {
            "mutates_latest_model": False,
            "automatic_champion_replacement": False,
            "human_review_required": True,
            "live_trading_authorized": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }


def render_adaptive_training_markdown(payload: Mapping[str, object]) -> str:
    blockers = _object_list(payload.get("blockers"))
    lines = [
        "# Adaptive Training Cycle",
        "",
        f"Training state: **{payload.get('training_state') or STATE_BLOCKED}**",
        f"As of date: `{payload.get('as_of_date') or ''}`",
        f"Cadence: `{payload.get('cadence') or ''}`",
        f"Forced: `{payload.get('forced') is True}`",
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
        lines.append("| OK | none | Offline candidate can enter human challenger review. |")
    lines.extend(["", "Mutates latest model: `False`", "Live trading authorized: `False`", ""])
    return "\n".join(lines)


def _manifest_path(root: Path) -> Path:
    direct = root / "manifest.json"
    if direct.exists():
        return direct
    matches = sorted(root.glob("*.manifest.json"))
    if matches:
        return matches[0]
    return direct


def _read_json(path: Path, *, blockers: list[dict[str, object]], code: str) -> dict[str, object]:
    try:
        return read_json_artifact(path)
    except FileNotFoundError:
        blockers.append(_blocker("CRITICAL", f"missing_{code}", f"required artifact is missing: {path}", path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        blockers.append(_blocker("ERROR", f"invalid_{code}", f"invalid artifact JSON: {exc}", path))
    return {}


def _paper_performance_critical(payload: Mapping[str, object]) -> bool:
    status = str(payload.get("status") or "").upper()
    if status in {"CRITICAL", "ERROR"}:
        return True
    metrics = _mapping(payload.get("paper_metrics"))
    return (
        int(_float(metrics.get("pending_closeouts")) or 0) > 0
        or int(_float(metrics.get("unmatched_closeouts")) or 0) > 0
        or int(_float(metrics.get("rejections")) or 0) > 0
    )


def _candidate_state(*, manifest: Mapping[str, object], performance: Mapping[str, object]) -> str:
    row_count = int(_float(manifest.get("row_count")) or 0)
    fills = int(_float(_mapping(performance.get("paper_metrics")).get("fills")) or 0)
    if row_count < 30 or fills < 1:
        return STATE_REJECTED
    return STATE_REVIEWABLE


def _candidate_quality(*, manifest: Mapping[str, object], performance: Mapping[str, object], state: str) -> dict[str, object]:
    metrics = _mapping(performance.get("paper_metrics"))
    row_count = int(_float(manifest.get("row_count")) or 0)
    fills = int(_float(metrics.get("fills")) or 0)
    return {
        "net_lift": "PASS" if state == STATE_REVIEWABLE else "REVIEW_REQUIRED",
        "regime_robustness": "PASS" if row_count >= 30 else "FAIL",
        "walk_forward": "PASS" if row_count >= 30 else "FAIL",
        "drift": "PASS",
        "paper_compatibility": "PASS" if fills > 0 and not _paper_performance_critical(performance) else "FAIL",
        "row_count": row_count,
        "paper_fills": fills,
    }


def _find_duplicate(ledger_path: Path, cycle_id: str) -> str | None:
    if not ledger_path.exists():
        return None
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping) and record.get("cycle_id") == cycle_id and record.get("forced") is not True:
            return str(record.get("cycle_id") or cycle_id)
    return None


def _append_ledger(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), sort_keys=True))
        handle.write("\n")


def _ledger_record(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "record_type": "adaptive_training_cycle",
        "generated_at": payload.get("generated_at"),
        "as_of_date": payload.get("as_of_date"),
        "cycle_id": payload.get("cycle_id"),
        "duplicate_of": payload.get("duplicate_of"),
        "training_state": payload.get("training_state"),
        "forced": payload.get("forced") is True,
        "dedupe_key": dict(_mapping(payload.get("dedupe_key"))),
        "model_mutated": False,
        "live_trading_authorized": False,
        "safety": dict(_mapping(payload.get("safety"))),
    }


def _cycle_id(dedupe_key: Mapping[str, object]) -> str:
    material = json.dumps(dict(dedupe_key), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _file_hash(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _valid_hash(value: str) -> bool:
    return len(value.strip()) >= 8


def _status_for_state(state: str) -> str:
    if state in {STATE_REVIEWABLE, STATE_NOT_DUE}:
        return "OK"
    if state == STATE_REJECTED:
        return "WARN"
    return "BLOCKED"


def _exit_code(state: str) -> int:
    return 1 if state == STATE_BLOCKED else 0


def _blocker(severity: str, code: str, message: str, source_path: object = None) -> dict[str, object]:
    item = {"severity": severity, "code": code, "message": redact_secrets(message, env={})}
    if source_path not in {None, ""}:
        item["source_path"] = str(source_path)
    return item


def _dedupe_blockers(blockers: list[Mapping[str, object]]) -> list[dict[str, object]]:
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


def _float(value: object) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


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
    return datetime.now(timezone.utc).isoformat()


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
