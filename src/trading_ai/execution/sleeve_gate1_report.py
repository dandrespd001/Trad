"""Build a fail-closed Gate 1 paper execution scorecard.

The governed sleeve-rebalance cycle (M3) writes one ``cycle_<sleeve>_<date>.json``
per sleeve per day plus an ``allocation_<date>.json``. As the operator
accumulates Gate 1 paper days, this module rolls them up into a single
scorecard with orders, fills, effective cost per trade, PnL proxy, and any
incidents — without ever submitting orders (read-only against the broker).

Since Sprint M7 each cycle payload also carries an ``account_risk`` snapshot
(equity, daily PnL %, current drawdown %) that feeds the daily-loss and
drawdown kill-switches. This scorecard surfaces that context per day plus a
``risk_track`` aggregate so the operator can audit those inputs from the Gate 1
evidence alone (see Sprint M8 — "no total losses" goal).

This report is evidence, not a best-effort dashboard. Corrupt artifacts,
missing policy, incomplete broker reads, unresolved orders, missing individual
FILL activities, unknown costs, or an unreconciled incident all return
``BLOCKED`` with a non-zero exit code. Paper can pass the operational gate but
never sets ``promotion_eligible`` by itself because Alpaca paper omits material
live execution effects and fees.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from trading_ai.execution.execution_costs import (
    ExecutionCostEvidenceError,
    execution_cost_components,
    execution_latency_ms,
    summarize_signed_bps,
)
from trading_ai.execution.execution_evidence_ledger import (
    DurableExecutionEvidenceLedger,
    ExecutionEvidenceLedgerError,
    InvalidExecutionEvidenceError,
    PolicyRegistration,
)
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    paper_exit_code,
    redact_secrets,
    write_json_artifact,
    write_text_artifact,
)

SCHEMA_VERSION = "2.0"
POLICY_SCHEMA_VERSION = "2.0"
EVIDENCE_SCOPE = "PAPER_SIMULATION"
_CYCLE_SCHEMA_VERSIONS = frozenset({"1.0"})
_TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})
_VALID_CYCLE_STATUSES = frozenset({"OK", "WARN", "BLOCKED", "REPORT_ONLY"})
_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "created_at",
        "window",
        "required_sleeves",
        "min_complete_days",
        "price_shortfall_limits_bps",
        "max_daily_loss_pct",
        "max_drawdown_pct",
    }
)

# Cycle files emitted by ``run_sleeve_rebalance`` follow this naming. The
# ``allocation`` files use a slightly different shape and are picked up by a
# separate pattern.
_CYCLE_PATTERN = re.compile(r"^cycle_([a-z0-9_]+)_(\d{4}-\d{2}-\d{2})\.json$")
_ALLOCATION_PATTERN = re.compile(r"^allocation_(\d{4}-\d{2}-\d{2})\.json$")
# Fallback for top-level daily cycle files (no sleeve prefix).
_CYCLE_DAILY_PATTERN = re.compile(r"^cycle_(\d{4}-\d{2}-\d{2})\.json$")

# Marker emitted by ``sleeve_rebalance`` when the broker failed to provide a
# real account snapshot — those cycles BLOCK with this blocker so the scorecard
# can flag the day as missing risk context.
ACCOUNT_RISK_CONTEXT_UNAVAILABLE = "account_risk_context_unavailable"

# Empty shape used when no cycle in the window carried an ``account_risk``
# snapshot (pre-M7 payloads, broker-less runs, empty directories). Keeping
# the schema stable lets callers always read ``payload["risk_track"]``.
_EMPTY_RISK_TRACK: dict[str, object] = {
    "days_with_risk_context": 0,
    "min_equity": None,
    "max_equity": None,
    "max_drawdown_pct_observed": None,
    "worst_daily_pnl_pct": None,
}


@dataclass(frozen=True)
class Gate1ReportResult:
    exit_code: int
    status: str  # "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def compute_gate1_report_hash(payload: Mapping[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "artifact_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_gate1_policy(path: str | Path) -> tuple[dict[str, object], str]:
    policy_path = Path(path)
    raw = policy_path.read_bytes()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("Gate 1 policy must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def register_gate1_policy(
    policy: Mapping[str, object],
    *,
    evidence_ledger: DurableExecutionEvidenceLedger,
) -> tuple[PolicyRegistration, bool]:
    """Persist a Gate 1 policy before its evidence window starts."""

    normalized, start, end, blockers = _normalize_policy(policy, start=None, end=None)
    if normalized is None or start is None or end is None or blockers:
        raise InvalidExecutionEvidenceError(
            "Gate 1 policy is invalid: " + ",".join(blockers or ["window_missing"])
        )
    return evidence_ledger.register_policy(
        str(normalized["policy_id"]),
        normalized,
        effective_from=datetime.combine(start, time.min, tzinfo=UTC),
        effective_until=datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _normalize_policy(
    policy: Mapping[str, object] | None,
    *,
    start: date | None,
    end: date | None,
) -> tuple[dict[str, object] | None, date | None, date | None, list[str]]:
    blockers: list[str] = []
    if policy is None:
        return None, start, end, ["gate_policy_missing"]
    supplied = dict(policy)
    if set(supplied) != _POLICY_FIELDS:
        blockers.append("gate_policy_fields_invalid")
    normalized = {field: supplied.get(field) for field in _POLICY_FIELDS}
    if normalized.get("schema_version") != POLICY_SCHEMA_VERSION:
        blockers.append("gate_policy_schema_invalid")
        normalized["schema_version"] = None
    policy_id = normalized.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        blockers.append("gate_policy_id_invalid")
        normalized["policy_id"] = None
    else:
        normalized["policy_id"] = policy_id.strip()
    window = normalized.get("window")
    window_map = window if isinstance(window, Mapping) else {}
    if set(window_map) != {"start", "end"}:
        blockers.append("gate_policy_window_invalid")
    try:
        policy_start = date.fromisoformat(str(window_map.get("start") or ""))
        policy_end = date.fromisoformat(str(window_map.get("end") or ""))
    except ValueError:
        policy_start = None
        policy_end = None
        blockers.append("gate_policy_window_invalid")
    normalized["window"] = {
        "start": policy_start.isoformat() if policy_start is not None else None,
        "end": policy_end.isoformat() if policy_end is not None else None,
    }
    if policy_start is not None and policy_end is not None and policy_start > policy_end:
        blockers.append("gate_policy_window_invalid")
    if start is not None and policy_start is not None and start != policy_start:
        blockers.append("gate_policy_window_mismatch")
    if end is not None and policy_end is not None and end != policy_end:
        blockers.append("gate_policy_window_mismatch")
    effective_start = start or policy_start
    effective_end = end or policy_end

    created_at = normalized.get("created_at")
    try:
        created = datetime.fromisoformat(str(created_at or "").replace("Z", "+00:00"))
    except ValueError:
        created = None
        blockers.append("gate_policy_created_at_invalid")
        normalized["created_at"] = None
    if created is not None and (created.tzinfo is None or created.utcoffset() is None):
        blockers.append("gate_policy_created_at_invalid")
        normalized["created_at"] = None
        created = None
    elif created is not None:
        normalized["created_at"] = created.astimezone(UTC).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    if created is not None and effective_start is not None:
        window_start = datetime.combine(effective_start, time.min, tzinfo=UTC)
        if created.astimezone(UTC) >= window_start:
            blockers.append("gate_policy_not_preregistered")

    sleeves = normalized.get("required_sleeves")
    if (
        not isinstance(sleeves, list)
        or not sleeves
        or any(not isinstance(item, str) or not item.strip() for item in sleeves)
        or len({str(item).strip().lower() for item in sleeves}) != len(sleeves)
    ):
        blockers.append("gate_policy_required_sleeves_invalid")
        normalized["required_sleeves"] = []
    else:
        normalized["required_sleeves"] = sorted(str(item).strip().lower() for item in sleeves)

    for field in ("min_complete_days",):
        value = normalized.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            blockers.append(f"gate_policy_{field}_invalid")
            normalized[field] = None
    limits = normalized.get("price_shortfall_limits_bps")
    if not isinstance(limits, Mapping):
        blockers.append("gate_policy_price_shortfall_limits_bps_invalid")
        normalized["price_shortfall_limits_bps"] = {}
    else:
        required = set(normalized.get("required_sleeves") or [])
        normalized_limits: dict[str, object] = {}
        limit_names = [
            str(key).strip().lower()
            for key in limits
            if isinstance(key, str) and key.strip()
        ]
        if (
            len(limit_names) != len(limits)
            or len(set(limit_names)) != len(limit_names)
            or set(limit_names) != required
        ):
            blockers.append("gate_policy_price_shortfall_limits_bps_invalid")
        for sleeve, raw_limit in limits.items():
            sleeve_name = str(sleeve).strip().lower()
            if not isinstance(raw_limit, Mapping) or set(raw_limit) != {
                "min_reconciled_fills",
                "max_median",
                "max_p90",
            }:
                blockers.append(f"gate_policy_price_shortfall_limit_invalid:{sleeve_name}")
                continue
            min_fills = raw_limit.get("min_reconciled_fills")
            median_limit = _coerce_float(raw_limit.get("max_median"))
            p90_limit = _coerce_float(raw_limit.get("max_p90"))
            if (
                not isinstance(min_fills, int)
                or isinstance(min_fills, bool)
                or min_fills <= 0
                or median_limit is None
                or median_limit < 0
                or p90_limit is None
                or p90_limit < 0
            ):
                blockers.append(f"gate_policy_price_shortfall_limit_invalid:{sleeve_name}")
                continue
            normalized_limits[sleeve_name] = {
                "min_reconciled_fills": min_fills,
                "max_median": median_limit,
                "max_p90": p90_limit,
            }
        normalized["price_shortfall_limits_bps"] = normalized_limits
    for field in ("max_daily_loss_pct", "max_drawdown_pct"):
        value = _coerce_float(normalized.get(field))
        if value is None or value < 0 or value > 1:
            blockers.append(f"gate_policy_{field}_invalid")
            normalized[field] = None
        else:
            normalized[field] = value
    return normalized, effective_start, effective_end, sorted(set(blockers))


def _coerce_float(value: object, *, default: float | None = None) -> float | None:
    """Coerce a finite float; missing, booleans and non-finite values fail."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(token: str) -> object:
    raise ValueError(f"non-finite JSON constant: {token}")


