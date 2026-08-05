"""Governed crypto-sleeve rebalance cycle (paper-only by default).

This module bridges the validated momentum-vol-target strategy (§28) to the
broker paper account: it reads fresh OHLCV data, builds the causal target
weights snapshot via :func:`compute_target_weights_snapshot`, and produces a
per-symbol rebalance plan against the broker's live positions. Orders are
report-only by default and only submitted when ``confirm_submit`` is passed
together with a non-``None`` broker (the CLI doubles down on opt-in via
``--real-paper --confirm-paper --confirm-auto-submit``).

Default behavior is fail-closed: any validation, freshness, or history
problem blocks the cycle and the payload says so explicitly. The payload's
``safety`` block is the single source of truth for whether real orders were
sent (``paper_only`` is always true because the live adapter is not wired
in this sprint).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.backtest.engine import BacktestConfig, compute_target_weights_snapshot
from trading_ai.config import ConfigError, load_risk_config, load_universe_config
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.alpaca_paper import PaperOrder
from trading_ai.execution.paper_common import (
    PAPER_BLOCKED,
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    redact_payload_json,
    redact_secrets,
)
from trading_ai.execution.paper_common import (
    write_json_artifact as _write_json_artifact,
)
from trading_ai.execution.paper_executor_ipc import (
    ExecutorTarget,
    PaperExecutorIpcError,
    PaperExecutorOutcomeUnknownError,
)
from trading_ai.execution.position_sizing import build_canary_sizing_decision

SCHEMA_VERSION = "1.0"
FETCH_ATTESTATION_SCHEMA_VERSION = "1.1"
CRYPTO_MIN_NOTIONAL_USD_DEFAULT = 10.0
MIN_DELTA_USD = 1.0
NOISE_DELTA_USD = 1.0
DEFAULT_EQUITY_HIGHWATER_PATH = "reports/tmp/sleeve_rebalance/equity_highwater.json"
# M11 sleeve circuit breaker (§33): persistent state JSON. The breaker module
# imports this constant (plus _account_risk_context) from us so we control the
# shared constants on the consumer side and avoid a circular import.
DEFAULT_BREAKER_STATE_PATH = "reports/tmp/sleeve_rebalance/breaker_state.json"

# M15 (WS?, §4 of docs/revision-operaciones-2026-07-14.md): an open order from
# THIS system for a universe pair must gate the next cycle's plan — otherwise
# Friday's weekend cycles re-emit the Monday-queued partial sells and the
# Monday open executes the same trade twice (XLF/XLI halved their objective).
# System orders hold their pair; manual/outside-system orders are surfaced as
# divergences and block a confirmed batch so they cannot collide silently.
SYSTEM_ORDER_PREFIXES: tuple[str, ...] = ("sleeve-", "breaker-")

# Limit-maker (M10): the resting side of the spread, expressed in bps from
# the live trade. A 1 bp resting limit buys 1 bp of spread AND drops the
# execution from taker (≈25 bps) to maker (≈15 bps), which is most of the
# 10 bp cost edge the §30 backtest already validates against.
LIMIT_MAKER_OFFSET_BPS = 1.0
LIMIT_WAIT_SECONDS_DEFAULT = 180
LIMIT_POLL_SECONDS = 10

# Status codes surfaced on limit-maker submissions.
STYLE_LIMIT_MAKER = "limit-maker"
STYLE_MARKET = "market"
FILLED_VIA_LIMIT = "limit"
FILLED_VIA_MARKET_FALLBACK = "market_fallback"
FILLED_VIA_LIMIT_PARTIAL = "limit_partial"

# The packaged executor intentionally exposes no opening authority yet.  This
# consumer therefore has only two governed modes: reductions may be attempted,
# or every mutation is deferred.  A future full rebalance must be a distinct,
# durable server-side workflow rather than another value added here.
EXECUTOR_MODE_REDUCE_ONLY = "reduce_only"
EXECUTOR_MODE_BLOCKED = "blocked"
_EXECUTOR_MODES = frozenset({EXECUTOR_MODE_REDUCE_ONLY, EXECUTOR_MODE_BLOCKED})


def _is_crypto_pair(pair: str) -> bool:
    """Sleeve-side crypto check (independent of the Alpaca adapter helper)."""
    return "/" in pair


@dataclass(frozen=True)
class SleeveRebalanceResult:
    exit_code: int
    status: str  # "REPORT_ONLY" | "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def write_json_artifact(payload: Mapping[str, object], path: str | Path) -> None:
    """Persist and return only a recursively redacted JSON-shaped payload.

    Broker responses, configuration errors and dataset errors all converge on
    this module boundary. Mutating dict payloads in place keeps
    ``SleeveRebalanceResult.payload`` identical to the sanitized artifact.
    """

    sanitized = redact_payload_json(payload)
    if isinstance(payload, dict):
        payload.clear()
        payload.update(sanitized)
    _write_json_artifact(sanitized, path)


@dataclass(frozen=True)
class OpenOrdersCheck:
    """Explicit result of a complete open-order snapshot attempt."""

    status: str  # "OK" | "UNAVAILABLE" | "NOT_APPLICABLE"
    summary: dict[str, dict[str, object]]
    fingerprint: str | None
    order_count: int
    divergences: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def map_broker_symbol_to_pair(symbol: str, universe_symbols: Iterable[str]) -> str | None:
    """Map an Alpaca-style broker symbol ("BTCUSD") to its universe pair ("BTC/USD").

    Broker surfaces may return either notation ("BTCUSD" positions,
    "BTC/USD" orders), so lookup is by the compacted form of both sides.
    """
    by_compact = {pair.replace("/", ""): pair for pair in universe_symbols}
    return by_compact.get(symbol.upper().replace("/", ""))


def _record_date(value: object) -> date | None:
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _dataset_contract(
    records: Iterable[Mapping[str, object]],
    *,
    universe_symbols: Iterable[str],
    as_of: date,
    max_age_days: int,
) -> tuple[list[str], dict[str, object]]:
    """Validate exact universe coverage and a common causal decision date."""

    expected = tuple(dict.fromkeys(str(symbol).upper() for symbol in universe_symbols))
    dates_by_symbol: dict[str, set[date]] = {symbol: set() for symbol in expected}
    counts: dict[str, int] = {symbol: 0 for symbol in expected}
    observed_dates: list[date] = []
    actual_symbols: set[str] = set()
    for row in records:
        symbol = str(row.get("symbol", "")).upper()
        if symbol:
            actual_symbols.add(symbol)
        observed = _record_date(row.get("timestamp"))
        if observed is None:
            continue
        observed_dates.append(observed)
        if symbol in dates_by_symbol:
            dates_by_symbol[symbol].add(observed)
            counts[symbol] += 1

    blockers: list[str] = []
    missing = sorted(set(expected) - actual_symbols)
    blockers.extend(f"dataset_universe_incomplete:{symbol}" for symbol in missing)

    global_latest = max(observed_dates) if observed_dates else None
    latest_by_symbol: dict[str, str | None] = {}
    for symbol in expected:
        symbol_dates = dates_by_symbol[symbol]
        latest = max(symbol_dates) if symbol_dates else None
        latest_by_symbol[symbol] = latest.isoformat() if latest is not None else None
        if latest is None:
            continue
        if global_latest is not None and global_latest not in symbol_dates:
            blockers.append(f"dataset_latest_bar_missing:{symbol}")
        age_days = (as_of - latest).days
        if age_days < 0:
            blockers.append(f"dataset_symbol_future:{symbol}")
        elif age_days > max_age_days:
            blockers.append(f"dataset_symbol_stale:{symbol}")

    evidence: dict[str, object] = {
        "row_count": sum(counts.values()),
        "per_symbol_row_counts": counts,
        "per_symbol_latest_dates": latest_by_symbol,
        "observed_start": min(observed_dates).isoformat() if observed_dates else None,
        "observed_end": max(observed_dates).isoformat() if observed_dates else None,
        "decision_date": global_latest.isoformat() if global_latest is not None else None,
    }
    return list(dict.fromkeys(blockers)), evidence


def _fetch_sidecar_path(dataset: str | Path) -> Path:
    dataset_path = Path(dataset)
    return dataset_path.with_name(dataset_path.name + ".fetch.json")


def _expected_provider_feed(*, asset_type: str) -> tuple[str, str]:
    if asset_type == "crypto":
        return "alpaca_crypto_data", "us"
    return "alpaca_market_data", "iex"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_attestation_not_evaluated(dataset: str | Path) -> dict[str, object]:
    return {
        "path": str(_fetch_sidecar_path(dataset)),
        "status": "NOT_EVALUATED",
        "valid": False,
        "eligible_for_submit": False,
        "blockers": [],
    }


def _validate_dataset_attestation(
    *,
    dataset: str | Path,
    universe_symbols: Iterable[str],
    asset_type: str,
    as_of: date,
    dataset_evidence: Mapping[str, object],
    submission_requested: bool,
) -> dict[str, object]:
    """Validate the producer's v1.1 publication attestation without network I/O."""

    sidecar_path = _fetch_sidecar_path(dataset)
    base: dict[str, object] = {
        "path": str(sidecar_path),
        "status": "MISSING",
        "valid": False,
        "eligible_for_submit": False,
        "blockers": ["fetch_attestation_missing"],
    }
    if not sidecar_path.is_file():
        return base

    try:
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        base["status"] = "INVALID"
        base["blockers"] = ["fetch_attestation_invalid"]
        return base
    if not isinstance(raw, Mapping):
        base["status"] = "INVALID"
        base["blockers"] = ["fetch_attestation_invalid"]
        return base

    blockers: list[str] = []
    if str(raw.get("schema_version") or "") != FETCH_ATTESTATION_SCHEMA_VERSION:
        blockers.append("fetch_attestation_invalid")
    if str(raw.get("status") or "") != "OK":
        blockers.append("fetch_attestation_status_not_ok")
    if raw.get("published") is not True:
        blockers.append("fetch_attestation_invalid")
    sidecar_blockers = raw.get("blockers")
    if not isinstance(sidecar_blockers, list) or sidecar_blockers:
        blockers.append("fetch_attestation_invalid")

    expected_symbols = tuple(str(symbol).upper() for symbol in universe_symbols)
    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, list):
        blockers.append("fetch_attestation_universe_mismatch")
    else:
        normalized_symbols = tuple(str(symbol).upper() for symbol in raw_symbols)
        if len(normalized_symbols) != len(set(normalized_symbols)) or set(normalized_symbols) != set(
            expected_symbols
        ):
            blockers.append("fetch_attestation_universe_mismatch")

    expected_provider, expected_feed = _expected_provider_feed(asset_type=asset_type)
    if str(raw.get("provider") or "") != expected_provider or str(raw.get("feed") or "") != expected_feed:
        blockers.append("fetch_attestation_provider_feed_mismatch")

    requested_start = _record_date(raw.get("start"))
    requested_end = _record_date(raw.get("end"))
    observed_start = _record_date(dataset_evidence.get("observed_start"))
    observed_end = _record_date(dataset_evidence.get("observed_end"))
    decision_date = _record_date(dataset_evidence.get("decision_date"))
    if (
        requested_start is None
        or requested_end is None
        or requested_start > requested_end
        or requested_end > as_of
        or observed_start is None
        or observed_end is None
        or decision_date is None
        or observed_start < requested_start
        or observed_end > requested_end
        or (asset_type == "crypto" and requested_end != decision_date)
    ):
        blockers.append("fetch_attestation_range_mismatch")
    if raw.get("observed_start") != dataset_evidence.get("observed_start") or raw.get(
        "observed_end"
    ) != dataset_evidence.get("observed_end"):
        blockers.append("fetch_attestation_range_mismatch")
    if raw.get("expected_latest_bar_date") != dataset_evidence.get("decision_date"):
        blockers.append("fetch_attestation_range_mismatch")

    raw_latest = raw.get("per_symbol_latest_dates")
    expected_latest = dataset_evidence.get("per_symbol_latest_dates")
    if not isinstance(raw_latest, Mapping) or dict(raw_latest) != expected_latest:
        blockers.append("fetch_attestation_range_mismatch")
    raw_counts = raw.get("per_symbol_row_counts")
    expected_counts = dataset_evidence.get("per_symbol_row_counts")
    if not isinstance(raw_counts, Mapping) or dict(raw_counts) != expected_counts:
        blockers.append("fetch_attestation_row_count_mismatch")
    expected_row_count = dataset_evidence.get("row_count")
    if isinstance(raw.get("row_count"), bool) or raw.get("row_count") != expected_row_count:
        blockers.append("fetch_attestation_row_count_mismatch")

    claimed_hash = str(raw.get("source_sha256") or "").lower()
    hash_is_valid = len(claimed_hash) == 64 and all(character in "0123456789abcdef" for character in claimed_hash)
    if not hash_is_valid:
        blockers.append("fetch_attestation_hash_mismatch")
    else:
        read_hash = str(dataset_evidence.get("source_sha256") or "").lower()
        try:
            actual_hash = _file_sha256(Path(dataset))
        except OSError:
            actual_hash = ""
        if claimed_hash != read_hash or claimed_hash != actual_hash:
            blockers.append("fetch_attestation_hash_mismatch")

    blockers = list(dict.fromkeys(blockers))
    valid = not blockers
    return {
        "path": str(sidecar_path),
        "status": "OK" if valid else "INVALID",
        "valid": valid,
        "eligible_for_submit": bool(submission_requested and valid),
        "schema_version": raw.get("schema_version"),
        "sidecar_status": raw.get("status"),
        "published": raw.get("published") is True,
        "provider": raw.get("provider"),
        "feed": raw.get("feed"),
        "source_sha256": raw.get("source_sha256"),
        "blockers": blockers,
    }


