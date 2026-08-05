"""Continuous sleeve re-validation envelope (Sprint M14, WS5).

The governed sleeve cycle (§28) rebalances per day; this module performs the
*meta*-check that decides whether the live envelope is still healthy and writes
``exposure_scale`` (1.0 or 0.5) to a state JSON the launcher consumes. The
trigger is the drawdown of the real account against the validated MC p95 of
the deployed budget (§28, §34) — the rolling-Sharpe trigger was rejected as a
gate (42% false-positive rate in the validation sample) and stays
report-only alongside the re-backtest on fresh data.

Hard rules
----------
- Read-only against the broker. The only side effect is writing
  ``exposure_scale`` to a state JSON. No orders are submitted.
- The P0-03 audit invalidated the evidence behind the legacy 0.066 envelope.
  It may still trigger a conservative scale-down, but cannot authorize any
  recovery/scale-up until replacement evidence v2 is generated.
- Fail-soft: broker/datasets unreadable → incident, prior scale is preserved.
  Pure report-only status. No network in tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trading_ai.backtest.engine import BacktestConfig, run_momentum_vol_target_backtest
from trading_ai.backtest.portfolio import combine_risk_parity_sleeves
from trading_ai.data.io import read_records
from trading_ai.data.validation import validate_ohlcv_records
from trading_ai.execution.paper_common import (
    PAPER_OK,
    PAPER_WARN,
    paper_exit_code,
    write_json_artifact,
)
from trading_ai.execution.sleeve_rebalance import (
    DEFAULT_EQUITY_HIGHWATER_PATH,
    _account_risk_context,
)
from trading_ai.research.metrics import annualized_sharpe, max_drawdown

SCHEMA_VERSION = "2.0"
COST_INPUT_SEMANTICS = "all_in_charged_once_on_execution_turnover"
PROMOTION_BLOCKER_NO_TRIAL_REGISTRY = "deflated_sharpe_trial_registry_missing"
PROMOTION_BLOCKER_NO_TRADE_LEDGER = "trade_level_profit_factor_unavailable"
ENVELOPE_REFERENCE_EVIDENCE_STATUS = "INVALIDATED_P0_03"
ENVELOPE_SCALE_UP_BLOCKER = "envelope_reference_evidence_invalidated_p0_03"
# §28: MC p95 of the deployed sleeve-portfolio budget; the live DD envelope
# is computed as this fraction of ``(total_notional_usd / equity)`` so the
# unit matched the historical validation.
ENVELOPE_MC_P95_FRACTION = 0.066

DEFAULT_REVALIDATION_STATE_PATH = "reports/tmp/sleeve_rebalance/revalidation_state.json"
DEFAULT_EQUITY_TRACK_PATH = "reports/tmp/sleeve_rebalance/equity_track.csv"

# Sleeve specs (M14). These match _sleeve_backtest / _sleeve_allocate so the
# re-backtest reuses the same plumbing without drift.
ETF_SPEC: dict[str, object] = {"cost_bps": 1.0, "momentum_window": 20, "periods_per_year": 252}
CRYPTO_SPEC: dict[str, object] = {"cost_bps": 25.0, "momentum_window": 120, "periods_per_year": 365}

EVENT_ENVELOPE_BREACHED = "envelope_breached"
# Compatibility constant for existing consumers. It is not emitted while the
# legacy reference evidence is marked INVALIDATED_P0_03.
EVENT_ENVELOPE_RECOVERED = "envelope_recovered"

SCALE_FULL = 1.0
SCALE_HALF = 0.5
ROLLING_WINDOW = 60


@dataclass(frozen=True)
class SleeveRevalidationResult:
    exit_code: int
    status: str  # "OK" | "WARN" | "BLOCKED"
    output_path: Path
    payload: dict[str, object]


def _read_revalidation_state(path: Path) -> dict[str, float | str]:
    """Read the prior ``exposure_scale`` state JSON, defaulting to 1.0 on any failure."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exposure_scale": SCALE_FULL}
    if not isinstance(payload, Mapping):
        return {"exposure_scale": SCALE_FULL}
    scale = payload.get("exposure_scale", SCALE_FULL)
    try:
        scale_value = float(scale)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        scale_value = SCALE_FULL
    return {
        "exposure_scale": scale_value,
        "since": str(payload.get("since") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _write_revalidation_state(
    path: Path,
    *,
    scale: float,
    as_of: date,
    now_iso: str,
) -> None:
    payload = {
        "exposure_scale": round(float(scale), 4),
        "since": as_of.isoformat(),
        "updated_at": now_iso,
    }
    write_json_artifact(payload, path)


def _coerce_close_path(records: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for row in records:
        symbol = str(row.get("symbol", "")).upper()
        timestamp = str(row.get("timestamp", ""))
        close = row.get("close")
        if not symbol or not timestamp or close is None or close == "":
            continue
        try:
            grouped.setdefault(symbol, {})[timestamp] = float(close)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return grouped


def _run_sleeve_backtest_for_revalidation(
    *,
    dataset_path: str | Path,
    cost_bps: float,
    momentum_window: int,
    periods_per_year: int,
) -> tuple[dict[str, float] | None, list[str]]:
    """Run the §28 momentum-vol-target backtest on a single sleeve's dataset.

    Returns ``({timestamp: return}, [incidents])``. On any failure the return
    series is ``None`` and the incidents list carries at least one
    ``dataset_unreadable:<path>`` entry — callers must propagate both.
    """
    incidents: list[str] = []
    try:
        records = read_records(dataset_path)
    except (OSError, ValueError):
        incidents.append(f"dataset_unreadable:{dataset_path}")
        return None, incidents
    validation = validate_ohlcv_records(records)
    if not validation.valid:
        for error in validation.errors:
            incidents.append(f"dataset_invalid:{dataset_path}:{error}")
        return None, incidents
    result = run_momentum_vol_target_backtest(
        records,
        BacktestConfig(
            max_single_position=0.10,
            cost_bps=cost_bps,
            # ``cost_bps`` is the sleeve spec's all-in, one-way execution
            # estimate.  It must be charged once on turnover, not duplicated
            # as an independent slippage estimate.
            slippage_bps=0.0,
            periods_per_year=periods_per_year,
            momentum_window=momentum_window,
            volatility_window=momentum_window,
        ),
    ).to_dict()
    closes: dict[str, float] = {}
    for snapshot, ret in zip(
        result["positions"],
        result["daily_returns"],
        strict=True,
    ):
        ts = str(snapshot["timestamp"])
        closes[ts] = float(ret)
    return closes, incidents


def _summarize_returns(returns: list[float]) -> dict[str, float | None]:
    """Sharpe/return gain-loss/maxdd summary; ``{}`` for sub-2 series."""
    if len(returns) < 2:
        return {}
    gp = sum(v for v in returns if v > 0)
    gl = -sum(v for v in returns if v < 0)
    return_gain_loss_ratio = (gp / gl) if gl > 0 else None
    return {
        "sharpe_full": round(float(annualized_sharpe(returns, periods_per_year=365)), 6),
        "sharpe_rolling_60d": round(
            float(annualized_sharpe(returns[-ROLLING_WINDOW:], periods_per_year=365)),
            6,
        ),
        # This ratio is computed from period returns, not a closed-trade
        # ledger.  Calling it ``profit_factor`` would overstate the evidence.
        "return_gain_loss_ratio": (
            round(float(return_gain_loss_ratio), 6)
            if return_gain_loss_ratio is not None
            else None
        ),
        # Compatibility shape for consumers of schema v1.  It is deliberately
        # null because this monitor has no closed-trade ledger.
        "profit_factor": None,
        "profit_factor_status": "UNAVAILABLE_NO_TRADE_LEDGER",
        "maxdd": round(float(max_drawdown(returns)), 6),
    }


def _promotion_evidence() -> dict[str, object]:
    """Return the stable fail-closed promotion evidence for this monitor.

    Sleeve revalidation has no durable ledger containing the number and
    dispersion of strategy trials.  A genuine deflated Sharpe ratio therefore
    cannot be computed here, and the report must not claim a promotable edge.
    This block is report-only and deliberately has no effect on the operational
    drawdown-envelope state machine.
    """

    return {
        "status": "BLOCKED",
        "promotion_eligible": False,
        "edge_promotable": False,
        "deflated_sharpe": None,
        "deflated_sharpe_status": "UNAVAILABLE_NO_TRIAL_REGISTRY",
        "trial_ledger_available": False,
        "blockers": [
            PROMOTION_BLOCKER_NO_TRIAL_REGISTRY,
            PROMOTION_BLOCKER_NO_TRADE_LEDGER,
        ],
        "report_only": True,
        "affects_operational_status": False,
        "affects_exposure_scale": False,
    }


def _build_strategy_check(
    *,
    etf_path: str | Path,
    crypto_path: str | Path,
) -> tuple[dict[str, object], list[str]]:
    """Run the §28 ETF + crypto sleeves, combine risk-parity and report metrics.

    Pure report-only: nothing is written to state, no orders, no broker
    dependency. Returns ``(strategy_check_block, incidents)`` so the caller
    can surface dataset issues without aborting the revalidation.
    """
    incidents: list[str] = []
    sleeves: dict[str, Mapping[str, float]] = {}
    sources: list[dict[str, object]] = []
    for spec, path in ((ETF_SPEC, etf_path), (CRYPTO_SPEC, crypto_path)):
        closes, sleeve_incidents = _run_sleeve_backtest_for_revalidation(
            dataset_path=path,
            cost_bps=float(spec["cost_bps"]),  # type: ignore[arg-type]
            momentum_window=int(spec["momentum_window"]),  # type: ignore[arg-type]
            periods_per_year=int(spec["periods_per_year"]),  # type: ignore[arg-type]
        )
        incidents.extend(sleeve_incidents)
        if closes is None:
            continue
        sleeves[str(path)] = closes  # type: ignore[assignment]
        sources.append(
            {
                "dataset": str(path),
                "total_one_way_cost_bps": float(spec["cost_bps"]),  # type: ignore[arg-type]
                # Compatibility alias; semantics are explicit and identical
                # to ``total_one_way_cost_bps`` rather than an added charge.
                "cost_bps": float(spec["cost_bps"]),  # type: ignore[arg-type]
                "cost_bps_alias_of": "total_one_way_cost_bps",
                "cost_input_semantics": COST_INPUT_SEMANTICS,
                "backtest_cost_bps": float(spec["cost_bps"]),  # type: ignore[arg-type]
                "backtest_slippage_bps": 0.0,
                "momentum_window": int(spec["momentum_window"]),  # type: ignore[arg-type]
                "periods_per_year": int(spec["periods_per_year"]),  # type: ignore[arg-type]
            }
        )
    if not sleeves:
        return ({"status": "unavailable", "sleeves": sources, "metrics": None}, incidents)
    combined = combine_risk_parity_sleeves(sleeves)  # start_date=None per M14 spec
    returns = list(combined.daily_returns)
    metrics = _summarize_returns(returns)
    return (
        {
            "status": "ok",
            "sleeves": sources,
            "combination": combined.sleeve_weights_note,
            "n_periods": len(returns),
            "metrics": metrics,
        },
        incidents,
    )


def _maybe_append_equity_track(
    *,
    path: Path,
    as_of: date,
    equity: float,
) -> bool:
    """Append ``as_of,equity`` to the CSV; refuse duplicates for the same date.

    Returns True if a row was appended or the file was created. A duplicate
    ``as_of`` is silently skipped (idempotent for daily revalidation).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    existing_dates: set[str] = set()
    if not is_new:
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            if "," in line:
                existing_dates.add(line.split(",", 1)[0].strip())
    if as_of.isoformat() in existing_dates:
        return False
    row = f"{as_of.isoformat()},{round(float(equity), 6)}\n"
    if is_new:
        path.write_text("date,equity\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(row)
    return True


def _summarize_equity_track(path: Path) -> dict[str, object]:
    """Read the equity-track CSV; return ``{}`` for missing/short files."""
    if not path.exists():
        return {}
    rows: list[tuple[str, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if "," not in line:
            continue
        date_str, equity_str = line.split(",", 1)
        try:
            rows.append((date_str.strip(), float(equity_str.strip())))
        except ValueError:
            continue
    if len(rows) < 2:
        return {"n_days": len(rows), "first": None, "last": None, "real_return_pct": None}
    first_date, first_equity = rows[0]
    last_date, last_equity = rows[-1]
    real_return = ((last_equity - first_equity) / first_equity) if first_equity > 0 else 0.0
    return {
        "n_days": len(rows),
        "first": {"date": first_date, "equity": round(first_equity, 6)},
        "last": {"date": last_date, "equity": round(last_equity, 6)},
        "real_return_pct": round(real_return, 6),
    }


def _render_telegram_message(
    *,
    as_of: date,
    events: list[str],
    envelope: Mapping[str, object],
    scale_before: float,
    scale_after: float,
    incidents: list[str],
    risk_context: Mapping[str, object] | None,
) -> str:
    """M9-style Telegram message for scale transitions / incidents."""
    lines: list[str] = [f"Sleeve revalidation paper {as_of.isoformat()}"]
    for event in events:
        lines.append(f"REVAL: {event}")
    threshold_dd = envelope.get("threshold_dd")
    current_dd = envelope.get("current_drawdown_pct")
    if threshold_dd is not None and current_dd is not None:
        lines.append(
            f"scale {scale_before:.1f}->{scale_after:.1f} "
            f"dd={current_dd:.4f} envelope={threshold_dd:.4f}"  # type: ignore[arg-type]
        )
    if risk_context is not None:
        equity = risk_context.get("equity")
        daily_pnl = risk_context.get("daily_pnl_pct")
        equity_text = f"${equity:.2f}" if isinstance(equity, (int, float)) else "n/a"  # type: ignore[arg-type]
        pnl_text = f"{daily_pnl * 100:.2f}%" if isinstance(daily_pnl, (int, float)) else "n/a"  # type: ignore[arg-type]
        lines.append(f"equity {equity_text} pnl_dia {pnl_text}")
    if incidents:
        lines.append("Av:")
        for incident in incidents:
            lines.append(f"- {incident}")
    return "\n".join(lines)


def run_sleeve_revalidation(
    *,
    etf_dataset: str | Path,
    crypto_dataset: str | Path,
    total_notional_usd: float,
    output: str | Path,
    telegram_artifact: str | Path | None = None,
    broker: Any | None = None,
    state_path: str | Path = DEFAULT_REVALIDATION_STATE_PATH,
    equity_track_path: str | Path = DEFAULT_EQUITY_TRACK_PATH,
    equity_highwater_path: str | Path = DEFAULT_EQUITY_HIGHWATER_PATH,
    as_of_date: date | None = None,
    generated_at: str | None = None,
) -> SleeveRevalidationResult:
    """Compute the §34 drawdown envelope and write ``exposure_scale`` state.

    Re-runs the §28 ETF + crypto backtest on the fresh datasets for reporting
    only (the trigger remains the actual drawdown against the MC p95
    envelope), then evaluates the hysteresis state machine against the live
    broker's account risk context. Returns the artifact wrapped in a
    :class:`SleeveRevalidationResult`.
    """

    output_path = Path(output)
    generated = generated_at or datetime.now(UTC).isoformat()
    as_of = as_of_date or date.today()
    state_path_obj = Path(state_path)
    high_water_path_obj = Path(equity_highwater_path)

    incidents: list[str] = []

    # 1) Re-backtest report-only.
    strategy_check, strategy_incidents = _build_strategy_check(
        etf_path=etf_dataset,
        crypto_path=crypto_dataset,
    )
    incidents.extend(strategy_incidents)
    if strategy_incidents:
        # Per §34: any unreadable dataset nulls the strategy check entirely —
        # we don't want a partial metrics block to look "good enough" for the
        # operator to trust.
        strategy_check = {
            "status": "degraded",
            "sleeves": strategy_check.get("sleeves", []),
            "metrics": None,
        }

    # 2) Envelope real (broker-driven). No broker => preserved scale + incident.
    prior_state = _read_revalidation_state(state_path_obj)
    scale_before = float(prior_state["exposure_scale"])
    scale_after = scale_before
    envelope_block: dict[str, object] = {
        "reference_evidence_status": ENVELOPE_REFERENCE_EVIDENCE_STATUS,
        "scale_up_allowed": False,
        "scale_up_blockers": [ENVELOPE_SCALE_UP_BLOCKER],
        "threshold_dd": None,
        "current_drawdown_pct": None,
        "exposure_scale_before": scale_before,
        "exposure_scale_after": scale_before,
        "events": [],
    }
    risk_context = (
        _account_risk_context(broker, high_water_path_obj) if broker is not None else None
    )
    if broker is None or risk_context is None:
        if broker is not None:
            # Broker was provided but failed to give us a real context — surface
            # this as the same incident so the operator can grep for either path.
            incidents.append("account_risk_context_unavailable")
    else:
        equity = float(risk_context["equity"])
        current_dd = float(risk_context["current_drawdown_pct"])
        if equity > 0 and total_notional_usd > 0:
            envelope_dd = ENVELOPE_MC_P95_FRACTION * (total_notional_usd / equity)
        else:
            envelope_dd = 0.0
        envelope_block = {
            "reference_evidence_status": ENVELOPE_REFERENCE_EVIDENCE_STATUS,
            "scale_up_allowed": False,
            "scale_up_blockers": [ENVELOPE_SCALE_UP_BLOCKER],
            "threshold_dd": round(envelope_dd, 6),
            "current_drawdown_pct": round(current_dd, 6),
            "exposure_scale_before": scale_before,
            "exposure_scale_after": scale_before,
            "events": [],
        }
        if scale_before >= SCALE_FULL - 1e-9 and current_dd > envelope_dd:
            scale_after = SCALE_HALF
            envelope_block["exposure_scale_after"] = scale_after
            envelope_block["events"].append(EVENT_ENVELOPE_BREACHED)  # type: ignore[union-attr]
            _write_revalidation_state(
                state_path_obj,
                scale=scale_after,
                as_of=as_of,
                now_iso=generated,
            )
        # P0-03 invalidated the evidence that produced the 0.066 reference.
        # It remains conservative enough to permit a breach-driven scale-down,
        # but must never authorize a recovery/increase until evidence v2 is
        # regenerated and this explicit blocker is removed.
        envelope_block["events"] = list(envelope_block["events"])  # type: ignore[assignment]

    # 3) Equity track.
    real_track: dict[str, object] = {}
    if risk_context is not None:
        equity_value = float(risk_context["equity"])
        _maybe_append_equity_track(
            path=Path(equity_track_path),
            as_of=as_of,
            equity=equity_value,
        )
        real_track = _summarize_equity_track(Path(equity_track_path))

    # 4) Status routing.
    envelope_events = list(envelope_block.get("events") or [])  # type: ignore[arg-type]
    breach_events = {EVENT_ENVELOPE_BREACHED}
    status = (
        PAPER_WARN
        if (envelope_events and any(event in breach_events for event in envelope_events))
        or incidents
        else PAPER_OK
    )

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "strategy_check": strategy_check,
        "promotion_evidence": _promotion_evidence(),
        "envelope": envelope_block,
        "real_track": real_track,
        "incidents": incidents,
        "status": status,
        "safety": {
            "read_only": True,
            "orders_submitted": False,
            "promotion_authorized": False,
            "live_trading_allowed": False,
        },
    }
    write_json_artifact(payload, output_path)

    # 5) Telegram artifact (M9 shape) — only on scale transition or incidents.
    if telegram_artifact is not None and (scale_after != scale_before or incidents):
        telegram_payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "as_of_date": as_of.isoformat(),
            "status": status if status != PAPER_OK else PAPER_WARN,
            "message": _render_telegram_message(
                as_of=as_of,
                events=envelope_events,
                envelope=envelope_block,
                scale_before=scale_before,
                scale_after=scale_after,
                incidents=incidents,
                risk_context=risk_context,
            ),
            "safety": {
                "paper_only": True,
                "broker_client_built": False,
                "credentials_read": False,
                "orders_submitted": False,
                "live_trading_authorized": False,
                "live_trading_allowed": False,
            },
        }
        write_json_artifact(telegram_payload, Path(telegram_artifact))

    return SleeveRevalidationResult(
        exit_code=paper_exit_code(status),
        status=status,
        output_path=output_path,
        payload=payload,
    )


__all__ = [
    "DEFAULT_REVALIDATION_STATE_PATH",
    "DEFAULT_EQUITY_TRACK_PATH",
    "COST_INPUT_SEMANTICS",
    "ENVELOPE_REFERENCE_EVIDENCE_STATUS",
    "ENVELOPE_SCALE_UP_BLOCKER",
    "ENVELOPE_MC_P95_FRACTION",
    "EVENT_ENVELOPE_BREACHED",
    "EVENT_ENVELOPE_RECOVERED",
    "PROMOTION_BLOCKER_NO_TRIAL_REGISTRY",
    "PROMOTION_BLOCKER_NO_TRADE_LEDGER",
    "SCHEMA_VERSION",
    "SleeveRevalidationResult",
    "run_sleeve_revalidation",
]
