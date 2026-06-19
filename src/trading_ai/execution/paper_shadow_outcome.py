"""Daily hypothetical outcome ledger for challenger shadow signals."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from trading_ai.data.io import read_records
from trading_ai.execution.paper_common import read_json_artifact, write_json_artifact, write_text_artifact


DEFAULT_OUTPUT_DIR = "reports/tmp/paper_shadow"
STATE_RECORDED = "RECORDED"
STATE_NO_SHADOW_SIGNAL = "NO_SHADOW_SIGNAL"
STATE_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PaperShadowOutcomeResult:
    exit_code: int
    state: str
    output_path: Path
    markdown_path: Path
    ledger_path: Path
    payload: dict[str, object]


def run_paper_shadow_outcome_report(
    *,
    as_of_date: str,
    signal_plan: str | Path,
    approved_dir: str | Path,
    ledger_output: str | Path,
    horizon_days: int = 1,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    generated_at: str | None = None,
) -> PaperShadowOutcomeResult:
    output_root = Path(output_dir) / as_of_date
    output_path = output_root / "shadow_outcome.json"
    markdown_path = output_root / "shadow_outcome.md"
    ledger_path = Path(ledger_output)
    signal: Mapping[str, object] | None = None
    try:
        plan = read_json_artifact(signal_plan)
        signal = _shadow_signal(plan)
        if signal is None:
            payload = _payload(as_of_date, generated_at, STATE_NO_SHADOW_SIGNAL, None, None, None, ["no_shadow_signal"], signal_plan, approved_dir)
            return _write(payload, output_path, markdown_path, ledger_path)
        rows = _read_approved_rows(Path(approved_dir))
        outcome = _outcome(signal, rows, horizon_days=horizon_days)
        payload = _payload(as_of_date, generated_at, STATE_RECORDED, signal, outcome, horizon_days, [], signal_plan, approved_dir)
        return _write(payload, output_path, markdown_path, ledger_path)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        payload = _payload(as_of_date, generated_at, STATE_BLOCKED, signal, None, horizon_days, [str(exc)], signal_plan, approved_dir)
        return _write(payload, output_path, markdown_path, ledger_path)


def _shadow_signal(plan: Mapping[str, object]) -> Mapping[str, object] | None:
    shadow = _mapping(plan.get("shadow"))
    if shadow.get("shadow_only") is not True or shadow.get("affects_paper_order") is True:
        return None
    signal = shadow.get("selected_signal")
    if isinstance(signal, Mapping) and str(signal.get("action") or "").lower() == "buy":
        return signal
    return None


def _read_approved_rows(root: Path) -> list[dict[str, object]]:
    csv_path = root / "ohlcv.csv"
    if csv_path.exists():
        return read_records(csv_path)
    parquet_path = root / "ohlcv.parquet"
    if parquet_path.exists():
        return read_records(parquet_path)
    raise ValueError(f"approved data missing ohlcv.csv or ohlcv.parquet: {root}")


def _outcome(signal: Mapping[str, object], rows: list[Mapping[str, object]], *, horizon_days: int) -> dict[str, object]:
    symbol = str(signal.get("symbol") or "").upper()
    signal_date = date.fromisoformat(str(signal.get("timestamp") or "")[:10])
    future_date = signal_date + timedelta(days=horizon_days)
    prices = {
        (str(row.get("symbol") or "").upper(), str(row.get("timestamp") or "")[:10]): float(row.get("close") or 0.0)
        for row in rows
    }
    entry = prices.get((symbol, signal_date.isoformat()))
    exit_price = prices.get((symbol, future_date.isoformat()))
    if entry is None or exit_price is None or entry <= 0:
        raise ValueError("missing_shadow_outcome_price")
    forward_return = (exit_price / entry) - 1.0
    return {
        "symbol": symbol,
        "entry_date": signal_date.isoformat(),
        "exit_date": future_date.isoformat(),
        "entry_close": entry,
        "exit_close": exit_price,
        "forward_return": forward_return,
        "forward_return_bps": forward_return * 10000.0,
        "win": forward_return > 0,
    }


def _payload(as_of_date, generated_at, state, signal, outcome, horizon_days, reasons, signal_plan, approved_dir):
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "as_of_date": as_of_date,
        "state": state,
        "shadow_signal": dict(signal) if signal is not None else None,
        "outcome": dict(outcome) if outcome is not None else None,
        "horizon_days": horizon_days,
        "sources": {"signal_plan": str(Path(signal_plan)), "approved_dir": str(Path(approved_dir))},
        "reasons": list(reasons),
        "safety": {"paper_only": True, "shadow_only": True, "orders_submitted": False, "live_trading_authorized": False},
    }


def _write(payload: dict[str, object], output_path: Path, markdown_path: Path, ledger_path: Path) -> PaperShadowOutcomeResult:
    write_json_artifact(payload, output_path)
    write_text_artifact(_render(payload), markdown_path)
    _upsert_ledger_record(_ledger_record(payload), ledger_path)
    state = str(payload.get("state") or STATE_BLOCKED)
    return PaperShadowOutcomeResult(0 if state in {STATE_RECORDED, STATE_NO_SHADOW_SIGNAL} else 1, state, output_path, markdown_path, ledger_path, payload)


def _ledger_record(payload: Mapping[str, object]) -> dict[str, object]:
    signal = _mapping(payload.get("shadow_signal"))
    symbol = str(signal.get("symbol") or "NONE").upper()
    record_id = f"{payload.get('as_of_date')}:{payload.get('horizon_days')}:{symbol}"
    return {
        "record_id": record_id,
        "record_type": "paper_shadow_outcome",
        "as_of_date": payload.get("as_of_date"),
        "state": payload.get("state"),
        "horizon_days": payload.get("horizon_days"),
        "shadow_signal": payload.get("shadow_signal"),
        "outcome": payload.get("outcome"),
        "reasons": payload.get("reasons"),
        "sources": payload.get("sources"),
    }


def _upsert_ledger_record(record: Mapping[str, object], ledger_path: Path) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    record_id = str(record.get("record_id") or "")
    existing: list[Mapping[str, object]] = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, Mapping) and str(item.get("record_id") or "") != record_id:
                existing.append(item)
    with ledger_path.open("w", encoding="utf-8") as handle:
        for item in existing:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
        handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def _render(payload: Mapping[str, object]) -> str:
    return f"# Paper Shadow Outcome\n\nState: **{payload.get('state') or STATE_BLOCKED}**\n"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