def _position_number(position: object, field: str, fallback: str | None = None) -> float:
    """Read one finite numeric position field or fail closed."""

    if isinstance(position, dict):
        raw = position.get(field)
        if raw is None and fallback is not None:
            raw = position.get(fallback)
    else:
        raw = getattr(position, field, None)
        if raw is None and fallback is not None:
            raw = getattr(position, fallback, None)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"position field {field!r} is not numeric") from None
    if not math.isfinite(value):
        raise ValueError(f"position field {field!r} is not finite")
    return value


def _read_broker_positions_by_pair(
    broker: Any,
    universe_symbols: Iterable[str],
) -> tuple[dict[str, float], dict[str, float], list[str], list[str]]:
    """Return a strict, single-read broker position snapshot.

    Unmapped, duplicate, short, or malformed positions remain visible in the
    report and block confirmed execution. They are never silently coerced to
    zero or omitted from the safety decision.
    """
    current_by_pair: dict[str, float] = {}
    quantity_by_pair: dict[str, float] = {}
    ignored: list[str] = []
    snapshot_blockers: list[str] = []
    try:
        raw_snapshot = broker.read_positions()
    except Exception as exc:  # noqa: BLE001 - failed observation must become evidence
        return (
            current_by_pair,
            quantity_by_pair,
            ignored,
            [f"positions_read_error:{type(exc).__name__}"],
        )
    if raw_snapshot is None or isinstance(raw_snapshot, (str, bytes, Mapping)):
        return (
            current_by_pair,
            quantity_by_pair,
            ignored,
            ["positions_snapshot_invalid"],
        )
    try:
        raw_positions = tuple(raw_snapshot)
    except Exception as exc:  # noqa: BLE001 - partial iteration is not a snapshot
        return (
            current_by_pair,
            quantity_by_pair,
            ignored,
            [f"positions_iteration_error:{type(exc).__name__}"],
        )
    universe_set = list(universe_symbols)
    for position in raw_positions:
        if isinstance(position, dict):
            broker_symbol = str(position.get("symbol", "")).upper()
        else:
            broker_symbol = str(getattr(position, "symbol", "")).upper()
        if not broker_symbol:
            snapshot_blockers.append("position_symbol_missing")
            continue
        pair = map_broker_symbol_to_pair(broker_symbol, universe_set)
        if pair is None:
            ignored.append(broker_symbol)
            snapshot_blockers.append(f"position_unmapped:{broker_symbol}")
            continue
        if pair in current_by_pair:
            snapshot_blockers.append(f"position_duplicate:{pair}")
            continue
        try:
            market_value = _position_number(position, "market_value")
            quantity = _position_number(position, "qty", "quantity")
        except ValueError:
            snapshot_blockers.append(f"position_numeric_invalid:{broker_symbol}")
            continue
        if abs(quantity) <= 1e-12:
            snapshot_blockers.append(f"position_quantity_zero:{broker_symbol}")
            continue
        if abs(market_value) <= 1e-12 or quantity * market_value <= 0:
            snapshot_blockers.append(f"position_market_value_inconsistent:{broker_symbol}")
            continue
        if quantity < 0:
            snapshot_blockers.append(f"short_position_unsupported:{pair}")
        current_by_pair[pair] = market_value
        quantity_by_pair[pair] = quantity
    return current_by_pair, quantity_by_pair, ignored, snapshot_blockers


def _load_high_water(path: Path) -> float | None:
    value, status = _read_high_water(path)
    return value if status == "ok" else None


def _read_high_water(path: Path) -> tuple[float | None, str]:
    if not path.is_file():
        return None, "missing"
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        value = float(payload.get("high_water_equity", 0.0))
        if not math.isfinite(value) or value <= 0:
            return None, "corrupt"
        return value, "ok"
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "corrupt"


