"""Human-gated USD 1 live canary precheck and evidence writer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.data.market_calendar import is_trading_day
from trading_ai.execution.autonomy_level import (
    DEFAULT_STATE_DIR as AUTONOMY_DEFAULT_STATE_DIR,
    evaluate_autonomy_gate,
    load_autonomy_state,
)
from trading_ai.execution.live_alpaca import LiveOrder
from trading_ai.execution.live_circuit_breaker import load_live_circuit_breaker
from trading_ai.execution.live_connection import AlpacaLiveConnectionError
from trading_ai.execution.live_readiness import STATE_READY
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact
from trading_ai.execution.paper_signal_approval import (
    DEFAULT_REGISTRY_DIR as APPROVAL_DEFAULT_REGISTRY_DIR,
    compute_plan_hash,
    evaluate_signal_approval_gate,
    load_signal_approval_registry,
)
from trading_ai.risk.policy import RiskLimits

DEFAULT_OUTPUT_DIR = "reports/tmp/live_canary"
ROLLBACK_COMMAND = (
    "python -m trading_ai.cli live-safe-flatten "
    "--as-of-date <YYYY-MM-DD> "
    "--positions-fixture <positions.json> "
    "--allowlist SPY "
    "--reviewer <reviewer> "
    "--reason <reason> "
    "--output-dir reports/tmp/live_safe_flatten"
)


@dataclass(frozen=True)
class LiveCanaryResult:
    exit_code: int
    status: str
    output_path: Path
    markdown_path: Path
    payload: dict[str, object]


def expected_live_canary_confirmation(*, as_of_date: str, symbol: str, reviewer: str, reason: str) -> str:
    return f"I confirm LIVE CANARY {as_of_date} {symbol.upper()} USD 1 reviewer={reviewer} reason={reason}"


def expected_real_submit_confirmation(
    *,
    as_of_date: str,
    symbol: str,
    expected_readiness_hash: str,
    reviewer: str,
    reason: str,
) -> str:
    return (
        f"I confirm REAL LIVE SUBMIT {as_of_date} {symbol.upper()} USD 1 "
        f"readiness_hash={expected_readiness_hash} reviewer={reviewer} reason={reason}"
    )


def run_live_canary(
    *,
    as_of_date: str,
    symbol: str,
    notional_usd: float,
    readiness: str | Path,
    expected_readiness_hash: str,
    breaker_state_path: str | Path,
    rehearsal_summary: str | Path,
    rollback_evidence: str | Path,
    reviewer: str,
    reason: str,
    confirmation: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    market_open: bool = True,
    market_clock: Any | None = None,
    enable_real_submit: bool = False,
    confirm_real_submit: str | None = None,
    reference_price: float | None = None,
    live_price: float | None = None,
    max_price_deviation_pct: float | None = None,
    risk_limits: RiskLimits | None = None,
    allowlist: tuple[str, ...] | None = None,
    runtime_factory: Any | None = None,
    broker: Any | None = None,
    generated_at: str | None = None,
    autonomy_state_dir: str | Path = AUTONOMY_DEFAULT_STATE_DIR,
    autonomy_market: str = "equities",
    signal_plan: str | Path | None = None,
    approval_registry_dir: str | Path = APPROVAL_DEFAULT_REGISTRY_DIR,
) -> LiveCanaryResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "live_canary.json"
    markdown_path = output_root / "live_canary.md"
    generated = generated_at or datetime.now(UTC).isoformat()
    clean_symbol = symbol.upper()
    blockers: list[str] = []

    readiness_path = Path(readiness)
    readiness_hash = _sha256_or_none(readiness_path)
    readiness_payload = _read_json_or_none(readiness_path)
    rehearsal_payload = _read_json_or_none(rehearsal_summary)
    rollback_payload = _read_json_or_none(rollback_evidence)
    breaker_state = load_live_circuit_breaker(breaker_state_path)

    expected_confirmation = expected_live_canary_confirmation(
        as_of_date=as_of_date,
        symbol=clean_symbol,
        reviewer=reviewer,
        reason=reason,
    )
    if confirmation != expected_confirmation:
        blockers.append("confirmation_mismatch")
    expected_submit_confirmation = expected_real_submit_confirmation(
        as_of_date=as_of_date,
        symbol=clean_symbol,
        expected_readiness_hash=expected_readiness_hash,
        reviewer=reviewer,
        reason=reason,
    )
    if enable_real_submit:
        if confirm_real_submit != expected_submit_confirmation:
            blockers.append("real_submit_confirmation_mismatch")
        if reference_price is None:
            blockers.append("missing_reference_price")
        elif reference_price <= 0:
            blockers.append("invalid_reference_price")
        if risk_limits is None:
            blockers.append("live_risk_limits_required")
        elif not risk_limits.live_trading_allowed:
            blockers.append("live_trading_not_allowed_by_risk_config")
        if allowlist is None:
            blockers.append("live_allowlist_required")
        elif clean_symbol not in {item.upper() for item in allowlist}:
            blockers.append("symbol_not_allowlisted")
    if not reviewer.strip() or not reason.strip():
        blockers.append("human_review_required")
    if readiness_hash != expected_readiness_hash:
        blockers.append("readiness_hash_mismatch")
    if _mapping(readiness_payload).get("live_readiness_state") != STATE_READY:
        blockers.append("readiness_not_ready")
    readiness_safety = _mapping(_mapping(readiness_payload).get("safety"))
    if readiness_safety.get("orders_submitted") is True:
        blockers.append("readiness_orders_submitted")
    if readiness_safety.get("live_trading_authorized") is True:
        blockers.append("readiness_live_authority_present")
    if breaker_state.tripped:
        blockers.append(f"breaker_tripped:{breaker_state.reason or 'unknown'}")
    if not market_open:
        blockers.append("market_closed")
    session_date = _parse_iso_date(as_of_date)
    if session_date is None:
        blockers.append("invalid_as_of_date")
    elif not is_trading_day(session_date):
        blockers.append("market_calendar_closed")
    if not _is_usd_one(notional_usd):
        blockers.append("notional_must_be_usd_1")
    if _mapping(rehearsal_payload).get("status") != "PASSED":
        blockers.append("s0_s11_evidence_missing")
    if not _rollback_prevalidated(rollback_payload):
        blockers.append("rollback_not_prevalidated")

    # Autonomy ladder gate (docs/autonomy-ladder.md, Sprint A6). Always
    # evaluated for evidence purposes; only extends the blocking list when a
    # real submit is being requested, so pre-N1 dry-run rehearsals stay
    # green.
    autonomy_state = load_autonomy_state(autonomy_market, state_dir=autonomy_state_dir)
    autonomy_gate_blockers = evaluate_autonomy_gate(
        market=autonomy_market,
        requested_action="real_submit_approved",
        state=autonomy_state,
    )
    if enable_real_submit:
        blockers.extend(autonomy_gate_blockers)

    # Signal-plan approval/veto gate (docs/autonomy-ladder.md, Sprint A2/A6).
    # Always computed when a plan is supplied so dry-runs can report it
    # informationally; only required (and only extends blockers) for a real
    # submit.
    signal_plan_path, signal_plan_hash, signal_approval_gate_blockers = _evaluate_signal_plan_approval(
        as_of_date=as_of_date,
        signal_plan=signal_plan,
        approval_registry_dir=approval_registry_dir,
        require_plan=enable_real_submit,
    )
    if enable_real_submit:
        blockers.extend(signal_approval_gate_blockers)

    runtime: Any | None = None
    credentials_read = False
    broker_client_built = broker is not None
    market_clock_open: bool | None = None
    resolved_live_price = live_price
    runtime_error_code: str | None = None
    market_data_error_code: str | None = None
    if not blockers and enable_real_submit and runtime_factory is not None:
        try:
            runtime = runtime_factory()
            credentials_read = bool(_runtime_value(runtime, "credentials_read", default=True))
        except Exception as exc:
            blockers.append("live_runtime_build_failed")
            runtime_error_code = _runtime_error_code(exc)
            runtime = None
    if runtime is not None:
        broker = broker or _runtime_value(runtime, "broker")
        runtime_clock = _runtime_value(runtime, "market_clock")
        if market_clock is None:
            try:
                market_clock = runtime_clock() if callable(runtime_clock) else runtime_clock
            except Exception:
                blockers.append("market_clock_unavailable")
                runtime_error_code = "market_clock_error"
        if resolved_live_price is None:
            runtime_price_result = _runtime_value(runtime, "live_price_result")
            if runtime_price_result is not None:
                try:
                    price_result = (
                        runtime_price_result(clean_symbol) if callable(runtime_price_result) else runtime_price_result
                    )
                    resolved_live_price = _runtime_value(price_result, "price")
                    market_data_error_code = _runtime_value(price_result, "error_code")
                except Exception:
                    market_data_error_code = "market_data_unavailable"
            else:
                runtime_price = _runtime_value(runtime, "live_price")
                try:
                    resolved_live_price = runtime_price(clean_symbol) if callable(runtime_price) else runtime_price
                except Exception:
                    market_data_error_code = "market_data_unavailable"
                if resolved_live_price is None and runtime_price is not None and market_data_error_code is None:
                    market_data_error_code = "market_data_price_missing"
    broker_client_built = broker is not None
    if enable_real_submit:
        if market_clock is None:
            blockers.append("market_clock_missing")
        else:
            market_clock_open, clock_error = _safe_market_clock_open(market_clock)
            if clock_error:
                blockers.append("market_clock_unavailable")
                runtime_error_code = runtime_error_code or "market_clock_error"
            if not market_clock_open:
                blockers.append("market_clock_closed")
        if resolved_live_price is None:
            blockers.append("missing_live_price")
        elif resolved_live_price <= 0:
            blockers.append("invalid_live_price")
    elif market_clock is not None:
        market_clock_open, clock_error = _safe_market_clock_open(market_clock)
        if clock_error:
            blockers.append("market_clock_unavailable")
            runtime_error_code = runtime_error_code or "market_clock_error"
        if not market_clock_open:
            blockers.append("market_clock_closed")

    price_deviation_pct = _price_deviation_pct(reference_price, resolved_live_price)
    resolved_max_deviation = (
        max_price_deviation_pct
        if max_price_deviation_pct is not None
        else risk_limits.max_price_deviation_pct
        if risk_limits is not None
        else 0.05
    )
    if (
        enable_real_submit
        and price_deviation_pct is not None
        and price_deviation_pct > resolved_max_deviation
    ):
        blockers.append("price_sanity_failed")
    orders_submitted = False
    post_check: dict[str, object] = {
        "order_id": None,
        "fill_status": None,
        "position": None,
        "slippage_bps": None,
        "breaker_state": {
            "tripped": breaker_state.tripped,
            "reason": breaker_state.reason,
        },
        "alert_tier": "none",
    }
    command_evidence = [
        "trading-ai live-canary --enable-real-submit" if enable_real_submit else "trading-ai live-canary",
        ROLLBACK_COMMAND,
    ]
    status = "BLOCKED" if blockers else "READY_FOR_SUBMIT"

    if not blockers and enable_real_submit:
        if broker is None:
            blockers.append("live_broker_not_injected")
            status = "BLOCKED"
        else:
            order = LiveOrder(
                symbol=clean_symbol,
                side="buy",
                client_order_id=f"live-canary-{as_of_date}-{clean_symbol}".lower(),
                notional=1.0,
                reference_price=reference_price,
                live_price=resolved_live_price,
                max_price_deviation_pct=resolved_max_deviation,
            )
            submit_result = broker.submit_order(order)
            if bool(getattr(submit_result, "accepted", False)):
                orders_submitted = True
                status = "SUBMITTED"
                response = getattr(submit_result, "broker_response", None)
                post_check = {
                    **post_check,
                    "order_id": _response_value(response, "id"),
                    "fill_status": _response_value(response, "status", getattr(submit_result, "status", None)),
                    "raw_status": getattr(submit_result, "status", None),
                    "alert_tier": "canary",
                }
            else:
                blockers.extend(str(reason) for reason in getattr(submit_result, "reasons", ()))
                status = "BLOCKED"

    payload = {
        "schema_version": "1.0",
        "generated_at": generated,
        "as_of_date": as_of_date,
        "status": status,
        "symbol": clean_symbol,
        "notional_usd": notional_usd,
        "max_orders": 1,
        "reviewer": reviewer,
        "reason": reason,
        "confirmation_expected": expected_confirmation,
        "readiness": str(readiness_path),
        "readiness_hash": readiness_hash,
        "expected_readiness_hash": expected_readiness_hash,
        "breaker_state": {
            "path": str(Path(breaker_state_path)),
            "tripped": breaker_state.tripped,
            "reason": breaker_state.reason,
        },
        "rehearsal_summary": str(Path(rehearsal_summary)),
        "rollback_evidence": str(Path(rollback_evidence)),
        "rollback_command": ROLLBACK_COMMAND,
        "command_evidence": command_evidence,
        "reference_price": reference_price,
        "live_price": resolved_live_price,
        "price_deviation_pct": price_deviation_pct,
        "max_price_deviation_pct": resolved_max_deviation,
        "market_clock_open": market_clock_open,
        "runtime_error_code": runtime_error_code,
        "market_data_error_code": market_data_error_code,
        "blockers": _dedupe(blockers),
        "autonomy": {
            "market": autonomy_market,
            "level": autonomy_state.level,
            "open_incident": autonomy_state.open_incident,
            "fail_closed": autonomy_state.fail_closed,
            "gate_blockers": autonomy_gate_blockers,
        },
        "signal_approval": {
            "plan_path": signal_plan_path,
            "plan_hash": signal_plan_hash,
            "gate_blockers": signal_approval_gate_blockers,
        },
        "post_check": post_check,
        "safety": {
            "human_confirmation_required": True,
            "exact_confirmation_matched": confirmation == expected_confirmation,
            "exact_real_submit_confirmation_matched": confirm_real_submit == expected_submit_confirmation,
            "broker_client_built": broker_client_built,
            "credentials_read": credentials_read,
            "orders_submitted": orders_submitted,
            "live_trading_authorized": False,
            "live_execution_enabled": enable_real_submit,
        },
    }
    write_json_artifact(payload, output_path)
    write_text_artifact(render_live_canary_markdown(payload), markdown_path)
    return LiveCanaryResult(
        exit_code=_exit_code(status),
        status=status,
        output_path=output_path,
        markdown_path=markdown_path,
        payload=payload,
    )


def render_live_canary_markdown(payload: Mapping[str, object]) -> str:
    blockers = payload.get("blockers")
    blocker_lines = [f"- `{item}`" for item in blockers] if isinstance(blockers, list) and blockers else ["- none"]
    post_check = _mapping(payload.get("post_check"))
    return "\n".join(
        [
            "# Live Canary USD 1",
            "",
            f"Status: **{payload.get('status')}**",
            f"As of date: `{payload.get('as_of_date')}`",
            f"Symbol: `{payload.get('symbol')}`",
            f"Notional USD: `{payload.get('notional_usd')}`",
            f"Reviewer: `{payload.get('reviewer')}`",
            f"Readiness hash: `{payload.get('readiness_hash')}`",
            "",
            "## Blockers",
            "",
            *blocker_lines,
            "",
            "## Post Check",
            "",
            f"- Order id: `{post_check.get('order_id')}`",
            f"- Fill status: `{post_check.get('fill_status')}`",
            f"- Alert tier: `{post_check.get('alert_tier')}`",
            "",
            f"Rollback command: `{payload.get('rollback_command')}`",
            "",
        ]
    )


def _evaluate_signal_plan_approval(
    *,
    as_of_date: str,
    signal_plan: str | Path | None,
    approval_registry_dir: str | Path,
    require_plan: bool,
) -> tuple[str | None, str | None, list[str]]:
    """Compute signal-plan approval evidence for the live canary.

    Returns ``(plan_path_or_none, plan_hash_or_none, gate_blockers)``. When
    ``require_plan`` is False (dry-run) and no plan is supplied, this is a
    fully informative no-op (empty ``gate_blockers``): rehearsals pre-N1 do
    not require a signal plan. When ``require_plan`` is True
    (``enable_real_submit``), a missing or unreadable plan, or one whose
    ``generated_at`` cannot be trusted, is itself surfaced as a blocker.
    """

    if signal_plan is None:
        return None, None, ["signal_plan_artifact_missing"] if require_plan else []

    plan_path = Path(signal_plan)
    try:
        plan_payload = read_json_artifact(plan_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return str(plan_path), None, ["signal_plan_artifact_invalid"]

    plan_hash = compute_plan_hash(plan_payload)
    plan_generated_at = plan_payload.get("generated_at")
    if not plan_generated_at:
        return str(plan_path), plan_hash, ["plan_generated_at_invalid"]

    registry = load_signal_approval_registry(as_of_date, registry_dir=approval_registry_dir)
    try:
        # evaluate_signal_approval_gate does not itself parse
        # plan_generated_at for requested_action="real_submit_approved" (it
        # only does so for the veto-window action), so a malformed timestamp
        # must be validated here to fail closed as a blocker rather than
        # being silently ignored (Sprint A2 review follow-up).
        _ensure_parseable_timestamp(plan_generated_at)
        gate_blockers = evaluate_signal_approval_gate(
            plan_hash=plan_hash,
            registry_payload=registry,
            requested_action="real_submit_approved",
            plan_generated_at=plan_generated_at,
            now=datetime.now(UTC).isoformat(),
        )
    except ValueError:
        gate_blockers = ["plan_generated_at_invalid"]
    return str(plan_path), plan_hash, gate_blockers


def _ensure_parseable_timestamp(value: object) -> None:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    datetime.fromisoformat(text)


def _rollback_prevalidated(payload: object) -> bool:
    data = _mapping(payload)
    safety = _mapping(data.get("safety"))
    return data.get("status") == "DRY_RUN_READY" and safety.get("orders_submitted") is False


def _read_json_or_none(path: str | Path) -> dict[str, object] | None:
    try:
        return read_json_artifact(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _sha256_or_none(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _market_clock_open(market_clock: Any) -> bool:
    if callable(market_clock):
        return bool(market_clock())
    is_open = getattr(market_clock, "is_open", None)
    if is_open is not None:
        return bool(is_open)
    return False


def _safe_market_clock_open(market_clock: Any) -> tuple[bool, bool]:
    try:
        return _market_clock_open(market_clock), False
    except Exception:
        return False, True


def _is_usd_one(value: float) -> bool:
    return abs(float(value) - 1.0) < 0.000001


def _exit_code(status: str) -> int:
    if status in {"READY_FOR_SUBMIT", "SUBMITTED"}:
        return 0
    if status == "BLOCKED":
        return 1
    return 2


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _runtime_value(runtime: Any, name: str, default: Any = None) -> Any:
    if isinstance(runtime, Mapping):
        return runtime.get(name, default)
    return getattr(runtime, name, default)


def _response_value(response: object, name: str, default: object = None) -> object:
    if isinstance(response, Mapping):
        return response.get(name, default)
    return getattr(response, name, default)


def _runtime_error_code(exc: Exception) -> str:
    if isinstance(exc, AlpacaLiveConnectionError):
        message = str(exc).lower()
        if "credential" in message:
            return "missing_live_credentials"
        if "alpaca-py is not installed" in message or "optional dependency" in message:
            return "alpaca_dependency_missing"
    if isinstance(exc, ImportError):
        return "alpaca_dependency_missing"
    return "live_runtime_unexpected_error"


def _price_deviation_pct(reference_price: float | None, live_price: float | None) -> float | None:
    if reference_price is None or live_price is None or reference_price <= 0 or live_price <= 0:
        return None
    return abs(float(live_price) - float(reference_price)) / float(reference_price)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
