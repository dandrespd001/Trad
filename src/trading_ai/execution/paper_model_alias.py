"""Human-gated paper-only model alias for adaptive routing."""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact
from trading_ai.models.baseline import validate_logistic_model_payload


DEFAULT_OUTPUT_DIR = "reports/tmp/paper_model_alias"
STATE_ACTIVE = "ACTIVE_PAPER_ALIAS"
STATE_CHAMPION_ONLY = "CHAMPION_ONLY"
STATE_BLOCKED = "BLOCKED"
PAPER_MODEL_ALIAS_SIGNING_KEY_ENV = "PAPER_MODEL_ALIAS_SIGNING_KEY"
ALIAS_SIGNATURE_FIELD = "alias_signature"
ALIAS_SIGNATURE_VERSION_FIELD = "alias_signature_version"
ALIAS_SIGNATURE_VERSION = "hmac-sha256-v1"


@dataclass(frozen=True)
class PaperModelAliasResult:
    exit_code: int
    alias_state: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_model_alias_decision(
    *,
    shadow_scorecard: str | Path,
    review_decision: str | Path,
    candidate_model_run: str | Path,
    latest_model: str | Path,
    reviewer: str,
    reason: str,
    ttl_days: int = 30,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperModelAliasResult:
    output_root = Path(output_dir)
    output_path = output_root / "current.json"
    markdown_path = output_root / "current.md"
    model_path = output_root / "paper_model.json"
    blockers: list[str] = []
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    try:
        scorecard = read_json_artifact(shadow_scorecard)
        decision = read_json_artifact(review_decision)
        candidate = read_json_artifact(candidate_model_run)
        latest_hash = _sha256(Path(latest_model))
        model_payload = _candidate_model(candidate)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        payload = _payload(generated, STATE_BLOCKED, blockers=[str(exc)], model_path=None, latest_model=latest_model, latest_hash=None, reviewer=reviewer, reason=reason, ttl_days=ttl_days)
        return _write(payload, output_path, markdown_path)

    if str(scorecard.get("scorecard_state") or "").upper() != "READY_FOR_PAPER_ALIAS":
        blockers.append("shadow_scorecard_not_ready")
    if str(decision.get("decision") or "").upper() != "APPROVE_FOR_NEXT_PAPER_CYCLE":
        blockers.append("review_decision_not_approved")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    if blockers:
        payload = _payload(generated, STATE_CHAMPION_ONLY, blockers=blockers, model_path=None, latest_model=latest_model, latest_hash=latest_hash, reviewer=reviewer, reason=reason, ttl_days=ttl_days)
        return _write(payload, output_path, markdown_path)

    model_path = model_path.resolve()
    write_json_artifact(model_payload, model_path)
    alias_hash = _sha256(model_path)
    payload = _payload(
        generated,
        STATE_ACTIVE,
        blockers=[],
        model_path=model_path,
        latest_model=latest_model,
        latest_hash=latest_hash,
        reviewer=reviewer,
        reason=reason,
        ttl_days=ttl_days,
    )
    payload["active_model_sha256"] = alias_hash
    payload["alias_hash"] = alias_hash
    payload["candidate_model_run"] = str(Path(candidate_model_run))
    signature = _sign_alias_payload(payload, _alias_signing_secret())
    if signature is not None:
        payload[ALIAS_SIGNATURE_FIELD] = signature
        payload[ALIAS_SIGNATURE_VERSION_FIELD] = ALIAS_SIGNATURE_VERSION
    return _write(payload, output_path, markdown_path)


def resolve_paper_model_route(
    *,
    signal_model: str | Path,
    paper_model_alias: str | Path | None,
    as_of_date: str,
) -> dict[str, object]:
    if paper_model_alias is None:
        return {"route_state": "CHAMPION", "active_model_path": str(Path(signal_model)), "alias_hash": None, "reason": "paper_model_alias_not_provided"}
    alias_path = Path(paper_model_alias).expanduser()
    if not alias_path.is_file():
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": None, "reason": f"invalid_alias_path:{alias_path}"}
    try:
        alias = read_json_artifact(alias_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": None, "reason": f"invalid_alias:{exc}"}

    signing_secret = _alias_signing_secret()
    signature_version = str(alias.get(ALIAS_SIGNATURE_VERSION_FIELD) or "").strip()
    if str(alias.get(ALIAS_SIGNATURE_FIELD) or "").strip() and signature_version != ALIAS_SIGNATURE_VERSION:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_signature_version_invalid"}
    if signing_secret is not None and not _alias_signature_valid(alias, signing_secret):
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_signature_invalid"}
    if str(alias.get(ALIAS_SIGNATURE_FIELD) or "").strip() and signing_secret is None:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_signature_present_without_key"}

    if str(alias.get("alias_state") or "").upper() != STATE_ACTIVE:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_not_active"}
    expires = str(alias.get("expires_on") or "")
    if expires and not _is_iso_date(expires):
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_expiry_not_iso_date"}
    if expires and expires < as_of_date:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_expired"}
    model_path = Path(str(alias.get("active_model_path") or ""))
    model_path = model_path.expanduser()
    if not model_path.exists():
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": alias.get("active_model_sha256"), "reason": "alias_model_missing"}
    expected_hash = str(alias.get("active_model_sha256") or "")
    if len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": expected_hash, "reason": "alias_model_hash_invalid"}
    actual_hash = _sha256(model_path)
    if expected_hash and actual_hash != expected_hash:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": expected_hash, "reason": "alias_model_hash_mismatch"}
    safety = _mapping(alias.get("safety"))
    if safety.get("live_trading_authorized") is True or safety.get("orders_submitted") is True:
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": expected_hash, "reason": "alias_safety_violation"}
    if _alias_governance_reason(alias):
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": expected_hash, "reason": "alias_governance_invalid"}
    try:
        model_payload = read_json_artifact(model_path)
        validate_logistic_model_payload(model_payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return {"route_state": "BLOCKED", "active_model_path": None, "alias_hash": expected_hash, "reason": "alias_model_invalid"}
    return {"route_state": "PAPER_ALIAS", "active_model_path": str(model_path), "alias_hash": actual_hash, "reason": "active_paper_alias"}


def _candidate_model(payload: Mapping[str, object]) -> Mapping[str, object]:
    model = payload.get("model")
    if not isinstance(model, Mapping):
        model = payload.get("serialized_model")
    if not isinstance(model, Mapping):
        model = payload
    for key in ("feature_names", "intercept", "coefficients"):
        if key not in model:
            raise ValueError(f"candidate model missing {key}")
    validate_logistic_model_payload(model)
    return dict(model)


def _alias_governance_reason(alias: Mapping[str, object]) -> str | None:
    if not str(alias.get("reviewer") or "").strip():
        return "reviewer_missing"
    if not str(alias.get("reason") or "").strip():
        return "reason_missing"
    if not str(alias.get("candidate_model_run") or "").strip():
        return "candidate_model_run_missing"
    latest_model = _mapping(alias.get("latest_model"))
    if latest_model.get("mutated") is not False:
        return "latest_model_mutation_unknown"
    authority = _mapping(alias.get("authority"))
    if authority.get("human_review_required") is not True:
        return "human_review_required_missing"
    if authority.get("mutates_latest_model") is not False:
        return "mutates_latest_model_not_false"
    if str(authority.get("llm_authority") or "").lower() != "none":
        return "llm_authority_not_none"
    return None


def _payload(generated, state, *, blockers, model_path, latest_model, latest_hash, reviewer, reason, ttl_days):
    created = date.fromisoformat(str(generated)[:10])
    return {
        "schema_version": "1.0",
        "generated_at": generated,
        "alias_state": state,
        "status": state,
        "active_model_path": str(model_path) if model_path is not None else None,
        "active_model_sha256": None,
        "alias_hash": None,
        "created_on": created.isoformat(),
        "expires_on": (created + timedelta(days=ttl_days)).isoformat(),
        "ttl_days": ttl_days,
        "reviewer": reviewer,
        "reason": reason,
        "latest_model": {"path": str(Path(latest_model)), "sha256": latest_hash, "mutated": False},
        "blockers": list(blockers),
        "authority": {"human_review_required": True, "mutates_latest_model": False, "llm_authority": "none"},
        "safety": {"paper_only": True, "broker_client_built": False, "credentials_read": False, "orders_submitted": False, "live_trading_authorized": False, "live_trading_allowed": False},
        ALIAS_SIGNATURE_FIELD: None,
        ALIAS_SIGNATURE_VERSION_FIELD: None,
    }


def _write(payload: dict[str, object], output_path: Path, markdown_path: Path) -> PaperModelAliasResult:
    if payload.get("active_model_sha256"):
        payload["alias_hash"] = payload["active_model_sha256"]
    write_json_artifact(payload, output_path)
    write_text_artifact(f"# Paper Model Alias\n\nState: **{payload.get('alias_state')}**\n", markdown_path)
    state = str(payload.get("alias_state") or STATE_BLOCKED)
    return PaperModelAliasResult(0 if state in {STATE_ACTIVE, STATE_CHAMPION_ONLY} else 1, state, output_path, markdown_path, payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _alias_signing_secret() -> str | None:
    key = os.environ.get(PAPER_MODEL_ALIAS_SIGNING_KEY_ENV)
    if key:
        key = key.strip()
        if key:
            return key
    return None


def _alias_signature_valid(alias: Mapping[str, object], secret: str) -> bool:
    signature = str(alias.get(ALIAS_SIGNATURE_FIELD) or "")
    if not signature.strip():
        return False
    expected = _sign_alias_payload(alias, secret)
    if expected is None:
        return False
    return hmac.compare_digest(signature.rstrip("="), expected.rstrip("="))


def _sign_alias_payload(alias: Mapping[str, object], secret: str | None) -> str | None:
    if not secret:
        return None
    normalized = dict(alias)
    normalized.pop(ALIAS_SIGNATURE_FIELD, None)
    normalized.pop(ALIAS_SIGNATURE_VERSION_FIELD, None)
    payload = json.dumps(_normalize_signature_payload(normalized), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()).decode("ascii").rstrip("=")


def _normalize_signature_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _normalize_signature_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_signature_payload(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _is_iso_date(value: object) -> bool:
    try:
        date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    return True