def _store_high_water(path: Path, value: float) -> None:
    write_json_artifact(
        {
            "high_water_equity": round(float(value), 2),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        path,
    )


def _breaker_state_blocker(state_path: Path) -> str | None:
    """Return a fail-closed blocker unless state is valid, healthy ``none``."""

    if not state_path.exists():
        return "circuit_breaker_state_missing"
    try:
        import json as _json

        payload = _json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "circuit_breaker_state_corrupt"
    if not isinstance(payload, dict):
        return "circuit_breaker_state_corrupt"
    stage = payload.get("stage")
    paused = payload.get("paused")
    first_breach_at = payload.get("first_breach_at")
    updated_at = payload.get("updated_at")
    if stage not in {"none", "partial_done", "flattened"} or not isinstance(paused, bool):
        return "circuit_breaker_state_corrupt"
    if first_breach_at is not None and not isinstance(first_breach_at, str):
        return "circuit_breaker_state_corrupt"
    if not isinstance(updated_at, str):
        return "circuit_breaker_state_corrupt"
    for timestamp in (first_breach_at, updated_at):
        if timestamp is None:
            continue
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return "circuit_breaker_state_corrupt"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "circuit_breaker_state_corrupt"
    if stage == "none" and first_breach_at is not None:
        return "circuit_breaker_state_corrupt"
    if stage in {"partial_done", "flattened"} and first_breach_at is None:
        return "circuit_breaker_state_corrupt"
    if stage == "flattened" and not paused:
        return "circuit_breaker_state_corrupt"
    if paused:
        return "circuit_breaker_paused"
    if stage != "none":
        return f"circuit_breaker_active:{stage}"
    return None


def _breaker_is_paused(state_path: Path) -> bool:
    """Backward-compatible predicate; unsafe state is treated as paused."""

    return _breaker_state_blocker(state_path) is not None


def _account_risk_context(
    broker: Any,
    high_water_path: Path,
    *,
    require_existing_high_water: bool = False,
    persist_high_water: bool = True,
) -> dict[str, float] | None:
    """Real account risk inputs for the broker's kill-switch evaluation.

    Returns None when the account cannot be read or reports no equity — in
    that case the caller must NOT submit orders with fake 0.0 risk inputs,
    because that silently disarms the daily-loss and drawdown kill-switches.
    """
    try:
        account = broker.read_account()
        equity = float(getattr(account, "equity", 0.0))
    except Exception:  # noqa: BLE001 - any broker failure means "no reliable context"
        return None
    if not math.isfinite(equity) or equity <= 0:
        return None
    last_equity = float(getattr(account, "last_equity", 0.0) or 0.0)
    if not math.isfinite(last_equity) or last_equity <= 0:
        return None
    daily_pnl_pct = (equity - last_equity) / last_equity
    stored, high_water_status = _read_high_water(high_water_path)
    if require_existing_high_water and high_water_status != "ok":
        return None
    high_water = max(stored or 0.0, equity)
    current_drawdown_pct = (high_water - equity) / high_water if high_water > 0 else 0.0
    if persist_high_water:
        try:
            _store_high_water(high_water_path, high_water)
        except (OSError, TypeError, ValueError):
            return None
    return {
        "equity": round(equity, 6),
        "last_equity": round(last_equity, 6),
        "daily_pnl_pct": round(daily_pnl_pct, 6),
        "high_water_equity": round(high_water, 6),
        "current_drawdown_pct": round(current_drawdown_pct, 6),
    }


def _is_system_order_id(client_order_id: object) -> bool:
    """True iff ``client_order_id`` belongs to THIS system (sleeve or breaker).

    Manual / outside-system orders are reported as divergences by the snapshot
    reader; only system orders contribute to the per-pair pending rollup.
    Idempotent re-issues on the same ``sleeve-<date>-<pair>-<side>`` id are
    the failure mode the M15 fix targets.
    """
    cid = str(client_order_id or "")
    return cid.startswith(SYSTEM_ORDER_PREFIXES)


def _order_field(order: object, name: str, default: object = None) -> object:
    if isinstance(order, Mapping):
        return order.get(name, default)
    return getattr(order, name, default)


def _open_orders_payload(
    initial: OpenOrdersCheck,
    *,
    recheck: OpenOrdersCheck | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": initial.status,
        "complete": initial.status == "OK",
        "order_count": initial.order_count,
        "fingerprint": initial.fingerprint,
        "divergences": list(initial.divergences),
        "reasons": list(initial.reasons),
        "rechecked": recheck is not None,
        "fingerprint_unchanged": None,
    }
    if recheck is not None:
        payload["pre_submit"] = {
            "status": recheck.status,
            "complete": recheck.status == "OK",
            "order_count": recheck.order_count,
            "fingerprint": recheck.fingerprint,
            "divergences": list(recheck.divergences),
            "reasons": list(recheck.reasons),
        }
        payload["fingerprint_unchanged"] = bool(
            initial.status == "OK"
            and recheck.status == "OK"
            and initial.fingerprint == recheck.fingerprint
        )
    return payload


def _open_orders_summary(broker: Any | None, universe_symbols: Iterable[str]) -> OpenOrdersCheck:
    """Return a complete, fingerprinted snapshot or an explicit failure."""

    if broker is None:
        return OpenOrdersCheck("NOT_APPLICABLE", {}, None, 0)
    list_orders = getattr(broker, "list_orders", None)
    if not callable(list_orders):
        return OpenOrdersCheck("UNAVAILABLE", {}, None, 0, reasons=("list_orders_unavailable",))
    try:
        raw_snapshot = list_orders(status="open")
    except Exception as exc:  # noqa: BLE001 - broker failure must fail closed
        return OpenOrdersCheck(
            "UNAVAILABLE",
            {},
            None,
            0,
            reasons=(f"list_orders_error:{type(exc).__name__}",),
        )
    if raw_snapshot is None:
        return OpenOrdersCheck("UNAVAILABLE", {}, None, 0, reasons=("list_orders_returned_none",))

    if isinstance(raw_snapshot, Mapping):
        if raw_snapshot.get("complete") is not True or "orders" not in raw_snapshot:
            return OpenOrdersCheck("UNAVAILABLE", {}, None, 0, reasons=("open_orders_envelope_incomplete",))
        collection = raw_snapshot.get("orders")
    else:
        collection = raw_snapshot
    if collection is None or isinstance(collection, (str, bytes, Mapping)):
        return OpenOrdersCheck("UNAVAILABLE", {}, None, 0, reasons=("open_orders_collection_invalid",))
    try:
        open_orders = list(collection)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - partial/failed iteration is not a snapshot
        return OpenOrdersCheck(
            "UNAVAILABLE",
            {},
            None,
            0,
            reasons=(f"open_orders_iteration_error:{type(exc).__name__}",),
        )

    summary: dict[str, dict[str, object]] = {}
    divergences: list[str] = []
    normalized_orders: list[dict[str, str]] = []
    universe = tuple(universe_symbols)
    for index, order in enumerate(open_orders):
        client_order_id = str(_order_field(order, "client_order_id", "") or "")
        symbol = str(_order_field(order, "symbol", "") or "").upper()
        side = str(_order_field(order, "side", "") or "").lower()
        normalized_orders.append(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "status": str(_order_field(order, "status", "") or "").lower(),
                "notional": str(_order_field(order, "notional", "") or ""),
                "quantity": str(
                    _order_field(order, "quantity", _order_field(order, "qty", "")) or ""
                ),
            }
        )
        if not client_order_id or not symbol or side not in {"buy", "sell"}:
            divergences.append(f"open_order_unclassifiable:{index}")
            continue
        if not _is_system_order_id(client_order_id):
            divergences.append(f"external_open_order:{index}")
            continue
        pair = map_broker_symbol_to_pair(symbol, universe)
        if pair is None:
            divergences.append(f"system_open_order_symbol_unmapped:{index}")
            continue
        info = summary.setdefault(pair, {"buy_notional": 0.0, "has_open_order": False})
        info["has_open_order"] = True
        if side == "buy":
            notional = _order_field(order, "notional", None)
            try:
                value = float(notional) if notional is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                info["buy_notional"] = float(info["buy_notional"]) + value  # type: ignore[operator]
    normalized_orders.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    fingerprint = hashlib.sha256(
        json.dumps(normalized_orders, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return OpenOrdersCheck(
        "OK",
        summary,
        fingerprint,
        len(open_orders),
        divergences=tuple(divergences),
    )


def _open_orders_blockers(check: OpenOrdersCheck) -> list[str]:
    """Map snapshot uncertainty/divergence to stable public blocker codes."""

    blockers: list[str] = []
    if check.status != "OK":
        blockers.append("open_orders_snapshot_unavailable")
    if any(value.startswith("external_open_order:") for value in check.divergences):
        blockers.append("external_open_orders_present")
    if any(
        value.startswith(("open_order_unclassifiable:", "system_open_order_symbol_unmapped:"))
        for value in check.divergences
    ):
        blockers.append("unclassified_open_orders_present")
    return blockers


def _last_close(close_by_symbol: dict[str, dict[str, float]], symbol: str, dates: list[str]) -> float | None:
    closes = close_by_symbol.get(symbol)
    if not closes or not dates:
        return None
    for ts in reversed(dates):
        if ts in closes:
            return float(closes[ts])
    return None


def _build_close_by_symbol(records: Iterable[dict[str, object]]) -> dict[str, dict[str, float]]:
    by_symbol: dict[str, dict[str, float]] = {}
    for row in records:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        timestamp = str(row.get("timestamp", ""))
        close_value = row.get("close")
        if close_value is None or close_value == "":
            continue
        try:
            by_symbol.setdefault(symbol, {})[timestamp] = float(close_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return by_symbol


def _build_close_by_symbol_from_engine(
    records: Iterable[dict[str, object]],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    grouped = _build_close_by_symbol(records)
    dates = sorted({ts for closes in grouped.values() for ts in closes})
    return grouped, dates


def _build_plan_entry(
    *,
    pair: str,
    weight: float,
    current_value: float,
    target_notional: float,
    reference_price: float | None,
    position_qty: float | None = None,
    min_notional: float = CRYPTO_MIN_NOTIONAL_USD_DEFAULT,
) -> dict[str, object]:
    delta = target_notional - current_value
    abs_delta = abs(delta)
    if abs_delta < NOISE_DELTA_USD:
        action = "hold"
        notional: float | None = round(target_notional, 2)
        quantity: float | None = None
        return {
            "pair": pair,
            "action": action,
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional,
            "quantity": quantity,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    if target_notional <= 0.0 and current_value > 0.0:
        # Full exit — close by exact quantity, no min-notional hop.
        quantity_value = float(position_qty) if position_qty is not None else None
        return {
            "pair": pair,
            "action": "sell_all",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": round(current_value, 2),
            "quantity": quantity_value,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    if delta > 0:
        notional_amount = round(delta, 2)
        if notional_amount < min_notional:
            return {
                "pair": pair,
                "action": "skip_below_min",
                "target_notional": round(target_notional, 2),
                "current_notional": round(current_value, 2),
                "delta": round(delta, 2),
                "notional": notional_amount,
                "quantity": None,
                "reference_price": reference_price,
                "weight": round(weight, 6),
                "skip_reason": "below_crypto_min_notional",
            }
        return {
            "pair": pair,
            "action": "buy",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional_amount,
            "quantity": None,
            "reference_price": reference_price,
            "weight": round(weight, 6),
        }
    # delta < 0, target > 0 → partial sell
    notional_amount = round(-delta, 2)
    if notional_amount < min_notional:
        return {
            "pair": pair,
            "action": "skip_below_min",
            "target_notional": round(target_notional, 2),
            "current_notional": round(current_value, 2),
            "delta": round(delta, 2),
            "notional": notional_amount,
            "quantity": None,
            "reference_price": reference_price,
            "weight": round(weight, 6),
            "skip_reason": "below_crypto_min_notional",
        }
    quantity_amount = None
    if reference_price is not None and reference_price > 0 and position_qty is not None:
        quantity_amount = min(abs(position_qty), notional_amount / reference_price)
    return {
        "pair": pair,
        "action": "sell",
        "target_notional": round(target_notional, 2),
        "current_notional": round(current_value, 2),
        "delta": round(delta, 2),
        "notional": notional_amount,
        "quantity": quantity_amount,
        "reference_price": reference_price,
        "weight": round(weight, 6),
    }


def _executor_target_to_dict(target: ExecutorTarget | None) -> dict[str, object] | None:
    if target is None:
        return None
    return {
        "account_scope_sha256": target.account_scope_sha256,
        "policy_sha256": target.policy_sha256,
        "authz_policy_sha256": target.authz_policy_sha256,
        "run_id": target.run_id,
        "fence_epoch": target.fence_epoch,
    }


def _executor_receipt_to_dict(receipt: Any) -> dict[str, object]:
    return {
        "request_id": str(receipt.request_id),
        "operation": str(receipt.operation),
        "outcome": str(receipt.outcome),
        "target": _executor_target_to_dict(receipt.target),
    }


def _executor_outcome_unknown_to_dict(
    error: PaperExecutorOutcomeUnknownError,
) -> dict[str, object]:
    return {
        "request_id": error.request_id,
        "operation": error.operation,
        "phase": error.phase,
        "retry_allowed": False,
        "target": _executor_target_to_dict(error.target),
    }


def _record_market_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    order: PaperOrder,
    broker: Any,
    style: str | None = None,
    cap_info: dict[str, float] | None = None,
) -> dict[str, object]:
    receipt_payload: dict[str, object] | None = None
    try:
        submit_with_receipt = getattr(broker, "submit_order_with_receipt", None)
        if callable(submit_with_receipt):
            receipt = submit_with_receipt(order)
            result = receipt.result
            receipt_payload = _executor_receipt_to_dict(receipt)
        else:
            result = broker.submit_order(order)
    except PaperExecutorOutcomeUnknownError as exc:
        record = {
            "pair": pair,
            "action": action,
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "submit_unresolved",
            "reasons": ["executor_outcome_unknown"],
            "executor_outcome_unknown": _executor_outcome_unknown_to_dict(exc),
        }
        if style is not None:
            record["style"] = style
        if cap_info:
            record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
            record["original_notional"] = cap_info["original_notional"]
        return record
    except PaperExecutorIpcError as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        record = {
            "pair": pair,
            "action": action,
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "submit_deferred",
            "reasons": [
                "executor_request_not_dispatched",
                redact_secrets(error_code),
            ],
        }
        if style is not None:
            record["style"] = style
        if cap_info:
            record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
            record["original_notional"] = cap_info["original_notional"]
        return record
    except Exception as exc:  # noqa: BLE001 - broker surface
        record: dict[str, object] = {
            "pair": pair,
            "action": action,
            "client_order_id": client_order_id,
            "submitted": False,
            "skipped": False,
            "status": "error",
            "reasons": [redact_secrets(f"{type(exc).__name__}: {exc}")],
        }
        if style is not None:
            record["style"] = style
        if cap_info:
            record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
            record["original_notional"] = cap_info["original_notional"]
        return record
    accepted_attr = getattr(result, "accepted", False)
    status_attr = getattr(result, "status", "unknown")
    reasons_attr = getattr(result, "reasons", ())
    record = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": bool(accepted_attr),
        "skipped": False,
        "status": redact_secrets(status_attr),
        "reasons": _redacted_broker_reasons(reasons_attr),
    }
    if style is not None:
        record["style"] = style
    if receipt_payload is not None:
        record["executor_receipt"] = receipt_payload
    if cap_info:
        record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
        record["original_notional"] = cap_info["original_notional"]
    return record


def _record_limit_maker_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    result: Any,
    style: str,
    filled_via: str | None = None,
    style_note: str | None = None,
    limit_filled_notional: float | None = None,
    market_client_order_id: str | None = None,
    cap_info: dict[str, float] | None = None,
) -> dict[str, object]:
    accepted_attr = getattr(result, "accepted", False)
    status_attr = getattr(result, "status", "unknown")
    reasons_attr = getattr(result, "reasons", ())
    record: dict[str, object] = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": bool(accepted_attr),
        "skipped": False,
        "status": redact_secrets(status_attr),
        "reasons": _redacted_broker_reasons(reasons_attr),
        "style": style,
    }
    if market_client_order_id is not None:
        record["market_client_order_id"] = market_client_order_id
    if filled_via is not None:
        record["filled_via"] = filled_via
    if style_note is not None:
        record["style_note"] = style_note
    if limit_filled_notional is not None:
        record["limit_filled_notional"] = round(float(limit_filled_notional), 4)
    if cap_info:
        record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
        record["original_notional"] = cap_info["original_notional"]
    return record


def _redacted_broker_reasons(value: object) -> list[str]:
    items = value if isinstance(value, (tuple, list)) else (value,)
    return [redact_secrets(item) for item in items]


def _record_error_submission(
    *,
    pair: str,
    action: str,
    client_order_id: str,
    error: BaseException,
    style: str,
    filled_via: str | None = None,
    cap_info: dict[str, float] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": False,
        "skipped": False,
        "status": "error",
        "reasons": [redact_secrets(f"{type(error).__name__}: {error}")],
        "style": style,
    }
    if filled_via is not None:
        record["filled_via"] = filled_via
    if cap_info:
        record["risk_to_stop_cap"] = cap_info["risk_to_stop_cap"]
        record["original_notional"] = cap_info["original_notional"]
    return record


def _limit_maker_offset_factor(side: str) -> float:
    return 1.0 - (LIMIT_MAKER_OFFSET_BPS / 1e4) if side == "buy" else 1.0 + (LIMIT_MAKER_OFFSET_BPS / 1e4)


def _poll_limit_until_filled_or_timeout(
    *,
    broker: Any,
    limit_id: str,
    deadline: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> Any | None:
    """Poll ``broker.get_order_by_client_id(limit_id)`` until filled or deadline.

    Returns the snapshot if it reads as "filled", else ``None``. Time is read
    via the injected ``now`` clock and ``sleep`` waits between polls — both
    are fakeable for offline tests.
    """
    while now() < deadline:
        sleep(LIMIT_POLL_SECONDS)
        try:
            snapshot = broker.get_order_by_client_id(limit_id)
        except Exception:  # noqa: BLE001 - degraded broker: keep polling until deadline
            snapshot = None
        if snapshot is None:
            continue
        status = str(getattr(snapshot, "status", "") or "").lower()
        if status == "filled":
            return snapshot
    return None


def _deferred_executor_submission(
    entry: Mapping[str, object],
    *,
    as_of_date: date,
    reason: str,
) -> dict[str, object]:
    action = str(entry.get("action"))
    pair = str(entry.get("pair"))
    if action not in {"buy", "sell", "sell_all"}:
        return {
            "pair": pair,
            "action": action,
            "submitted": False,
            "skipped": True,
            "status": "skipped",
            "reasons": ("action_does_not_submit",),
        }
    side = "buy" if action == "buy" else "sell"
    client_order_id = (
        f"sleeve-{as_of_date.isoformat()}-{pair.replace('/', '')}-{side}"
    )
    return {
        "pair": pair,
        "action": action,
        "client_order_id": client_order_id,
        "submitted": False,
        "skipped": True,
        "status": "submit_deferred",
        "reasons": (reason,),
    }


def _execute_submissions(
    *,
    plan: list[dict[str, object]],
    broker: Any,
    as_of_date: date,
    universe_name: str,
    risk_context: dict[str, float] | None = None,
    gross_current: float = 0.0,
    order_style: str = "market",
    limit_wait_seconds: int = LIMIT_WAIT_SECONDS_DEFAULT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    # M12 risk-to-stop cap. Default off keeps the pre-M12 behavior byte-identical.
    risk_to_stop_enabled: bool = False,
    risk_budget_pct: float = 0.005,
    stop_loss_pct: float = 0.10,
) -> list[dict[str, object]]:
    equity = float(risk_context["equity"]) if risk_context else 0.0
    daily_pnl_pct = float(risk_context["daily_pnl_pct"]) if risk_context else 0.0
    current_drawdown_pct = float(risk_context["current_drawdown_pct"]) if risk_context else 0.0
    submissions: list[dict[str, object]] = []
    use_limit_maker = order_style == "limit-maker"
    planned_opening_notional = 0.0
    for entry in plan:
        if any(
            str(previous.get("status") or "").lower()
            in {"submit_deferred", "submit_unresolved", "cancel_unresolved", "error"}
            for previous in submissions
        ):
            submissions.append(
                {
                    "pair": entry.get("pair"),
                    "action": str(entry.get("action")),
                    "submitted": False,
                    "skipped": True,
                    "status": "halted_after_unresolved",
                    "reasons": ("prior_order_state_unresolved",),
                }
            )
            continue
        action = str(entry.get("action"))
        if action not in {"buy", "sell", "sell_all"}:
            submissions.append(
                {
                    "pair": entry.get("pair"),
                    "action": action,
                    "submitted": False,
                    "skipped": True,
                    "status": "skipped",
                    "reasons": ("action_does_not_submit",),
                }
            )
            continue
        pair = str(entry.get("pair"))
        side = "buy" if action == "buy" else "sell"
        notional_value = entry.get("notional")
        quantity_value = entry.get("quantity")
        reference_price = entry.get("reference_price")
        id_base = f"sleeve-{as_of_date.isoformat()}-{pair.replace('/', '')}-{side}"
        client_order_id = id_base  # byte-identical pre-M10 default
        order_kwargs: dict[str, Any] = {
            "symbol": pair,
            "side": side,
            "client_order_id": client_order_id,
            "reference_price": reference_price,
            "position_intent": "open" if action == "buy" else "close" if action == "sell_all" else "reduce",
        }
        # Real account risk inputs so the broker's evaluate_risk_state can
        # actually trip the daily-loss/drawdown/position kill-switches.
        order_value = 0.0
        if action in {"sell", "sell_all"} and quantity_value is not None:
            try:
                ref = float(reference_price) if reference_price is not None else 0.0
                order_value = abs(float(quantity_value)) * ref
            except (TypeError, ValueError):
                order_value = 0.0
        elif notional_value is not None:
            try:
                order_value = abs(float(notional_value))
            except (TypeError, ValueError):
                order_value = 0.0
        # M12 risk-to-stop cap (opt-in, buy/sell only — sell_all is never
        # capped so closing risk is always permitted). The cap ONLY reduces
        # order_value: it cannot increase it and never converts a hold/skip
        # into a trade. The decision is reused via build_canary_sizing_decision
        # (the existing canary helper, §37), and we read its ``cap_usd`` (the
        # stop-loss-based cap, not its first-live $1 notional).
        cap_info: dict[str, float] | None = None
        if risk_to_stop_enabled and equity > 0 and action in {"buy", "sell"}:
            decision = build_canary_sizing_decision(
                bankroll_usd=equity,
                risk_budget_pct=risk_budget_pct,
                stop_loss_pct=stop_loss_pct,
                slippage_bps=0.0,
                cost_bps=0.0,
                fixed_fees_usd=0.0,
                expected_edge_bps=0.0,
                stage_cap_usd=order_value,
            )
            cap = float(decision.cap_usd)
            # The canary decision emits *input* blockers (bankroll/
            # risk_budget/stop/stage_cap invalid) when the sizing math cannot
            # be performed, and a *post-cap* blocker (``edge_net_not_positive``)
            # when net edge is non-positive. Only the input blockers should
            # invalidate the cap itself — ``edge_net_not_positive`` describes
            # the trade's economics, which the cycle handles separately via
            # the kill-switch surface.
            input_blockers = (
                "bankroll_usd_invalid",
                "risk_budget_pct_invalid",
                "stop_loss_pct_invalid",
                "stage_cap_usd_invalid",
            )
            has_input_blocker = any(
                code in decision.blockers for code in input_blockers
            )
            if has_input_blocker or cap <= 0:
                submissions.append(
                    {
                        "pair": pair,
                        "action": action,
                        "client_order_id": client_order_id,
                        "submitted": False,
                        "skipped": True,
                        "status": "skipped",
                        "reasons": ("risk_to_stop_blocked", *decision.blockers),
                    }
                )
                continue
            if cap < order_value:
                if action == "buy" and cap < CRYPTO_MIN_NOTIONAL_USD_DEFAULT:
                    submissions.append(
                        {
                            "pair": pair,
                            "action": action,
                            "client_order_id": client_order_id,
                            "submitted": False,
                            "skipped": True,
                            "status": "skipped",
                            "reasons": ("risk_to_stop_below_min",),
                            "risk_to_stop_cap": round(cap, 2),
                            "original_notional": round(order_value, 2),
                        }
                    )
                    continue
                cap_info = {
                    "risk_to_stop_cap": round(cap, 2),
                    "original_notional": round(order_value, 2),
                }
                order_value = round(cap, 2)
        if equity > 0:
            order_kwargs["daily_pnl_pct"] = daily_pnl_pct
            order_kwargs["current_drawdown_pct"] = current_drawdown_pct
            order_kwargs["estimated_position_weight"] = order_value / equity
            projected_opening_notional = planned_opening_notional + (
                order_value if action == "buy" else 0.0
            )
            order_kwargs["projected_gross_exposure"] = (
                gross_current + projected_opening_notional
            ) / equity
            if action == "buy":
                planned_opening_notional = projected_opening_notional
        if action in {"sell", "sell_all"} and quantity_value is not None:
            quantity = abs(float(quantity_value))
            if action == "sell" and reference_price is not None and float(reference_price) > 0:
                quantity = min(quantity, order_value / float(reference_price))
            order_kwargs["quantity"] = quantity
        else:
            if notional_value is None:
                submissions.append(
                    {
                        "pair": pair,
                        "action": action,
                        "submitted": False,
                        "skipped": True,
                        "status": "skipped",
                        "reasons": ("missing_notional",),
                    }
                )
                continue
            # Use ``order_value`` (the M12-capped value when the cap fired)
            # instead of the plan's raw ``notional_value`` so the broker
            # actually receives the reduced notional.
            order_kwargs["notional"] = float(order_value)

        # ----- DEFAULT MARKET PATH (byte-identical pre-M10 behavior) -----
        # The default ``order_style="market"`` path is intentionally unchanged:
        # same ids (no ``-lim`` / ``-mkt`` suffix), same record shape, no new
        # ``style`` field. ``-lim``/``-mkt`` suffixes are only emitted on the
        # limit-maker path below.
        if not use_limit_maker or not _is_crypto_pair(pair):
            order = PaperOrder(**order_kwargs)
            record_style = STYLE_MARKET if use_limit_maker else None
            submissions.append(
                _record_market_submission(
                    pair=pair,
                    action=action,
                    client_order_id=client_order_id,
                    order=order,
                    broker=broker,
                    style=record_style,
                    cap_info=cap_info,
                )
            )
            continue

        # ----- LIMIT-MAKER PATH (crypto pairs only) -----
        limit_id = f"{id_base}-lim"

        # a. Try to read the latest live trade price; degraded broker → None.
        price: float | None
        try:
            price = broker.latest_trade_price(pair)
        except Exception:  # noqa: BLE001 - degraded broker surface
            price = None

        if price is None or price <= 0:
            # Fall back to market with a style_note explaining why we did not
            # even try a limit. Same id (no ``-lim`` suffix).
            market_kwargs = dict(order_kwargs)
            market_kwargs["client_order_id"] = id_base
            market_order = PaperOrder(**market_kwargs)
            try:
                market_result = broker.submit_order(market_order)
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=id_base,
                        result=market_result,
                        style=STYLE_MARKET,
                        style_note="limit_price_unavailable",
                        cap_info=cap_info,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=id_base,
                        error=exc,
                        style=STYLE_MARKET,
                        cap_info=cap_info,
                    )
                )
            continue

        # b. Resting limit price on the maker side of the spread.
        limit_price = round(float(price) * _limit_maker_offset_factor(side), 4)

        # c. Submit the limit order. Broker-side rejection (risk / allowlist
        # / validation) is recorded verbatim — no fallback: the rejection is
        # not a liquidity problem.
        limit_kwargs = dict(order_kwargs)
        limit_kwargs["client_order_id"] = limit_id
        limit_kwargs["order_type"] = "limit"
        limit_kwargs["limit_price"] = limit_price
        limit_order = PaperOrder(**limit_kwargs)
        try:
            limit_result = broker.submit_order(limit_order)
        except Exception as exc:  # noqa: BLE001
            submissions.append(
                _record_error_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    error=exc,
                    style=STYLE_LIMIT_MAKER,
                    cap_info=cap_info,
                )
            )
            continue
        if not bool(getattr(limit_result, "accepted", False)):
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    cap_info=cap_info,
                )
            )
            continue

        # d. Poll until filled or timeout. Time / sleep are injected so tests
        # can drive the loop deterministically without touching the wall clock.
        deadline = now() + float(limit_wait_seconds)
        filled_snapshot = _poll_limit_until_filled_or_timeout(
            broker=broker,
            limit_id=limit_id,
            deadline=deadline,
            sleep=sleep,
            now=now,
        )
        if filled_snapshot is not None:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT,
                    cap_info=cap_info,
                )
            )
            continue

        # e. Timeout → cancel and re-read state; the limit may have filled in
        # the race window between the last poll and the cancel request.
        try:
            cancel_result = broker.cancel_order(client_order_id=limit_id)
        except Exception as exc:  # noqa: BLE001 - ambiguity blocks fallback
            submissions.append(
                _record_error_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    error=exc,
                    style=STYLE_LIMIT_MAKER,
                    cap_info=cap_info,
                )
            )
            continue
        cancel_status = str(getattr(cancel_result, "status", "") or "").lower()
        cancel_accepted = bool(getattr(cancel_result, "accepted", False))
        if not cancel_accepted and cancel_status not in {"canceled", "cancelled", "expired", "rejected"}:
            submissions.append(
                {
                    "pair": pair,
                    "action": action,
                    "client_order_id": limit_id,
                    "submitted": False,
                    "skipped": False,
                    "status": "cancel_unresolved",
                    "reasons": (
                        "limit_cancel_not_accepted",
                        redact_secrets(cancel_status or "unknown"),
                    ),
                    "style": STYLE_LIMIT_MAKER,
                    **(cap_info or {}),
                }
            )
            continue
        try:
            final_snapshot = broker.get_order_by_client_id(limit_id)
        except Exception as exc:  # noqa: BLE001 - unknown order state blocks fallback
            submissions.append(
                _record_error_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    error=exc,
                    style=STYLE_LIMIT_MAKER,
                    cap_info=cap_info,
                )
            )
            continue
        final_status = ""
        if final_snapshot is not None:
            final_status = str(getattr(final_snapshot, "status", "") or "").lower()
        if final_status == "filled":
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT,
                    cap_info=cap_info,
                )
            )
            continue
        if final_status not in {"canceled", "cancelled", "expired", "rejected"}:
            submissions.append(
                {
                    "pair": pair,
                    "action": action,
                    "client_order_id": limit_id,
                    "submitted": False,
                    "skipped": False,
                    "status": "cancel_unresolved",
                    "reasons": (
                        "limit_order_not_terminal_after_cancel",
                        redact_secrets(final_status or "unknown"),
                    ),
                    "style": STYLE_LIMIT_MAKER,
                    **(cap_info or {}),
                }
            )
            continue

        # Compute the remainder using the post-cancel snapshot's fill detail.
        filled_qty = 0.0
        filled_avg: float | None = None
        if final_snapshot is not None:
            try:
                filled_qty = float(getattr(final_snapshot, "filled_quantity", 0.0) or 0.0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            filled_avg_value = getattr(final_snapshot, "filled_avg_price", None)
            if filled_avg_value is not None:
                try:
                    filled_avg = float(filled_avg_value)
                except (TypeError, ValueError):
                    filled_avg = None
        limit_filled_notional = (
            filled_qty * filled_avg if filled_qty > 0 and filled_avg is not None else 0.0
        )

        if action in {"sell", "sell_all"}:
            try:
                original_qty = float(order_kwargs.get("quantity") or 0.0)
            except (TypeError, ValueError):
                original_qty = 0.0
            remainder_qty = max(0.0, original_qty - filled_qty)
            if remainder_qty < 1e-9:
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=limit_id,
                        result=limit_result,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_LIMIT_PARTIAL,
                    )
                )
                continue
            mkt_id = f"{id_base}-mkt"
            mkt_kwargs = dict(order_kwargs)
            mkt_kwargs["client_order_id"] = mkt_id
            mkt_kwargs["quantity"] = remainder_qty
            mkt_order = PaperOrder(**mkt_kwargs)
            try:
                mkt_result = broker.submit_order(mkt_order)
                submissions.append(
                    _record_limit_maker_submission(
                        pair=pair,
                        action=action,
                        client_order_id=limit_id,
                        result=mkt_result,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                        limit_filled_notional=limit_filled_notional,
                        market_client_order_id=mkt_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=mkt_id,
                        error=exc,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                        cap_info=cap_info,
                    )
                )
            continue

        # buy / sell partial → compute remainder by notional (floor at 0).
        try:
            original_notional = float(order_value)
        except (TypeError, ValueError):
            original_notional = 0.0
        remainder = max(0.0, original_notional - limit_filled_notional)
        if remainder < MIN_DELTA_USD:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT_PARTIAL,
                    cap_info=cap_info,
                )
            )
            continue
        if remainder < CRYPTO_MIN_NOTIONAL_USD_DEFAULT:
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=limit_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_LIMIT_PARTIAL,
                    style_note="fallback_below_min",
                    cap_info=cap_info,
                )
            )
            continue
        mkt_id = f"{id_base}-mkt"
        mkt_kwargs = dict(order_kwargs)
        mkt_kwargs["client_order_id"] = mkt_id
        mkt_kwargs["notional"] = round(remainder, 2)
        mkt_order = PaperOrder(**mkt_kwargs)
        try:
            mkt_result = broker.submit_order(mkt_order)
            submissions.append(
                _record_limit_maker_submission(
                    pair=pair,
                    action=action,
                    client_order_id=limit_id,
                    result=mkt_result,
                    style=STYLE_LIMIT_MAKER,
                    filled_via=FILLED_VIA_MARKET_FALLBACK,
                    limit_filled_notional=limit_filled_notional,
                    market_client_order_id=mkt_id,
                    cap_info=cap_info,
                )
            )
        except Exception as exc:  # noqa: BLE001
                submissions.append(
                    _record_error_submission(
                        pair=pair,
                        action=action,
                        client_order_id=mkt_id,
                        error=exc,
                        style=STYLE_LIMIT_MAKER,
                        filled_via=FILLED_VIA_MARKET_FALLBACK,
                        cap_info=cap_info,
                    )
                )

    # Reference the universe through a closure-captured local to keep the
    # function signature honest; suppress unused warnings.
    _ = universe_name
    return submissions