def _coerce_position_value(position: Any, attribute: str, *, default: Any = None) -> Any:
    """Pull ``attribute`` from a position that may be a dict or a duck-typed object."""
    if isinstance(position, Mapping):
        return position.get(attribute, default)
    return getattr(position, attribute, default)


def _drawdown_pct(risk: Mapping[str, object]) -> float:
    """Tolerantly read ``current_drawdown_pct`` from an account_risk snapshot.

    Missing or non-numeric values fall back to ``0.0`` so the snapshot can
    still participate in the per-day "highest drawdown" selection without
    raising.
    """
    return _coerce_float(risk.get("current_drawdown_pct"), default=0.0) or 0.0


def _summarize_risk_track(
    account_risks_seen: list[Mapping[str, object]],
    per_day_risk_candidates: Mapping[str, list[Mapping[str, object]]],
) -> dict[str, object]:
    """Build the ``risk_track`` aggregate for the scorecard payload.

    ``days_with_risk_context`` counts unique dates that saw at least one
    non-null account_risk snapshot. The extrema are computed across every
    snapshot observed (a date with multiple cycles contributes each of its
    snapshots to the distribution). Each metric falls back to ``None`` when
    no value is available.
    """
    equities: list[float] = []
    drawdowns: list[float] = []
    daily_pnls: list[float] = []
    for risk in account_risks_seen:
        equity = _coerce_float(risk.get("equity"))
        if equity is not None:
            equities.append(equity)
        drawdown = _coerce_float(risk.get("current_drawdown_pct"))
        if drawdown is not None:
            drawdowns.append(drawdown)
        daily_pnl = _coerce_float(risk.get("daily_pnl_pct"))
        if daily_pnl is not None:
            daily_pnls.append(daily_pnl)
    return {
        "days_with_risk_context": len(per_day_risk_candidates),
        "min_equity": round(min(equities), 6) if equities else None,
        "max_equity": round(max(equities), 6) if equities else None,
        "max_drawdown_pct_observed": round(max(drawdowns), 6) if drawdowns else None,
        "worst_daily_pnl_pct": round(min(daily_pnls), 6) if daily_pnls else None,
    }


