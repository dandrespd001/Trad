"""Configuration loading and validation for the trading AI MVP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from trading_ai.risk.policy import RiskLimits

PAPER_STAGES = {"CANARY", "SCALE_UP", "READINESS"}


class ConfigError(ValueError):
    """Raised when a configuration file is missing required safe defaults."""


@dataclass(frozen=True)
class UniverseConfig:
    name: str
    symbols: tuple[str, ...]
    asset_type: str = "etf"
    market: str = "us_equities"


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be a mapping: {config_path}")
    return loaded


def load_universe_config(path: str | Path) -> UniverseConfig:
    payload = load_yaml_file(path)
    universe = payload.get("universe", payload)
    if not isinstance(universe, dict):
        raise ConfigError("universe config must be a mapping")

    raw_symbols = universe.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ConfigError("universe.symbols must be a non-empty list")

    symbols = tuple(str(symbol).strip().upper() for symbol in raw_symbols)
    if any(not symbol for symbol in symbols):
        raise ConfigError("universe contains an empty symbol")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("universe contains duplicate symbols")

    return UniverseConfig(
        name=str(universe.get("name", "default_universe")),
        symbols=symbols,
        asset_type=str(universe.get("asset_type", "etf")),
        market=str(universe.get("market", "us_equities")),
    )


def load_risk_config(path: str | Path, *, allow_live: bool = False) -> RiskLimits:
    payload = load_yaml_file(path)
    risk_limits = payload.get("risk_limits", payload)
    if not isinstance(risk_limits, dict):
        raise ConfigError("risk_limits config must be a mapping")

    limits = RiskLimits(
        max_daily_loss_pct=_positive_fraction(risk_limits, "max_daily_loss_pct"),
        max_drawdown_pct=_positive_fraction(risk_limits, "max_drawdown_pct"),
        max_gross_exposure=_positive_fraction(risk_limits, "max_gross_exposure"),
        max_single_position=_positive_fraction(risk_limits, "max_single_position"),
        live_trading_allowed=bool(risk_limits.get("live_trading_allowed", False)),
        paper_notional_usd=_positive_float(risk_limits, "paper_notional_usd", default=1.0),
        paper_stage=str(risk_limits.get("paper_stage", "CANARY")).strip().upper(),
        paper_stage_reviewer=_optional_string(risk_limits.get("paper_stage_reviewer")),
        paper_stage_reason=_optional_string(risk_limits.get("paper_stage_reason")),
        min_signal_margin=_non_negative_float(risk_limits, "min_signal_margin", default=0.05),
        max_buy_signals=_positive_int(risk_limits, "max_buy_signals", default=3),
        max_consecutive_error_days=_non_negative_int(risk_limits, "max_consecutive_error_days", default=0),
        stop_loss_atr_mult=_non_negative_float(risk_limits, "stop_loss_atr_mult", default=0.0),
        take_profit_atr_mult=_non_negative_float(risk_limits, "take_profit_atr_mult", default=0.0),
        trailing_atr_mult=_non_negative_float(risk_limits, "trailing_atr_mult", default=0.0),
        sizing_mode=str(risk_limits.get("sizing_mode", "fixed_notional")).strip().lower(),
        target_volatility=_non_negative_float(risk_limits, "target_volatility", default=0.0),
        max_leverage=_non_negative_float(risk_limits, "max_leverage", default=1.0),
        max_price_deviation_pct=_non_negative_float(risk_limits, "max_price_deviation_pct", default=0.05),
    )
    if limits.sizing_mode not in {"fixed_notional", "vol_target"}:
        raise ConfigError("sizing_mode must be fixed_notional or vol_target")
    if limits.sizing_mode == "vol_target" and limits.target_volatility <= 0:
        raise ConfigError("vol_target sizing requires target_volatility > 0")
    if limits.live_trading_allowed and not allow_live:
        raise ConfigError("live trading cannot be enabled by default")
    if limits.max_single_position > limits.max_gross_exposure:
        raise ConfigError("max_single_position cannot exceed max_gross_exposure")
    _validate_paper_stage(limits)
    return limits


def _positive_fraction(mapping: dict[str, Any], key: str) -> float:
    if key not in mapping:
        raise ConfigError(f"missing risk limit: {key}")
    value = float(mapping[key])
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    if value > 1:
        raise ConfigError(f"{key} must be less than or equal to 1")
    return value


def _positive_float(mapping: dict[str, Any], key: str, *, default: float | None = None) -> float:
    if key not in mapping:
        if default is None:
            raise ConfigError(f"missing risk limit: {key}")
        return float(default)
    value = float(mapping[key])
    if not math.isfinite(value):
        raise ConfigError(f"{key} must be finite")
    if value <= 0:
        raise ConfigError(f"{key} must be greater than 0")
    return value


def _non_negative_float(mapping: dict[str, Any], key: str, *, default: float) -> float:
    value = float(mapping.get(key, default))
    if not math.isfinite(value):
        raise ConfigError(f"{key} must be finite")
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    return value


def _positive_int(mapping: dict[str, Any], key: str, *, default: int) -> int:
    value = int(mapping.get(key, default))
    if value < 1:
        raise ConfigError(f"{key} must be >= 1")
    return value


def _non_negative_int(mapping: dict[str, Any], key: str, *, default: int) -> int:
    value = int(mapping.get(key, default))
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_paper_stage(limits: RiskLimits) -> None:
    if limits.paper_stage not in PAPER_STAGES:
        raise ConfigError("paper_stage must be one of CANARY, SCALE_UP, READINESS")
    if limits.paper_stage == "CANARY":
        if abs(limits.paper_notional_usd - 1.0) > 1e-9:
            raise ConfigError("CANARY paper_stage requires paper_notional_usd == 1.0")
        return
    if limits.paper_stage_reviewer is None:
        raise ConfigError(f"{limits.paper_stage} paper_stage requires paper_stage_reviewer")
    if limits.paper_stage_reason is None:
        raise ConfigError(f"{limits.paper_stage} paper_stage requires paper_stage_reason")
    if limits.paper_notional_usd < 1.0 or limits.paper_notional_usd > 5.0:
        raise ConfigError(f"{limits.paper_stage} paper_stage requires 1.0 <= paper_notional_usd <= 5.0")