def _exit_code_for_status(status: str) -> int:
    base = paper_exit_code(status)
    if status == "REPORT_ONLY":
        return 0
    return base


def run_sleeve_rebalance(
    *,
    universe_config: str | Path,
    risk_config: str | Path,
    dataset: str | Path,
    output: str | Path,
    notional_usd: float,
    momentum_window: int = 120,
    periods_per_year: int = 365,
    max_single_position: float = 0.10,
    max_age_days: int = 3,
    as_of_date: date | None = None,
    broker: Any | None = None,
    confirm_submit: bool = False,
    generated_at: str | None = None,
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    breaker_state_path: str | Path | None = DEFAULT_BREAKER_STATE_PATH,
    order_style: str = "market",
    limit_wait_seconds: int = LIMIT_WAIT_SECONDS_DEFAULT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    # M12 (WS3, Gate 2): opt-in risk-to-stop cap. Default off keeps the
    # pre-M12 cycle byte-identical (no params, no submission fields change
    # unless the caller opts in). The cap ONLY reduces buy/sell notionals
    # via the existing canary sizing decision (§37) — sell_all is never
    # capped, no orders are converted holds→trades by the cap.
    risk_to_stop_enabled: bool = False,
    risk_budget_pct: float = 0.005,
    stop_loss_pct: float = 0.10,
    executor_capability_mode: str | None = None,
    executor_health: Mapping[str, object] | None = None,
) -> SleeveRebalanceResult:
    """Run the governed crypto-sleeve rebalance cycle (report-only by default)."""

    output_path = Path(output)
    dataset_path = Path(dataset)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()
    blockers: list[str] = []
    if (
        executor_capability_mode is not None
        and executor_capability_mode not in _EXECUTOR_MODES
    ):
        blockers.append("invalid_executor_capability_mode")
        executor_capability_mode = EXECUTOR_MODE_BLOCKED
    executor_evidence = (
        dict(executor_health) if executor_health is not None else None
    )
    executor_payload_fragment: dict[str, object] = (
        {
            "executor": {
                "workflow": "sleeve_rebalance_reduce_only_v1",
                "capability_mode": executor_capability_mode,
                "health": executor_evidence,
                "opening_orders_attempted": False,
            }
        }
        if executor_capability_mode is not None
        else {}
    )
    submission_requested = bool(confirm_submit and broker is not None)
    dataset_attestation = _dataset_attestation_not_evaluated(dataset)
    initial_open_orders = _open_orders_summary(None, ())
    open_orders_check = _open_orders_payload(initial_open_orders)

    if order_style not in {"market", "limit-maker"}:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": ["invalid_order_style"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    if notional_usd <= 0:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": ["invalid_notional_budget"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": False,
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    try:
        universe = load_universe_config(universe_config)
        load_risk_config(risk_config, allow_live=False)
    except ConfigError as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "blockers": [f"config_error:{exc}"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    # Confirmed mutations require a present, valid and healthy breaker latch.
    # Report-only diagnostics may continue while surfacing the state blocker.
    breaker_state_blocker = (
        "circuit_breaker_state_path_missing"
        if breaker_state_path is None
        else _breaker_state_blocker(Path(breaker_state_path))
    )
    if submission_requested and breaker_state_blocker is not None:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "breaker_state_blocker": breaker_state_blocker,
            "blockers": [breaker_state_blocker],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    try:
        source_sha256_before_read = _file_sha256(dataset_path)
        raw_records = read_records(dataset_path)
        source_sha256_after_read = _file_sha256(dataset_path)
    except (OSError, ValueError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "blockers": [f"dataset_unreadable:{exc}"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    if source_sha256_before_read != source_sha256_after_read:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "plan": [],
            "blockers": ["dataset_changed_during_read"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    validation = validate_ohlcv_records(
        raw_records,
        allowed_symbols=universe.symbols,
        expected_symbols=universe.symbols,
    )
    dataset_blockers, dataset_evidence = _dataset_contract(
        raw_records,
        universe_symbols=universe.symbols,
        as_of=as_of,
        max_age_days=max_age_days,
    )
    dataset_evidence["source_sha256"] = source_sha256_after_read
    data_blockers = list(dict.fromkeys([*dataset_blockers, *validation.errors]))
    if data_blockers:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "plan": [],
            "blockers": data_blockers,
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    dataset_attestation = _validate_dataset_attestation(
        dataset=dataset,
        universe_symbols=universe.symbols,
        asset_type=universe.asset_type,
        as_of=as_of,
        dataset_evidence=dataset_evidence,
        submission_requested=submission_requested,
    )
    if submission_requested and not bool(dataset_attestation["valid"]):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "plan": [],
            "blockers": list(dataset_attestation["blockers"]),  # type: ignore[arg-type]
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": True,
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    close_by_symbol, dates = _build_close_by_symbol_from_engine(raw_records)
    if not dates:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "blockers": ["empty_dataset"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    snapshot = compute_target_weights_snapshot(
        raw_records,
        BacktestConfig(
            momentum_window=momentum_window,
            volatility_window=momentum_window,
            periods_per_year=periods_per_year,
            max_single_position=max_single_position,
        ),
    )
    if not snapshot["sufficient_history"]:
        blockers.append("insufficient_history")

    if blockers:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "blockers": blockers,
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": bool(confirm_submit),
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    raw_weights = snapshot["weights"]
    weights = {str(symbol).upper(): float(value) for symbol, value in raw_weights.items()}  # type: ignore[union-attr]
    as_of_iso = str(snapshot["as_of"])

    if broker is None:
        current_by_pair: dict[str, float] = {}
        ignored_positions: list[str] = []
        position_qty_by_pair: dict[str, float] = {}
        position_snapshot_blockers: list[str] = []
        pending_buy_by_pair: dict[str, float] = {}
        pending_order_pairs: list[str] = []
        gross_for_submission = 0.0
    else:
        initial_open_orders = _open_orders_summary(broker, universe.symbols)
        open_orders_check = _open_orders_payload(initial_open_orders)
        open_order_blockers = _open_orders_blockers(initial_open_orders)
        if submission_requested and open_order_blockers:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
                "plan": [],
                "submissions": [],
                "blockers": open_order_blockers,
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        (
            current_by_pair,
            position_qty_by_pair,
            ignored_positions,
            position_snapshot_blockers,
        ) = _read_broker_positions_by_pair(broker, universe.symbols)
        initial_position_quantities = dict(position_qty_by_pair)
        initial_ignored_positions = tuple(sorted(ignored_positions))
        if submission_requested and position_snapshot_blockers:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
                "plan": [],
                "ignored_positions": sorted(set(ignored_positions)),
                "position_snapshot_blockers": position_snapshot_blockers,
                "submissions": [],
                "blockers": position_snapshot_blockers,
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        # M15: prefer the per-pair rollup (buy_notional + has_open_order) over
        # the bare buy-only sum. The rollup is filtered to system orders only
        # (sleeve-/breaker-) so external orders never inflate
        # ``pending_buy_notional``; confirmed cycles with external orders were
        # already blocked above as broker-state divergences.
        pending_summary = initial_open_orders.summary
        pending_buy_by_pair = {
            pair: float(info["buy_notional"])
            for pair, info in pending_summary.items()
            if float(info["buy_notional"]) > 0.0
        }
        pending_order_pairs = sorted(
            pair for pair, info in pending_summary.items() if bool(info["has_open_order"])
        )
        for pair, pending_value in pending_buy_by_pair.items():
            current_by_pair[pair] = current_by_pair.get(pair, 0.0) + pending_value
        gross_for_submission = sum(abs(value) for value in current_by_pair.values())

    plan: list[dict[str, object]] = []
    all_pairs = sorted({pair.upper() for pair in universe.symbols} | set(weights) | set(current_by_pair))
    for pair in all_pairs:
        weight = weights.get(pair, 0.0)
        target_notional = float(weight) * notional_usd
        current_value = float(current_by_pair.get(pair, 0.0))
        reference_price = _last_close(close_by_symbol, pair, dates)
        # M15 (§4 of docs/revision-operaciones-2026-07-14.md): if THIS system
        # already has an open order for this pair (sleeve- or breaker-), the
        # in-flight order must settle first before we re-plan from true
        # positions. Emit a ``pending_order_hold`` entry — same shape as a
        # normal hold but with action="pending_order_hold" + a note explaining
        # the gate. ``_execute_submissions`` already routes any non-{buy,sell,
        # sell_all} action through the skipped/no-submit branch.
        if pair in pending_order_pairs:
            entry = {
                "pair": pair,
                "action": "pending_order_hold",
                "target_notional": round(target_notional, 2),
                "current_notional": round(current_value, 2),
                "delta": round(target_notional - current_value, 2),
                "notional": round(target_notional, 2),
                "quantity": None,
                "reference_price": reference_price,
                "weight": round(weight, 6),
                "note": "open_order_in_flight",
            }
            plan.append(entry)
            continue
        if weight > 0:
            entry = _build_plan_entry(
                pair=pair,
                weight=weight,
                current_value=current_value,
                target_notional=target_notional,
                reference_price=reference_price,
                position_qty=position_qty_by_pair.get(pair),
            )
        else:
            entry = _build_plan_entry(
                pair=pair,
                weight=0.0,
                current_value=current_value,
                target_notional=0.0,
                reference_price=reference_price,
                position_qty=position_qty_by_pair.get(pair),
            )
        plan.append(entry)

    risk_context: dict[str, float] | None = None
    if broker is not None:
        caller_risk_is_authoritative = executor_capability_mode is None
        risk_context = _account_risk_context(
            broker,
            Path(equity_highwater_path),
            require_existing_high_water=(
                submission_requested and caller_risk_is_authoritative
            ),
            persist_high_water=(
                submission_requested and caller_risk_is_authoritative
            ),
        )

    if (
        confirm_submit
        and broker is not None
        and risk_context is None
        and executor_capability_mode is None
    ):
        # Fail-closed: submitting with fake 0.0 daily-loss/drawdown inputs
        # silently disarms the kill-switches — block instead.
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "as_of": as_of_iso,
            "universe": universe.name,
            "dataset": str(dataset),
            "dataset_attestation": dataset_attestation,
            "open_orders_check": open_orders_check,
            "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
            "plan": plan,
            "pending_buy_notional": {pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())},
            "pending_order_pairs": list(pending_order_pairs),
            "ignored_positions": sorted(set(ignored_positions)),
            "position_snapshot_blockers": position_snapshot_blockers,
            "submissions": [],
            "account_risk": None,
            "blockers": ["account_risk_context_unavailable"],
            "status": PAPER_BLOCKED,
            **executor_payload_fragment,
            "safety": {
                "paper_only": True,
                "orders_submitted": False,
                "confirm_submit": True,
                "live_trading_authorized": False,
            },
        }
        write_json_artifact(payload, output_path)
        return SleeveRebalanceResult(
            exit_code=_exit_code_for_status(PAPER_BLOCKED),
            status=PAPER_BLOCKED,
            output_path=output_path,
            payload=payload,
        )

    orders_submitted = False
    submissions: list[dict[str, object]] = []
    if confirm_submit and broker is not None:
        dataset_attestation = _validate_dataset_attestation(
            dataset=dataset,
            universe_symbols=universe.symbols,
            asset_type=universe.asset_type,
            as_of=as_of,
            dataset_evidence=dataset_evidence,
            submission_requested=True,
        )
        if not bool(dataset_attestation["valid"]):
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
                "plan": plan,
                "pending_buy_notional": {
                    pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())
                },
                "pending_order_pairs": list(pending_order_pairs),
                "ignored_positions": sorted(set(ignored_positions)),
                "position_snapshot_blockers": position_snapshot_blockers,
                "submissions": [],
                "account_risk": risk_context,
                "blockers": list(dataset_attestation["blockers"]),  # type: ignore[arg-type]
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        pre_submit_open_orders = _open_orders_summary(broker, universe.symbols)
        open_orders_check = _open_orders_payload(
            initial_open_orders,
            recheck=pre_submit_open_orders,
        )
        recheck_blockers = _open_orders_blockers(pre_submit_open_orders)
        if initial_open_orders.fingerprint != pre_submit_open_orders.fingerprint:
            recheck_blockers.append("open_orders_snapshot_changed")
        if recheck_blockers:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
                "plan": plan,
                "pending_buy_notional": {
                    pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())
                },
                "pending_order_pairs": list(pending_order_pairs),
                "ignored_positions": sorted(set(ignored_positions)),
                "position_snapshot_blockers": position_snapshot_blockers,
                "submissions": [],
                "account_risk": risk_context,
                "blockers": recheck_blockers,
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        (
            rechecked_position_values,
            rechecked_position_quantities,
            rechecked_ignored_positions,
            rechecked_position_blockers,
        ) = _read_broker_positions_by_pair(broker, universe.symbols)
        position_recheck_blockers = list(rechecked_position_blockers)
        if (
            rechecked_position_quantities != initial_position_quantities
            or tuple(sorted(rechecked_ignored_positions)) != initial_ignored_positions
        ):
            position_recheck_blockers.append("positions_snapshot_changed")
        if position_recheck_blockers:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
                "plan": plan,
                "pending_buy_notional": {
                    pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())
                },
                "pending_order_pairs": list(pending_order_pairs),
                "ignored_positions": sorted(set(rechecked_ignored_positions)),
                "position_snapshot_blockers": position_recheck_blockers,
                "submissions": [],
                "account_risk": risk_context,
                "blockers": position_recheck_blockers,
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        pre_dispatch_breaker = (
            "circuit_breaker_state_path_missing"
            if breaker_state_path is None
            else _breaker_state_blocker(Path(breaker_state_path))
        )
        if pre_dispatch_breaker is not None:
            breaker_blocker = f"pre_dispatch_{pre_dispatch_breaker}"
            payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated,
                "as_of": as_of_iso,
                "universe": universe.name,
                "dataset": str(dataset),
                "dataset_attestation": dataset_attestation,
                "open_orders_check": open_orders_check,
                "weights": {
                    symbol: round(value, 6)
                    for symbol, value in sorted(weights.items())
                },
                "plan": plan,
                "pending_buy_notional": {
                    pair: round(value, 2)
                    for pair, value in sorted(pending_buy_by_pair.items())
                },
                "pending_order_pairs": list(pending_order_pairs),
                "ignored_positions": sorted(set(rechecked_ignored_positions)),
                "position_snapshot_blockers": [],
                "submissions": [],
                "account_risk": risk_context,
                "blockers": [breaker_blocker],
                "status": PAPER_BLOCKED,
                **executor_payload_fragment,
                "safety": {
                    "paper_only": True,
                    "orders_submitted": False,
                    "orders_attempted": False,
                    "orders_submission_unknown": False,
                    "confirm_submit": True,
                    "live_trading_authorized": False,
                },
            }
            write_json_artifact(payload, output_path)
            return SleeveRebalanceResult(
                exit_code=_exit_code_for_status(PAPER_BLOCKED),
                status=PAPER_BLOCKED,
                output_path=output_path,
                payload=payload,
            )
        gross_for_submission = sum(abs(value) for value in rechecked_position_values.values()) + sum(
            abs(value) for value in pending_buy_by_pair.values()
        )
        if executor_capability_mode == EXECUTOR_MODE_BLOCKED:
            submissions = [
                _deferred_executor_submission(
                    entry,
                    as_of_date=as_of,
                    reason="executor_mutations_unavailable",
                )
                for entry in plan
            ]
        elif (
            executor_capability_mode == EXECUTOR_MODE_REDUCE_ONLY
            and order_style != "market"
        ):
            # Limit-maker is a multi-mutation workflow (submit, cancel, inspect,
            # optional fallback).  It remains disabled until that whole state
            # machine lives durably inside the executor.
            submissions = [
                _deferred_executor_submission(
                    entry,
                    as_of_date=as_of,
                    reason="executor_limit_maker_workflow_unavailable",
                )
                for entry in plan
            ]
        else:
            submission_plan = plan
            if executor_capability_mode == EXECUTOR_MODE_REDUCE_ONLY:
                reductions = [
                    entry
                    for entry in plan
                    if str(entry.get("action")) in {"sell", "sell_all"}
                ]
                non_mutations = [
                    entry
                    for entry in plan
                    if str(entry.get("action")) not in {"buy", "sell", "sell_all"}
                ]
                # Reductions are always attempted before an opening is even
                # represented as deferred.  No buy reaches the executor from
                # this phase-one consumer.
                submission_plan = [*reductions, *non_mutations]
            submissions = _execute_submissions(
                plan=submission_plan,
                broker=broker,
                as_of_date=as_of,
                universe_name=universe.name,
                risk_context=risk_context,
                gross_current=gross_for_submission,
                order_style=order_style,
                limit_wait_seconds=limit_wait_seconds,
                sleep=sleep,
                now=now,
                risk_to_stop_enabled=risk_to_stop_enabled,
                risk_budget_pct=risk_budget_pct,
                stop_loss_pct=stop_loss_pct,
            )
            if executor_capability_mode == EXECUTOR_MODE_REDUCE_ONLY:
                submissions.extend(
                    _deferred_executor_submission(
                        entry,
                        as_of_date=as_of,
                        reason="executor_opening_orders_deferred",
                    )
                    for entry in plan
                    if str(entry.get("action")) == "buy"
                )
        # Distinguish confirmed broker acceptance from an ambiguous transport
        # outcome. A timeout/error is not evidence that nothing reached the
        # broker and therefore blocks the cycle instead of degrading to WARN.
        orders_submitted = any(submission.get("submitted") is True for submission in submissions)
        unresolved_statuses = {
            "cancel_unresolved",
            "submit_unresolved",
            "error",
        }
        unresolved_submissions = [
            submission
            for submission in submissions
            if str(submission.get("status") or "").lower() in unresolved_statuses
        ]
        any_rejected = any(
            (not submission.get("submitted", False)) and not submission.get("skipped", False)
            for submission in submissions
        )
        deferred_submissions = [
            submission
            for submission in submissions
            if str(submission.get("status") or "").lower() == "submit_deferred"
        ]
        executor_reductions = [
            submission
            for submission in submissions
            if str(submission.get("action")) in {"sell", "sell_all"}
        ]
        accepted_executor_reductions = [
            submission
            for submission in executor_reductions
            if submission.get("submitted") is True
        ]
        incomplete_executor_reductions = [
            submission
            for submission in executor_reductions
            if submission.get("submitted") is not True
        ]
        executor_cycle_requires_block = bool(
            executor_capability_mode is not None
            and (
                deferred_submissions
                or accepted_executor_reductions
                or incomplete_executor_reductions
                or "invalid_executor_capability_mode" in blockers
            )
        )
        if executor_capability_mode is not None:
            if deferred_submissions:
                blockers.append("executor_cycle_deferred")
            if accepted_executor_reductions:
                blockers.append("executor_reduction_reconciliation_pending")
            if incomplete_executor_reductions:
                blockers.append("executor_reduction_incomplete")
        if unresolved_submissions:
            blockers.append("order_state_unresolved")
            status = PAPER_BLOCKED
        elif executor_cycle_requires_block:
            status = PAPER_BLOCKED
        else:
            status = PAPER_WARN if any_rejected else PAPER_OK
    else:
        status = "REPORT_ONLY"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of_iso,
        "universe": universe.name,
        "dataset": str(dataset),
        "dataset_attestation": dataset_attestation,
        "open_orders_check": open_orders_check,
        "params": {
            "notional_usd": float(notional_usd),
            "momentum_window": int(momentum_window),
            "periods_per_year": int(periods_per_year),
            "max_single_position": float(max_single_position),
            "max_age_days": int(max_age_days),
            "order_style": str(order_style),
            # M12: only added when the operator opts into the cap, so the
            # default-off JSON output stays byte-identical to pre-M12.
            **(
                {
                    "risk_to_stop_enabled": True,
                    "risk_budget_pct": float(risk_budget_pct),
                    "stop_loss_pct": float(stop_loss_pct),
                }
                if risk_to_stop_enabled
                else {}
            ),
        },
        "weights": {symbol: round(value, 6) for symbol, value in sorted(weights.items())},
        "plan": plan,
        "pending_buy_notional": {pair: round(value, 2) for pair, value in sorted(pending_buy_by_pair.items())},
        "pending_order_pairs": list(pending_order_pairs),
        "ignored_positions": sorted(set(ignored_positions)),
        "position_snapshot_blockers": position_snapshot_blockers,
        "account_risk": risk_context,
        "breaker_state_blocker": breaker_state_blocker,
        "submissions": submissions,
        "blockers": blockers,
        "status": status,
        **executor_payload_fragment,
        "safety": {
            "paper_only": True,
            "orders_submitted": bool(orders_submitted),
            "orders_attempted": bool(
                confirm_submit
                and any(not submission.get("skipped", False) for submission in submissions)
            ),
            "orders_submission_unknown": bool(
                confirm_submit
                and any(
                    str(submission.get("status") or "").lower()
                    in {"submit_unresolved", "error"}
                    for submission in submissions
                )
            ),
            "confirm_submit": bool(confirm_submit),
            "live_trading_authorized": False,
        },
    }
    write_json_artifact(payload, output_path)

    exit_code = _exit_code_for_status(status)
    return SleeveRebalanceResult(
        exit_code=exit_code,
        status=status,
        output_path=output_path,
        payload=payload,
    )
