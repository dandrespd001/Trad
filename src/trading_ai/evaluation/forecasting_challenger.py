"""Local forecasting challenger features for signal research."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import stdev
from typing import Any, cast

from trading_ai.config import load_universe_config
from trading_ai.data.io import read_records, write_records
from trading_ai.data.manifest import build_dataset_manifest
from trading_ai.execution.paper_common import write_json_artifact, write_text_artifact

SCHEMA_VERSION = 1
MODEL_ID = "local_return_forecaster_v1"
FORECAST_FEATURE_COLUMNS = (
    "forecast_return_1d",
    "forecast_volatility_5d",
    "forecast_confidence",
)


class ForecastingChallengerOperationalError(RuntimeError):
    """Raised when local forecast features cannot be produced."""


@dataclass(frozen=True)
class ForecastingChallengerResult:
    exit_code: int
    status: str
    output_dir: Path
    features_path: Path
    report_path: Path
    markdown_path: Path


def run_forecasting_challenger_report(
    *,
    as_of_date: str,
    features: str | Path,
    config: str | Path = "configs/universe.yml",
    output_dir: str | Path = "reports/tmp/forecasting_challenger",
    lookback_days: int = 5,
) -> ForecastingChallengerResult:
    if lookback_days < 1:
        raise ForecastingChallengerOperationalError("lookback_days must be positive")
    universe = load_universe_config(config)
    source_rows = read_records(features)
    rows = build_forecast_feature_rows(
        source_rows,
        allowlist=universe.symbols,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
    )
    run_dir = Path(output_dir) / as_of_date
    run_dir.mkdir(parents=True, exist_ok=True)
    features_path = run_dir / "forecast_features.csv"
    report_path = run_dir / "forecast_report.json"
    markdown_path = run_dir / "forecast_report.md"
    status = "OK" if rows else "BLOCKED"
    blockers = [] if rows else ["no_forecast_rows"]
    if rows:
        write_records(rows, features_path)
    report = _report(
        status=status,
        blockers=blockers,
        rows=rows,
        input_features=features,
        output_features=features_path,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
    )
    write_json_artifact(report, report_path)
    write_text_artifact(_render_markdown(report), markdown_path)
    return ForecastingChallengerResult(
        exit_code=0 if status == "OK" else 1,
        status=status,
        output_dir=run_dir,
        features_path=features_path,
        report_path=report_path,
        markdown_path=markdown_path,
    )


def build_forecast_feature_rows(
    rows: list[dict[str, object]],
    *,
    allowlist: tuple[str, ...],
    as_of_date: str,
    lookback_days: int = 5,
) -> list[dict[str, object]]:
    allowed = {symbol.upper() for symbol in allowlist}
    cutoff = date.fromisoformat(as_of_date)
    by_symbol: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in allowed:
            continue
        row_date = _parse_date(row.get("timestamp"))
        if row_date is None or row_date > cutoff:
            continue
        by_symbol.setdefault(symbol, []).append(dict(row))

    output: list[dict[str, object]] = []
    for symbol, symbol_rows in by_symbol.items():
        sorted_rows = sorted(symbol_rows, key=lambda row: str(row.get("timestamp") or ""))
        closes: list[float] = []
        returns: list[float] = []
        for row in sorted_rows:
            close = _float(row.get("close"))
            if close is None or not math.isfinite(close) or close <= 0:
                continue
            if closes:
                returns.append(close / closes[-1] - 1.0)
            recent_returns = returns[-lookback_days:]
            enriched = dict(row)
            forecast_return = _mean(recent_returns) if recent_returns else 0.0
            volatility = stdev(recent_returns) if len(recent_returns) >= 2 else 0.0
            confidence = _confidence(sample_count=len(recent_returns), lookback_days=lookback_days, volatility=volatility)
            enriched.update(
                {
                    "symbol": symbol,
                    "forecast_return_1d": forecast_return,
                    "forecast_volatility_5d": volatility,
                    "forecast_confidence": confidence,
                    "forecast_model_id": MODEL_ID,
                    "forecast_training_window_count": len(recent_returns),
                }
            )
            output.append(enriched)
            closes.append(close)
    return sorted(output, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or "")))


def _report(
    *,
    status: str,
    blockers: list[str],
    rows: list[dict[str, object]],
    input_features: str | Path,
    output_features: Path,
    as_of_date: str,
    lookback_days: int,
) -> dict[str, object]:
    dataset_manifest = build_dataset_manifest(rows, source=str(output_features)) if rows else {"dataset_hash": None}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "as_of_date": as_of_date,
        "model_id": MODEL_ID,
        "model_family": "local_small_forecaster",
        "lookback_days": lookback_days,
        "input_features": str(Path(input_features)),
        "output_features": str(output_features),
        "row_count": len(rows),
        "dataset_hash": dataset_manifest.get("dataset_hash"),
        "feature_columns": list(FORECAST_FEATURE_COLUMNS),
        "blockers": blockers,
        "authority": {
            "llm_authority": "none",
            "orders_submitted": False,
            "risk_changed": False,
            "mutates_latest_model": False,
        },
        "safety": {
            "paper_only": True,
            "broker_client_built": False,
            "credentials_read": False,
            "orders_submitted": False,
            "live_trading_authorized": False,
            "live_trading_allowed": False,
            "external_api_used": False,
        },
    }


def _render_markdown(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# Forecasting Challenger",
            "",
            f"- Status: `{payload.get('status')}`",
            f"- Model: `{payload.get('model_id')}`",
            f"- Rows: `{payload.get('row_count')}`",
            "",
        ]
    )


def _confidence(*, sample_count: int, lookback_days: int, volatility: float) -> float:
    sample_score = min(1.0, sample_count / max(1, lookback_days))
    volatility_penalty = 1.0 / (1.0 + 20.0 * max(0.0, volatility))
    return sample_score * volatility_penalty


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _float(value: object) -> float | None:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