def _scan_cycle_files(cycles_dir: Path) -> tuple[list[tuple[str, date, Path]], list[tuple[date, Path]]]:
    """Return (cycle_records, allocation_records) discovered in ``cycles_dir``.

    Files whose name does not match the expected patterns are ignored
    silently — they belong to other commands and must not influence the
    scorecard.
    """
    cycles: list[tuple[str, date, Path]] = []
    allocations: list[tuple[date, Path]] = []
    if not cycles_dir.exists() or not cycles_dir.is_dir():
        return cycles, allocations
    for entry in sorted(cycles_dir.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        match = _CYCLE_PATTERN.match(entry.name)
        if match:
            try:
                sleeve_date = date.fromisoformat(match.group(2))
            except ValueError:
                continue
            cycles.append((match.group(1), sleeve_date, entry))
            continue
        match = _ALLOCATION_PATTERN.match(entry.name)
        if match:
            try:
                allocation_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            allocations.append((allocation_date, entry))
            continue
        match = _CYCLE_DAILY_PATTERN.match(entry.name)
        if match:
            try:
                daily_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            cycles.append(("daily", daily_date, entry))
    return cycles, allocations


def _in_window(target: date, start: date | None, end: date | None) -> bool:
    if start is not None and target < start:
        return False
    return not (end is not None and target > end)


def _extract_plan_reference_prices(plan: object) -> dict[str, float]:
    """Map ``symbol/pair -> reference_price`` from a cycle ``plan`` list."""
    result: dict[str, float] = {}
    if not isinstance(plan, list):
        return result
    for entry in plan:
        if not isinstance(entry, Mapping):
            continue
        symbol = str(entry.get("pair") or entry.get("symbol") or "").upper()
        if not symbol:
            continue
        price = _coerce_float(entry.get("reference_price"))
        if price is not None and price > 0:
            result[symbol] = price
    return result


def _lookup_submission_order(
    broker: Any,
    client_order_id: str,
) -> tuple[Mapping[str, object] | None, str | None]:
    """Return ``(snapshot_dict, error_label)``.

    The snapshot is a plain dict the caller can serialize, or ``None`` if the
    broker lookup failed. ``error_label`` carries the reason when the lookup
    raised — it is the ``order_lookup_failed:<cid>`` incident slug.
    """
    try:
        snapshot = broker.get_order_by_client_id(client_order_id)
    except Exception as exc:  # noqa: BLE001
        return None, f"order_lookup_failed:{client_order_id}:{type(exc).__name__}"
    if snapshot is None:
        return None, f"order_lookup_failed:{client_order_id}:not_found"
    if isinstance(snapshot, Mapping):
        return dict(snapshot), None
    # Duck-typed broker: convert to a plain dict.
    serialized: dict[str, object] = {}
    for attribute in (
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "status",
        "notional",
        "quantity",
        "filled_quantity",
        "filled_avg_price",
        "submitted_at",
        "created_at",
        "updated_at",
        "expires_at",
        "filled_at",
    ):
        serialized[attribute] = getattr(snapshot, attribute, None)
    return serialized, None


def _summarize_fills(
    effective_costs: list[float],
) -> dict[str, object]:
    return summarize_signed_bps(list(effective_costs))


def _render_markdown(payload: Mapping[str, object]) -> str:
    """Render a human-readable scorecard from the JSON payload."""
    status = str(payload.get("status") or "")
    window = payload.get("window") or {}
    start = str(window.get("start") or "—")
    end = str(window.get("end") or "—")
    days = payload.get("days") or []
    n_cycles = payload.get("n_cycles") or 0
    by_status = payload.get("by_status") or {}
    fills = payload.get("fills") or []
    costs = payload.get("signed_price_shortfall_bps") or {}
    incidents = payload.get("incidents") or []
    positions = payload.get("positions") or []
    equity = payload.get("account_equity")
    safety = payload.get("safety") or {}

    lines: list[str] = []
    lines.append(f"# Gate 1 Scorecard — {status}")
    lines.append("")
    lines.append(f"- Window: {start} → {end}")
    lines.append(f"- Days with cycles: **{len(days)}** ({', '.join(days) if days else 'none'})")
    lines.append(f"- Cycles: {n_cycles}")
    lines.append(f"- By status: {by_status}")
    lines.append(
        f"- Safety: read_only={safety.get('read_only')} "
        f"orders_submitted={safety.get('orders_submitted')}"
    )
    if equity is not None:
        lines.append(f"- Account equity (USD): {equity}")
    lines.append("")
    if isinstance(payload.get("per_day"), list) and payload["per_day"]:
        lines.append("## Per-day")
        lines.append("")
        lines.append("| Date | Sleeves | Statuses | Submitted | Errored |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in payload["per_day"]:  # type: ignore[union-attr]
            sleeves = ",".join(str(s) for s in (entry.get("sleeves") or []))
            statuses = ",".join(
                f"{item.get('sleeve')}={item.get('status')}"
                for item in (entry.get("sleeves_status") or [])
            )
            lines.append(
                f"| {entry.get('date')} | {sleeves} | {statuses} | "
                f"{entry.get('orders_submitted', 0)} | {entry.get('orders_errored', 0)} |"
            )
        lines.append("")
    risk_track = payload.get("risk_track")
    if isinstance(risk_track, Mapping):
        lines.append("## Risk track")
        lines.append("")
        lines.append(
            f"- Days with risk context: **{risk_track.get('days_with_risk_context', 0)}**"
        )
        lines.append(
            f"- Equity range (USD): min={risk_track.get('min_equity')}, "
            f"max={risk_track.get('max_equity')}"
        )
        lines.append(
            f"- Max drawdown observed: {risk_track.get('max_drawdown_pct_observed')}"
        )
        lines.append(
            f"- Worst daily PnL %: {risk_track.get('worst_daily_pnl_pct')}"
        )
        per_day_with_risk = [
            entry for entry in (payload.get("per_day") or [])
            if isinstance(entry, Mapping) and entry.get("account_risk")
        ]
        if per_day_with_risk:
            lines.append("")
            lines.append("| Date | Equity (USD) | Daily PnL % | Drawdown % |")
            lines.append("| --- | --- | --- | --- |")
            for entry in per_day_with_risk:
                risk = entry["account_risk"]
                lines.append(
                    f"| {entry.get('date')} | {risk.get('equity')} | "
                    f"{risk.get('daily_pnl_pct')} | {risk.get('current_drawdown_pct')} |"
                )
        lines.append("")
    if fills:
        lines.append("## Fills")
        lines.append("")
        lines.append("| Date | Sleeve | Symbol | Side | Qty | Avg Price | Reference | Cost (bps) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for fill in fills:
            lines.append(
                f"| {fill.get('date')} | {fill.get('sleeve')} | {fill.get('symbol')} | "
                f"{fill.get('side')} | {fill.get('filled_quantity')} | "
                f"{fill.get('filled_avg_price')} | {fill.get('reference_price')} | "
                f"{fill.get('effective_cost_bps')} |"
            )
        lines.append("")
    lines.append("## Signed price shortfall (bps)")
    lines.append("")
    lines.append(f"- n={costs.get('n')} min={costs.get('min')} median={costs.get('median')} max={costs.get('max')}")
    lines.append("")
    if positions:
        lines.append("## Positions (snapshot)")
        lines.append("")
        lines.append("| Symbol | Qty | Market Value | Unrealized PnL |")
        lines.append("| --- | --- | --- | --- |")
        for position in positions:
            lines.append(
                f"| {position.get('symbol')} | {position.get('qty')} | "
                f"{position.get('market_value')} | {position.get('unrealized_pl')} |"
            )
        lines.append("")
    if incidents:
        lines.append("## Incidents")
        lines.append("")
        for incident in incidents:
            lines.append(f"- {incident}")
        lines.append("")
    return "\n".join(lines)


def run_gate1_report(
    *,
    cycles_dir: str | Path,
    output: str | Path,
    markdown_output: str | Path | None = None,
    start: date | None = None,
    end: date | None = None,
    broker: Any | None = None,
    generated_at: str | None = None,
    policy: Mapping[str, object] | None = None,
    policy_sha256: str | None = None,
    evidence_ledger: DurableExecutionEvidenceLedger | None = None,
) -> Gate1ReportResult:
    """Build a broker-first scorecard and never turn missing evidence into OK."""

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    cycles_root = Path(cycles_dir)
    normalized_policy, start, end, gate_blockers = _normalize_policy(
        policy,
        start=start,
        end=end,
    )
    if start is not None and end is not None and start > end:
        gate_blockers.append("invalid_window:start_after_end")
    try:
        generated_timestamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        generated_timestamp = None
    if (
        generated_timestamp is None
        or generated_timestamp.tzinfo is None
        or generated_timestamp.utcoffset() is None
    ):
        gate_blockers.append("generated_at_invalid")
    if policy_sha256 is not None and not _is_sha256(policy_sha256):
        gate_blockers.append("policy_sha256_invalid")

    ledger_policy: PolicyRegistration | None = None
    ledger_verification: dict[str, object] = {
        "configured": evidence_ledger is not None,
        "policy_sha256": None,
        "valid": False,
        "integrity_valid": False,
        "completeness_valid": False,
        "operationally_clear": False,
        "issues": [],
        "chain_heads": {},
    }
    if evidence_ledger is None:
        gate_blockers.append("execution_evidence_ledger_missing")
    elif normalized_policy is not None and start is not None and end is not None:
        try:
            policy_id = str(normalized_policy.get("policy_id") or "")
            ledger_policy = evidence_ledger.read_policy(policy_id)
            expected_start = datetime.combine(start, time.min, tzinfo=UTC).isoformat().replace(
                "+00:00", "Z"
            )
            expected_end = datetime.combine(
                end + timedelta(days=1),
                time.min,
                tzinfo=UTC,
            ).isoformat().replace("+00:00", "Z")
            if (
                ledger_policy is None
                or ledger_policy.policy != normalized_policy
                or not _same_instant(ledger_policy.effective_from, expected_start)
                or not _same_instant(ledger_policy.effective_until, expected_end)
            ):
                gate_blockers.append("execution_policy_not_preregistered")
                ledger_policy = None
            else:
                ledger_verification["policy_sha256"] = ledger_policy.policy_sha256
        except ExecutionEvidenceLedgerError as exc:
            gate_blockers.append(f"execution_evidence_ledger_read_failed:{type(exc).__name__}")

    cycles, allocations = _scan_cycle_files(cycles_root)
    cycles = [item for item in cycles if _in_window(item[1], start, end)]
    allocations = [item for item in allocations if _in_window(item[0], start, end)]

    by_status: dict[str, int] = {}
    fills: list[dict[str, object]] = []
    orders: list[dict[str, object]] = []
    effective_costs: list[float] = []
    effective_costs_by_sleeve: dict[str, list[float]] = {}
    per_day_map: dict[str, dict[str, object]] = {}
    per_day_risk_candidates: dict[str, list[Mapping[str, object]]] = {}
    account_risks_seen: list[Mapping[str, object]] = []
    submitted_records: list[dict[str, object]] = []
    evidence_manifest: list[dict[str, object]] = []
    seen_cycle_keys: set[tuple[str, str]] = set()
    seen_client_ids: set[str] = set()

    allocation_dates: set[str] = set()
    for allocation_date, path in allocations:
        date_key = allocation_date.isoformat()
        allocation_dates.add(date_key)
        try:
            raw = path.read_bytes()
            allocation_payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            gate_blockers.append(f"allocation_unreadable:{path.name}:{type(exc).__name__}")
            continue
        evidence_manifest.append(_manifest_entry(path, raw, kind="allocation"))
        if not isinstance(allocation_payload, Mapping):
            gate_blockers.append(f"allocation_invalid:{path.name}:not_a_mapping")
            continue
        total_notional = _coerce_float(allocation_payload.get("total_notional_usd"))
        if total_notional is None or total_notional <= 0:
            gate_blockers.append(f"allocation_invalid:{path.name}:total_notional")
        allocation = allocation_payload.get("allocation")
        allocation_map = allocation if isinstance(allocation, Mapping) else {}
        allocated_total = _coerce_float(allocation_map.get("total_notional_usd"))
        if (
            total_notional is None
            or allocated_total is None
            or abs(total_notional - allocated_total) > max(1e-8, abs(total_notional) * 1e-9)
        ):
            gate_blockers.append(f"allocation_invalid:{path.name}:nested_total")
        sleeve_allocations = allocation_map.get("sleeves")
        if not isinstance(sleeve_allocations, Mapping):
            gate_blockers.append(f"allocation_invalid:{path.name}:sleeves")
        elif normalized_policy is not None:
            required = set(normalized_policy.get("required_sleeves") or [])
            observed = {str(item).strip().lower() for item in sleeve_allocations}
            if observed != required:
                gate_blockers.append(f"allocation_invalid:{path.name}:required_sleeves")
            budget_sum = 0.0
            for sleeve_name, sleeve_payload in sleeve_allocations.items():
                if not isinstance(sleeve_payload, Mapping):
                    gate_blockers.append(
                        f"allocation_invalid:{path.name}:sleeve:{sleeve_name}"
                    )
                    continue
                budget = _coerce_float(sleeve_payload.get("budget_usd"))
                if budget is None or budget < 0:
                    gate_blockers.append(
                        f"allocation_invalid:{path.name}:budget:{sleeve_name}"
                    )
                    continue
                budget_sum += budget
            if total_notional is not None and abs(budget_sum - total_notional) > max(
                1e-8, abs(total_notional) * 1e-9
            ):
                gate_blockers.append(f"allocation_invalid:{path.name}:budget_sum")

    for sleeve, cycle_date, path in cycles:
        date_key = cycle_date.isoformat()
        cycle_key = (sleeve, date_key)
        if cycle_key in seen_cycle_keys:
            gate_blockers.append(f"duplicate_cycle:{sleeve}:{date_key}")
        seen_cycle_keys.add(cycle_key)
        try:
            raw = path.read_bytes()
            payload_cycle = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            gate_blockers.append(f"cycle_unreadable:{path.name}:{type(exc).__name__}")
            continue
        evidence_manifest.append(_manifest_entry(path, raw, kind="cycle"))
        if not isinstance(payload_cycle, Mapping):
            gate_blockers.append(f"cycle_invalid:{path.name}:not_a_mapping")
            continue
        if str(payload_cycle.get("schema_version") or "") not in _CYCLE_SCHEMA_VERSIONS:
            gate_blockers.append(f"cycle_invalid:{path.name}:schema")
        if str(payload_cycle.get("as_of") or "") != date_key:
            gate_blockers.append(f"cycle_date_mismatch:{path.name}")

        cycle_status = str(payload_cycle.get("status") or "UNKNOWN").upper()
        by_status[cycle_status] = by_status.get(cycle_status, 0) + 1
        if cycle_status not in _VALID_CYCLE_STATUSES:
            gate_blockers.append(f"cycle_status_unknown:{path.name}:{cycle_status}")
        elif cycle_status != PAPER_OK:
            gate_blockers.append(f"cycle_status_not_ok:{path.name}:{cycle_status}")

        cycle_reported_blockers = payload_cycle.get("blockers")
        if not isinstance(cycle_reported_blockers, list):
            gate_blockers.append(f"cycle_blockers_invalid:{path.name}")
        elif cycle_reported_blockers:
            gate_blockers.append(f"cycle_has_blockers:{path.name}")

        safety = payload_cycle.get("safety")
        if not isinstance(safety, Mapping) or (
            safety.get("paper_only") is not True
            or safety.get("live_trading_authorized") is not False
            or safety.get("orders_submission_unknown") is not False
        ):
            gate_blockers.append(f"cycle_safety_invalid:{path.name}")

        account_risk_raw = payload_cycle.get("account_risk")
        account_risk_snapshot = (
            _normalized_risk_snapshot(account_risk_raw)
            if isinstance(account_risk_raw, Mapping)
            else None
        )
        if account_risk_snapshot is None:
            gate_blockers.append(f"cycle_account_risk_invalid:{path.name}")
        else:
            per_day_risk_candidates.setdefault(date_key, []).append(account_risk_snapshot)
            account_risks_seen.append(account_risk_snapshot)
            if normalized_policy is not None:
                daily_pnl = _coerce_float(account_risk_snapshot.get("daily_pnl_pct"))
                drawdown = _coerce_float(account_risk_snapshot.get("current_drawdown_pct"))
                max_daily_loss = _coerce_float(normalized_policy.get("max_daily_loss_pct"))
                max_drawdown = _coerce_float(normalized_policy.get("max_drawdown_pct"))
                if (
                    daily_pnl is not None
                    and max_daily_loss is not None
                    and daily_pnl < -max_daily_loss
                ):
                    gate_blockers.append(f"cycle_daily_loss_limit_exceeded:{path.name}")
                if (
                    drawdown is not None
                    and max_drawdown is not None
                    and drawdown > max_drawdown
                ):
                    gate_blockers.append(f"cycle_drawdown_limit_exceeded:{path.name}")

        plan_reference_prices = _extract_plan_reference_prices(payload_cycle.get("plan"))
        submissions = payload_cycle.get("submissions")
        if not isinstance(submissions, list):
            gate_blockers.append(f"cycle_submissions_invalid:{path.name}")
            submissions = []
        submitted_count = 0
        errored_count = 0
        for index, submission in enumerate(submissions):
            if not isinstance(submission, Mapping):
                gate_blockers.append(f"submission_invalid:{path.name}:{index}")
                continue
            submission_status = str(submission.get("status") or "").strip().lower()
            is_submitted = submission.get("submitted") is True
            is_skipped = submission.get("skipped") is True
            if submission_status in {
                "error",
                "submit_deferred",
                "submit_unresolved",
                "cancel_unresolved",
            }:
                errored_count += 1
                gate_blockers.append(f"submission_unresolved:{path.name}:{index}:{submission_status}")
            if not is_submitted:
                if not is_skipped and submission_status not in {"skipped", "halted_after_unresolved"}:
                    gate_blockers.append(f"submission_rejected:{path.name}:{index}")
                continue
            submitted_count += 1
            client_order_id = submission.get("client_order_id")
            if not isinstance(client_order_id, str) or not client_order_id.strip():
                gate_blockers.append(f"submission_client_order_id_invalid:{path.name}:{index}")
                continue
            if client_order_id in seen_client_ids:
                gate_blockers.append(f"duplicate_client_order_id:{client_order_id}")
                continue
            seen_client_ids.add(client_order_id)
            pair = str(submission.get("pair") or "").upper()
            action = str(submission.get("action") or "").lower()
            expected_side = "buy" if action == "buy" else "sell" if action in {"sell", "sell_all"} else ""
            reference_price = plan_reference_prices.get(pair)
            if not pair or not expected_side or reference_price is None:
                gate_blockers.append(f"submission_intent_invalid:{client_order_id}")
            submitted_records.append(
                {
                    "date": date_key,
                    "sleeve": sleeve,
                    "client_order_id": client_order_id,
                    "symbol": pair,
                    "side": expected_side,
                    "reference_price": reference_price,
                }
            )

        attestation = payload_cycle.get("dataset_attestation")
        if not isinstance(attestation, Mapping) or not _valid_dataset_attestation(
            attestation,
            submission_requested=submitted_count > 0,
        ):
            gate_blockers.append(f"cycle_dataset_attestation_invalid:{path.name}")
        if isinstance(safety, Mapping):
            if safety.get("orders_submitted") is not (submitted_count > 0):
                gate_blockers.append(f"cycle_safety_submission_mismatch:{path.name}")
            if submitted_count > 0 and safety.get("confirm_submit") is not True:
                gate_blockers.append(f"cycle_safety_confirmation_invalid:{path.name}")

        day_entry = per_day_map.setdefault(
            date_key,
            {
                "date": date_key,
                "sleeves": [],
                "sleeves_status": [],
                "orders_submitted": 0,
                "orders_errored": 0,
                "account_risk": None,
            },
        )
        if sleeve not in day_entry["sleeves"]:  # type: ignore[operator]
            day_entry["sleeves"].append(sleeve)  # type: ignore[union-attr]
        day_entry["sleeves_status"].append({"sleeve": sleeve, "status": cycle_status})  # type: ignore[union-attr]
        day_entry["orders_submitted"] = int(day_entry["orders_submitted"]) + submitted_count  # type: ignore[arg-type]
        day_entry["orders_errored"] = int(day_entry["orders_errored"]) + errored_count  # type: ignore[arg-type]

    if not cycles:
        gate_blockers.append("no_cycles_found")
    for date_key, candidates in per_day_risk_candidates.items():
        if date_key in per_day_map:
            per_day_map[date_key]["account_risk"] = max(candidates, key=_drawdown_pct)
    per_day = [per_day_map[key] for key in sorted(per_day_map)]
    days = sorted(per_day_map)
    risk_track = _summarize_risk_track(account_risks_seen, per_day_risk_candidates)

    if normalized_policy is not None:
        required_sleeves = set(normalized_policy.get("required_sleeves") or [])
        min_days = normalized_policy.get("min_complete_days")
        if isinstance(min_days, int) and len(days) < min_days:
            gate_blockers.append("insufficient_complete_days")
        for day_entry in per_day:
            day_sleeves = {str(item).lower() for item in day_entry.get("sleeves", [])}
            if day_sleeves != required_sleeves:
                gate_blockers.append(f"required_sleeves_incomplete:{day_entry.get('date')}")
            if day_entry.get("date") not in allocation_dates:
                gate_blockers.append(f"allocation_missing:{day_entry.get('date')}")

    activities: list[Any] = []
    activity_window_start: datetime | None = None
    activity_window_end: datetime | None = None
    activity_snapshot_complete = False
    if submitted_records:
        if broker is None:
            gate_blockers.append("broker_required_for_submitted_orders")
        elif not hasattr(broker, "list_fill_activities"):
            gate_blockers.append("fill_activity_source_unavailable")
        elif start is None or end is None:
            gate_blockers.append("fill_activity_window_unavailable")
        else:
            activity_window_start = datetime.combine(start, time.min, tzinfo=UTC)
            activity_window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
            try:
                activities = list(
                    broker.list_fill_activities(
                        after=activity_window_start,
                        until=activity_window_end,
                    )
                )
                activity_snapshot_complete = True
            except Exception as exc:  # noqa: BLE001
                gate_blockers.append(f"fill_activity_read_failed:{type(exc).__name__}")

    activities_by_order: dict[str, list[Any]] = {}
    seen_activity_ids: set[str] = set()
    for activity in activities:
        order_id = str(_coerce_position_value(activity, "order_id", default=""))
        activity_id = str(_coerce_position_value(activity, "activity_id", default=""))
        if not order_id or not activity_id:
            gate_blockers.append("fill_activity_identity_invalid")
            continue
        if activity_id in seen_activity_ids:
            gate_blockers.append(f"duplicate_fill_activity_id:{activity_id}")
            continue
        seen_activity_ids.add(activity_id)
        activities_by_order.setdefault(order_id, []).append(activity)
        if evidence_ledger is not None and ledger_policy is not None:
            try:
                evidence_ledger.record_fill(
                    activity_id,
                    ledger_policy.policy_sha256,
                    _ledger_fill_payload(activity),
                )
            except ExecutionEvidenceLedgerError as exc:
                gate_blockers.append(
                    f"execution_fill_ledger_write_failed:{activity_id}:{type(exc).__name__}"
                )

    if (
        evidence_ledger is not None
        and ledger_policy is not None
        and activity_snapshot_complete
        and activity_window_start is not None
        and activity_window_end is not None
    ):
        try:
            evidence_ledger.record_manifest(
                _gate_manifest_id(
                    ledger_policy.policy_sha256,
                    captured_at=generated,
                    activity_ids=sorted(seen_activity_ids),
                ),
                ledger_policy.policy_sha256,
                window_start=activity_window_start,
                window_end=activity_window_end,
                captured_at=generated,
                expected_activity_ids=sorted(seen_activity_ids),
                metadata={
                    "source": "alpaca_account_activities_fill",
                    "pagination_complete": True,
                    "evidence_scope": EVIDENCE_SCOPE,
                    "paper_fees_modeled": False,
                },
            )
        except ExecutionEvidenceLedgerError as exc:
            gate_blockers.append(f"execution_manifest_write_failed:{type(exc).__name__}")

    broker_order_ids: dict[str, str] = {}
    for record in submitted_records:
        client_order_id = str(record["client_order_id"])
        if broker is None:
            continue
        snapshot, error_label = _lookup_submission_order(broker, client_order_id)
        if error_label is not None:
            gate_blockers.append(error_label)
            continue
        assert snapshot is not None
        broker_order_id = str(snapshot.get("order_id") or snapshot.get("id") or "")
        snapshot_client_id = str(snapshot.get("client_order_id") or "")
        symbol = str(snapshot.get("symbol") or "").upper()
        side = str(snapshot.get("side") or "").lower()
        status = str(snapshot.get("status") or "").lower()
        if (
            not broker_order_id
            or snapshot_client_id != client_order_id
            or _asset_key(symbol) != _asset_key(str(record["symbol"]))
            or side != record["side"]
        ):
            gate_blockers.append(f"broker_order_identity_mismatch:{client_order_id}")
        previous_client = broker_order_ids.get(broker_order_id)
        if previous_client is not None and previous_client != client_order_id:
            gate_blockers.append(f"duplicate_broker_order_id:{broker_order_id}")
        broker_order_ids[broker_order_id] = client_order_id
        if status not in _TERMINAL_STATUSES:
            gate_blockers.append(f"broker_order_not_terminal:{client_order_id}:{status or 'missing'}")
        elif status != "filled":
            gate_blockers.append(f"broker_order_not_fully_filled:{client_order_id}:{status}")

        filled_quantity = _coerce_float(snapshot.get("filled_quantity"))
        filled_avg_price = _coerce_float(snapshot.get("filled_avg_price"))
        if filled_quantity is None or filled_quantity < 0:
            gate_blockers.append(f"broker_filled_quantity_invalid:{client_order_id}")
            filled_quantity = 0.0
        order_activities = sorted(
            activities_by_order.get(broker_order_id, []),
            key=lambda item: (
                str(_coerce_position_value(item, "transaction_time", default="")),
                str(_coerce_position_value(item, "activity_id", default="")),
            ),
        )
        if filled_quantity > 0 and not order_activities:
            gate_blockers.append(f"fill_activity_missing:{client_order_id}")
        if filled_quantity <= 0 and order_activities:
            gate_blockers.append(f"fill_activity_quantity_mismatch:{client_order_id}")
        if status == "filled" and filled_quantity <= 0:
            gate_blockers.append(f"filled_order_has_zero_quantity:{client_order_id}")
        if status == "rejected":
            gate_blockers.append(f"broker_order_rejected:{client_order_id}")

        quantity_sum = 0.0
        value_sum = 0.0
        for activity in order_activities:
            activity_id = str(_coerce_position_value(activity, "activity_id", default=""))
            activity_symbol = str(_coerce_position_value(activity, "symbol", default="")).upper()
            activity_side = str(_coerce_position_value(activity, "side", default="")).lower()
            activity_qty = _coerce_float(_coerce_position_value(activity, "quantity"))
            activity_price = _coerce_float(_coerce_position_value(activity, "price"))
            transaction_time = str(_coerce_position_value(activity, "transaction_time", default=""))
            activity_cumulative = _coerce_float(
                _coerce_position_value(activity, "cumulative_quantity")
            )
            activity_leaves = _coerce_float(
                _coerce_position_value(activity, "leaves_quantity")
            )
            if (
                _asset_key(activity_symbol) != _asset_key(symbol)
                or activity_side != side
                or activity_qty is None
                or activity_qty <= 0
                or activity_price is None
                or activity_price <= 0
            ):
                gate_blockers.append(f"fill_activity_payload_mismatch:{activity_id or client_order_id}")
                continue
            try:
                activity_timestamp = datetime.fromisoformat(
                    transaction_time.replace("Z", "+00:00")
                )
            except ValueError:
                activity_timestamp = None
            if (
                activity_timestamp is None
                or activity_timestamp.tzinfo is None
                or activity_timestamp.utcoffset() is None
                or activity_window_start is None
                or activity_window_end is None
                or activity_timestamp < activity_window_start
                or activity_timestamp >= activity_window_end
            ):
                gate_blockers.append(f"fill_activity_timestamp_outside_window:{activity_id}")
            quantity_sum += activity_qty
            value_sum += activity_qty * activity_price
            if (
                activity_cumulative is None
                or activity_cumulative <= 0
                or abs(activity_cumulative - quantity_sum)
                > max(1e-9, abs(quantity_sum) * 1e-9)
                or activity_leaves is None
                or activity_leaves < 0
            ):
                gate_blockers.append(f"fill_activity_sequence_invalid:{activity_id}")
            snapshot_quantity = _coerce_float(snapshot.get("quantity"))
            if (
                snapshot_quantity is not None
                and activity_cumulative is not None
                and activity_leaves is not None
                and abs(activity_cumulative + activity_leaves - snapshot_quantity)
                > max(1e-9, abs(snapshot_quantity) * 1e-9)
            ):
                gate_blockers.append(f"fill_activity_leaves_mismatch:{activity_id}")
            try:
                cost = execution_cost_components(
                    side=side,
                    quantity=activity_qty,
                    decision_mid=record["reference_price"],
                    fill_price=activity_price,
                )
                cost_bps = float(cost.implementation_shortfall_bps)
                latency_ms = execution_latency_ms(
                    submitted_at=snapshot.get("submitted_at"),
                    filled_at=transaction_time,
                )
            except ExecutionCostEvidenceError as exc:
                gate_blockers.append(
                    f"execution_cost_evidence_invalid:{client_order_id}:{type(exc).__name__}"
                )
                cost_bps = None
                latency_ms = None
            fill_record = {
                "date": record["date"],
                "sleeve": record["sleeve"],
                "activity_id": activity_id,
                "broker_order_id": broker_order_id,
                "client_order_id": client_order_id,
                "symbol": activity_symbol,
                "side": activity_side,
                "order_status": status,
                "filled_quantity": activity_qty,
                "filled_avg_price": activity_price,
                "filled_at": transaction_time,
                "reference_price": record["reference_price"],
                "price_shortfall_bps": cost_bps,
                "effective_cost_bps": cost_bps,
                "implementation_shortfall_bps": None,
                "fee_cost_usd": None,
                "fee_source": "UNAVAILABLE_PAPER_NOT_MODELED",
                "broker_latency_ms": latency_ms,
            }
            fills.append(fill_record)
            if cost_bps is not None:
                effective_costs.append(cost_bps)
                effective_costs_by_sleeve.setdefault(str(record["sleeve"]), []).append(
                    cost_bps
                )

        if order_activities:
            if abs(quantity_sum - filled_quantity) > max(1e-9, abs(filled_quantity) * 1e-9):
                gate_blockers.append(f"fill_quantity_sum_mismatch:{client_order_id}")
            activity_vwap = value_sum / quantity_sum if quantity_sum > 0 else None
            if (
                filled_avg_price is None
                or activity_vwap is None
                or abs(activity_vwap - filled_avg_price) > max(1e-8, abs(filled_avg_price) * 1e-8)
            ):
                gate_blockers.append(f"fill_vwap_mismatch:{client_order_id}")
            last_cumulative = _coerce_float(
                _coerce_position_value(order_activities[-1], "cumulative_quantity")
            )
            if last_cumulative is None or abs(last_cumulative - quantity_sum) > max(
                1e-9, abs(quantity_sum) * 1e-9
            ):
                gate_blockers.append(f"fill_cumulative_quantity_mismatch:{client_order_id}")
            order_quantity = _coerce_float(snapshot.get("quantity"))
            if order_quantity is not None and abs(order_quantity - filled_quantity) > max(
                1e-9, abs(order_quantity) * 1e-9
            ):
                gate_blockers.append(f"order_not_fully_filled:{client_order_id}")
        orders.append(
            {
                "date": record["date"],
                "sleeve": record["sleeve"],
                "client_order_id": client_order_id,
                "broker_order_id": broker_order_id,
                "symbol": symbol,
                "side": side,
                "status": status,
                "filled_quantity": filled_quantity,
                "filled_avg_price": filled_avg_price,
                "fill_activity_count": len(order_activities),
            }
        )

    known_broker_ids = set(broker_order_ids)
    for order_id in sorted(set(activities_by_order) - known_broker_ids):
        gate_blockers.append(f"orphan_or_external_fill:{order_id}")

    positions_out: list[dict[str, object]] = []
    account_equity: float | None = None
    if broker is not None:
        try:
            positions_raw = broker.read_positions()
        except Exception as exc:  # noqa: BLE001
            gate_blockers.append(f"positions_read_failed:{type(exc).__name__}")
            positions_raw = []
        for position in positions_raw or []:
            symbol = str(_coerce_position_value(position, "symbol", default="")).upper()
            quantity = _coerce_float(
                _coerce_position_value(
                    position,
                    "qty",
                    default=_coerce_position_value(position, "quantity"),
                )
            )
            market_value = _coerce_float(_coerce_position_value(position, "market_value"))
            unrealized_pl_raw = _coerce_position_value(position, "unrealized_pl")
            unrealized_pl = _coerce_float(unrealized_pl_raw)
            if (
                not symbol
                or quantity is None
                or market_value is None
                or (unrealized_pl_raw is not None and unrealized_pl is None)
            ):
                gate_blockers.append("position_snapshot_invalid")
                continue
            entry_position = {
                "symbol": symbol,
                "qty": quantity,
                "market_value": market_value,
                "unrealized_pl": unrealized_pl,
            }
            positions_out.append(entry_position)
        try:
            account = broker.read_account()
        except Exception as exc:  # noqa: BLE001
            gate_blockers.append(f"account_read_failed:{type(exc).__name__}")
            account = None
        if account is not None:
            account_equity = _coerce_float(_coerce_position_value(account, "equity"))
            if account_equity is None or account_equity <= 0:
                gate_blockers.append("account_equity_invalid")

    cost_summary = _summarize_fills(effective_costs)
    required_sleeves = (
        list(normalized_policy.get("required_sleeves") or [])
        if normalized_policy is not None
        else sorted(effective_costs_by_sleeve)
    )
    cost_summary_by_sleeve = {
        sleeve: _summarize_fills(effective_costs_by_sleeve.get(sleeve, []))
        for sleeve in required_sleeves
    }
    if normalized_policy is not None:
        limits = normalized_policy.get("price_shortfall_limits_bps")
        if isinstance(limits, Mapping):
            for sleeve in required_sleeves:
                sleeve_summary = cost_summary_by_sleeve[sleeve]
                sleeve_limits = limits.get(sleeve)
                if not isinstance(sleeve_limits, Mapping):
                    continue
                observations = sleeve_summary.get("n")
                min_fills = sleeve_limits.get("min_reconciled_fills")
                if (
                    isinstance(observations, int)
                    and isinstance(min_fills, int)
                    and observations < min_fills
                ):
                    gate_blockers.append(f"insufficient_reconciled_fills:{sleeve}")
                median = _coerce_float(sleeve_summary.get("median"))
                p90 = _coerce_float(sleeve_summary.get("p90"))
                max_median = _coerce_float(sleeve_limits.get("max_median"))
                max_p90 = _coerce_float(sleeve_limits.get("max_p90"))
                if median is None:
                    gate_blockers.append(f"price_shortfall_median_missing:{sleeve}")
                elif max_median is not None and median > max_median:
                    gate_blockers.append(f"price_shortfall_median_exceeded:{sleeve}")
                if p90 is None:
                    gate_blockers.append(f"price_shortfall_p90_missing:{sleeve}")
                elif max_p90 is not None and p90 > max_p90:
                    gate_blockers.append(f"price_shortfall_p90_exceeded:{sleeve}")

    if evidence_ledger is not None and ledger_policy is not None:
        try:
            verification = evidence_ledger.verify(
                policy_sha256=ledger_policy.policy_sha256,
            )
            ledger_verification = {
                "configured": True,
                "policy_sha256": ledger_policy.policy_sha256,
                "valid": verification.valid,
                "integrity_valid": verification.integrity_valid,
                "completeness_valid": verification.completeness_valid,
                "operationally_clear": verification.operationally_clear,
                "issues": list(verification.issues),
                "chain_heads": dict(verification.chain_heads),
                "fill_count": verification.fill_count,
                "manifest_count": verification.manifest_count,
                "incident_event_count": verification.incident_event_count,
                "open_incident_ids": list(verification.open_incident_ids),
            }
            if not verification.integrity_valid:
                gate_blockers.append("execution_evidence_ledger_integrity_invalid")
            if not verification.completeness_valid:
                gate_blockers.append("execution_evidence_ledger_incomplete")
            if not verification.operationally_clear:
                gate_blockers.append("execution_evidence_incident_open")
        except ExecutionEvidenceLedgerError as exc:
            gate_blockers.append(f"execution_evidence_ledger_verify_failed:{type(exc).__name__}")

    gate_blockers = sorted(set(redact_secrets(item, env={}) for item in gate_blockers))
    status = PAPER_BLOCKED if gate_blockers else PAPER_OK
    canonical_policy_hash = (
        hashlib.sha256(
            json.dumps(normalized_policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()
        if normalized_policy is not None
        else None
    )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "cycles_dir": str(cycles_root),
        "window": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
        "evidence_scope": EVIDENCE_SCOPE,
        "policy": normalized_policy,
        "policy_sha256": policy_sha256 or canonical_policy_hash,
        "evidence_manifest": sorted(evidence_manifest, key=lambda item: str(item["path"])),
        "execution_evidence_ledger": ledger_verification,
        "days": days,
        "n_cycles": len(cycles),
        "by_status": dict(sorted(by_status.items())),
        "per_day": per_day,
        "orders": orders,
        "fills": fills,
        "signed_price_shortfall_bps": cost_summary,
        "signed_price_shortfall_bps_by_sleeve": cost_summary_by_sleeve,
        "effective_cost_bps": cost_summary,
        "implementation_shortfall_bps": {
            "complete": False,
            "reason": "paper_fees_and_market_benchmarks_unavailable",
        },
        "cost_coverage": {
            "individual_fill_activities": len(fills),
            "signed_price_shortfall_observations": len(effective_costs),
            "fee_source": "UNAVAILABLE_PAPER_NOT_MODELED",
            "fees_final": False,
            "spread_components_complete": False,
            "gap_components_complete": False,
            "live_cost_evidence": False,
        },
        "incidents": gate_blockers,
        "positions": positions_out,
        "account_equity": account_equity,
        "risk_track": risk_track,
        "gate": {
            "operational_pass": not gate_blockers,
            "economic_reconciliation_complete": False,
            "blockers": gate_blockers,
            "promotion_eligible": False,
            "promotion_blockers": [
                "paper_simulation_not_live_cost_evidence",
                "paper_fees_not_modeled",
            ],
        },
        "status": status,
        "safety": {
            "read_only": True,
            "orders_submitted": False,
            "paper_only": True,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
        },
    }
    payload["artifact_hash"] = compute_gate1_report_hash(payload)
    write_json_artifact(payload, output_path)
    if markdown_output is not None:
        write_text_artifact(_render_markdown(payload), markdown_output)
    return Gate1ReportResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


def _manifest_entry(path: Path, raw: bytes, *, kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _normalized_risk_snapshot(risk: Mapping[str, object]) -> dict[str, float] | None:
    equity = _coerce_float(risk.get("equity"))
    last_equity = _coerce_float(risk.get("last_equity"))
    high_water = _coerce_float(risk.get("high_water_equity"))
    daily_pnl = _coerce_float(risk.get("daily_pnl_pct"))
    drawdown = _coerce_float(risk.get("current_drawdown_pct"))
    if not (
        equity is not None
        and equity > 0
        and last_equity is not None
        and last_equity > 0
        and high_water is not None
        and high_water > 0
        and daily_pnl is not None
        and drawdown is not None
        and 0 <= drawdown <= 1
        and daily_pnl >= -1
        and high_water + 1e-8 >= equity
    ):
        return None
    expected_daily_pnl = (equity - last_equity) / last_equity
    expected_drawdown = max(0.0, (high_water - equity) / high_water)
    if not math.isclose(daily_pnl, expected_daily_pnl, rel_tol=0.0, abs_tol=1e-6) or not math.isclose(
        drawdown,
        expected_drawdown,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        return None
    return {
        "equity": equity,
        "last_equity": last_equity,
        "daily_pnl_pct": daily_pnl,
        "high_water_equity": high_water,
        "current_drawdown_pct": drawdown,
    }


def _valid_dataset_attestation(
    attestation: Mapping[str, object],
    *,
    submission_requested: bool,
) -> bool:
    blockers = attestation.get("blockers")
    source_sha256 = str(attestation.get("source_sha256") or "").lower()
    return (
        attestation.get("schema_version") == "1.1"
        and attestation.get("status") == "OK"
        and attestation.get("sidecar_status") == "OK"
        and attestation.get("published") is True
        and attestation.get("valid") is True
        and isinstance(blockers, list)
        and not blockers
        and _is_sha256(source_sha256)
        and (
            not submission_requested
            or attestation.get("eligible_for_submit") is True
        )
    )


def _is_sha256(value: object) -> bool:
    normalized = str(value or "").lower()
    return len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized)


def _asset_key(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("-", "")


def _ledger_fill_payload(activity: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "activity_id": _coerce_position_value(activity, "activity_id"),
        "order_id": _coerce_position_value(activity, "order_id"),
        "symbol": _coerce_position_value(activity, "symbol"),
        "side": _coerce_position_value(activity, "side"),
        "quantity": _coerce_position_value(activity, "quantity"),
        "price": _coerce_position_value(activity, "price"),
        "transaction_time": _coerce_position_value(activity, "transaction_time"),
    }
    for field in (
        "cumulative_quantity",
        "leaves_quantity",
        "activity_type",
        "order_status",
    ):
        value = _coerce_position_value(activity, field)
        if value is not None and value != "":
            payload[field] = value
    return payload


def _gate_manifest_id(
    policy_sha256: str,
    *,
    captured_at: str,
    activity_ids: list[str],
) -> str:
    body = json.dumps(
        {
            "activity_ids": activity_ids,
            "captured_at": captured_at,
            "policy_sha256": policy_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    suffix = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    return f"gate1-manifest-{policy_sha256[:16]}-{suffix}"


def _same_instant(left: object, right: object) -> bool:
    try:
        left_dt = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
    except ValueError:
        return False
    if (
        left_dt.tzinfo is None
        or left_dt.utcoffset() is None
        or right_dt.tzinfo is None
        or right_dt.utcoffset() is None
    ):
        return False
    return left_dt.astimezone(UTC) == right_dt.astimezone(UTC)


__all__ = [
    "Gate1ReportResult",
    "SCHEMA_VERSION",
    "compute_gate1_report_hash",
    "load_gate1_policy",
    "register_gate1_policy",
    "run_gate1_report",
]
