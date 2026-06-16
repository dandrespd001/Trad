"""Configuration loading and validation for the trading AI MVP."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from trading_ai.risk.policy import RiskLimits


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
    )
    if limits.live_trading_allowed and not allow_live:
        raise ConfigError("live trading cannot be enabled by default")
    return limits


def _positive_fraction(mapping: dict[str, Any], key: str) -> float:
    if key not in mapping:
        raise ConfigError(f"missing risk limit: {key}")
    value = float(mapping[key])
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    return value
