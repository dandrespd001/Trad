"""Generate measurable shadow-only challenger signals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.data.io import read_records
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact
from trading_ai.models.baseline import LogisticBaselineModel
from trading_ai.models.signals import generate_model_signals


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "reports/tmp/paper_challenger_signals"


class PaperChallengerSignalsOperationalError(RuntimeError):
    """Raised when challenger shadow signals cannot be generated."""


@dataclass(frozen=True)
class PaperChallengerSignalsResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def run_paper_challenger_signals(
    *,
    as_of_date: str,
    model_run: str | Path,
    features: str | Path,
    readiness: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperChallengerSignalsResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "challenger_signals.json"
    markdown_path = output_root / "challenger_signals.md"
    reasons: list[str] = []
    try:
        run_payload = read_json_artifact(model_run)
        readiness_payload = read_json_artifact(readiness)
        model = _model_from_run(run_payload)
        rows = read_records(features)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        payload = _payload(
            as_of_date=as_of_date,
            generated_at=generated_at,
            status="BLOCKED",
            signals=[],
            selected_signal=None,
            reasons=[f"invalid_input:{exc}"],
            model_run=model_run,
            features=features,
            readiness=readiness,
        )
        return _write(payload, output_path=output_path, markdown_path=markdown_path)

    reasons.extend(_readiness_blockers(readiness_payload, as_of_date=as_of_date))
    allowlist = _allowlist(readiness_payload)
    if reasons:
        signals = []
    else:
        signals = [_signal_to_dict(signal) for signal in generate_model_signals(rows, model=model, allowlist=allowlist)]
    buy_signals = [signal for signal in signals if str(signal.get("action") or "").lower() == "buy"]
    buy_signals.sort(key=lambda item: (float(item.get("probability") or 0.0), str(item.get("symbol") or "")), reverse=True)
    payload = _payload(
        as_of_date=as_of_date,
        generated_at=generated_at,
        status="OK" if not reasons else "BLOCKED",
        signals=signals,
        selected_signal=buy_signals[0] if buy_signals else None,
        reasons=reasons,
        model_run=model_run,
        features=features,
        readiness=readiness,
    )
    return _write(payload, output_path=output_path, markdown_path=markdown_path)


def _model_from_run(payload: Mapping[str, object]) -> LogisticBaselineModel:
    model_payload = payload.get("model")
    if not isinstance(model_payload, Mapping):
        model_payload = payload.get("serialized_model")
    if not isinstance(model_payload, Mapping):
        model_payload = payload
    return LogisticBaselineModel.from_dict(model_payload)


def _readiness_blockers(payload: Mapping[str, object], *, as_of_date: str) -> list[str]:
    reasons: list[str] = []
    if str(payload.get("status") or "").upper() != "READY" or payload.get("ready_for_paper_daily") is not True:
        reasons.append("readiness_not_ready")
    approved = _mapping(payload.get("approved_dataset"))
    latest = str(approved.get("end") or payload.get("as_of_date") or "")
    if latest and latest[:10] < as_of_date:
        reasons.append("dataset_stale")
    safety = _mapping(payload.get("safety"))
    if safety.get("credentials_read") is True:
        reasons.append("credentials_read")
    if safety.get("live_trading_allowed") is True or safety.get("live_trading_authorized") is True:
        reasons.append("live_trading_not_allowed")
    return reasons


def _allowlist(payload: Mapping[str, object]) -> tuple[str, ...]:
    symbols = _mapping(payload.get("approved_dataset")).get("symbols")
    if isinstance(symbols, list):
        return tuple(str(symbol).upper() for symbol in symbols)
    return ()


def _signal_to_dict(signal) -> dict[str, object]:
    return {
        "timestamp": signal.timestamp,
        "symbol": signal.symbol,
        "probability": signal.probability,
        "threshold": signal.threshold,
        "action": signal.action,
    }


def _payload(
    *,
    as_of_date: str,
    generated_at: str | None,
    status: str,
    signals: list[Mapping[str, object]],
    selected_signal: Mapping[str, object] | None,
    reasons: list[str],
    model_run: str | Path,
    features: str | Path,
    readiness: str | Path,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "as_of_date": as_of_date,
        "status": status,
        "shadow_only": True,
        "affects_paper_order": False,
        "signals": [dict(signal) for signal in signals],
        "selected_signal": dict(selected_signal) if selected_signal is not None else None,
        "sources": {"model_run": str(Path(model_run)), "features": str(Path(features)), "readiness": str(Path(readiness))},
        "reasons": reasons,
        "authority": {"llm_authority": "none", "orders_submitted": False, "mutates_latest_model": False},
        "safety": {"paper_only": True, "shadow_only": True, "broker_client_built": False, "credentials_read": False, "orders_submitted": False, "live_trading_authorized": False, "live_trading_allowed": False},
    }


def _write(payload: dict[str, object], *, output_path: Path, markdown_path: Path) -> PaperChallengerSignalsResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(_render_markdown(payload), markdown_path)
    status = str(payload.get("status") or "BLOCKED")
    return PaperChallengerSignalsResult(0 if status == "OK" else 1, status, output_path, markdown_path, payload)


def _render_markdown(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# Paper Challenger Signals",
            "",
            f"Status: **{payload.get('status') or 'BLOCKED'}**",
            f"As of date: `{payload.get('as_of_date') or ''}`",
            f"Signals: `{len(payload.get('signals') if isinstance(payload.get('signals'), list) else [])}`",
            "",
            "Shadow only: `True`",
            "Affects paper order: `False`",
            "",
        ]
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
