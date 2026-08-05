"""Configuration loading and validation for the trading AI MVP."""

from __future__ import annotations

import inspect
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from trading_ai.risk.policy import RiskLimits

PAPER_STAGES = {"CANARY", "SCALE_UP", "READINESS"}
LIVE_BYPASS_AUDIT_PATH = Path("reports/tmp/live_bypass_audit.jsonl")

_log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when a configuration file is missing required safe defaults."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConfigError("configuration mapping keys must be scalar") from exc
        if duplicate:
            raise ConfigError(f"duplicate configuration key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loader = _UniqueKeySafeLoader(handle)
            try:
                loaded = loader.get_single_data() or {}
            finally:
                loader.dispose()
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML configuration: {config_path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be a mapping: {config_path}")
    return loaded


def load_yaml_bytes(payload: bytes) -> dict[str, Any]:
    """Load strict UTF-8 YAML from already-validated immutable bytes."""

    if type(payload) is not bytes or not payload:
        raise ConfigError("configuration bytes must be non-empty")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("configuration bytes must be valid UTF-8") from exc
    try:
        loader = _UniqueKeySafeLoader(text)
        try:
            loaded = loader.get_single_data() or {}
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML configuration bytes") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("configuration byte root must be a mapping")
    return loaded


def load_universe_config(path: str | Path) -> UniverseConfig:
    return _universe_config_from_payload(load_yaml_file(path))


def load_universe_config_bytes(payload: bytes) -> UniverseConfig:
    """Parse a universe from the exact byte bundle covered by a policy hash."""

    return _universe_config_from_payload(load_yaml_bytes(payload))


def _universe_config_from_payload(payload: dict[str, Any]) -> UniverseConfig:
    universe = payload.get("universe", payload)
    if not isinstance(universe, dict):
        raise ConfigError("universe config must be a mapping")

    raw_symbols = universe.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise ConfigError("universe.symbols must be a non-empty list")
    if any(not isinstance(symbol, str) for symbol in raw_symbols):
        raise ConfigError("universe.symbols must contain only strings")

    symbols = tuple(symbol.strip().upper() for symbol in raw_symbols)
    if any(not symbol for symbol in symbols):
        raise ConfigError("universe contains an empty symbol")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("universe contains duplicate symbols")

    name = _required_config_text(universe.get("name", "default_universe"), key="universe.name")
    asset_type = _required_config_text(universe.get("asset_type", "etf"), key="universe.asset_type")
    market = _required_config_text(universe.get("market", "us_equities"), key="universe.market")
    return UniverseConfig(name=name, symbols=symbols, asset_type=asset_type, market=market)


def load_risk_config(path: str | Path, *, allow_live: bool) -> RiskLimits:
    if type(allow_live) is not bool:
        raise ConfigError("allow_live must be an explicit boolean")
    limits = _risk_config_from_payload(
        load_yaml_file(path),
        allow_live=allow_live,
    )
    if allow_live:
        _write_live_bypass_audit(path)
    return limits


def load_risk_config_bytes(payload: bytes) -> RiskLimits:
    """Parse fail-closed paper risk from policy-hashed immutable bytes."""

    return _risk_config_from_payload(load_yaml_bytes(payload), allow_live=False)


def _risk_config_from_payload(
    payload: dict[str, Any],
    *,
    allow_live: bool,
) -> RiskLimits:
    risk_limits = payload.get("risk_limits", payload)
    if not isinstance(risk_limits, dict):
        raise ConfigError("risk_limits config must be a mapping")

    limits = RiskLimits(
        max_daily_loss_pct=_positive_fraction(risk_limits, "max_daily_loss_pct"),
        max_drawdown_pct=_positive_fraction(risk_limits, "max_drawdown_pct"),
        max_gross_exposure=_positive_fraction(risk_limits, "max_gross_exposure"),
        max_single_position=_positive_fraction(risk_limits, "max_single_position"),
        live_trading_allowed=_strict_bool(
            risk_limits,
            "live_trading_allowed",
            default=False,
        ),
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
        breakeven_trigger_atr_mult=_non_negative_float(risk_limits, "breakeven_trigger_atr_mult", default=0.0),
        breakeven_buffer_atr_mult=_non_negative_float(risk_limits, "breakeven_buffer_atr_mult", default=0.0),
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


def _write_live_bypass_audit(path: str | Path) -> None:
    frames = inspect.stack(context=0)[1:3]
    caller = [
        {
            "file": _redact_path(frame.filename),
            "function": frame.function,
            "line": frame.lineno,
        }
        for frame in frames
    ]
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "path": _redact_path(str(path)),
        "allow_live": True,
        "caller": caller,
    }
    LIVE_BYPASS_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LIVE_BYPASS_AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    _log.warning("load_risk_config called with allow_live=True path=%s caller=%s", record["path"], caller)


def _redact_path(value: str) -> str:
    text = str(value)
    redacted_parts = []
    for part in Path(text).parts:
        lowered = part.lower()
        if any(token in lowered for token in ("secret", "token", "password", "apikey", "api_key")):
            redacted_parts.append("[redacted]")
        else:
            redacted_parts.append(part)
    return str(Path(*redacted_parts)) if redacted_parts else text


def _positive_fraction(mapping: dict[str, Any], key: str) -> float:
    if key not in mapping:
        raise ConfigError(f"missing risk limit: {key}")
    value = _finite_config_float(mapping[key], key=key)
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    if value > 1:
        raise ConfigError(f"{key} must be less than or equal to 1")
    return value


def _positive_float(mapping: dict[str, Any], key: str, *, default: float | None = None) -> float:
    if key not in mapping:
        if default is None:
            raise ConfigError(f"missing risk limit: {key}")
        return _finite_config_float(default, key=key)
    value = _finite_config_float(mapping[key], key=key)
    if value <= 0:
        raise ConfigError(f"{key} must be greater than 0")
    return value


def _non_negative_float(mapping: dict[str, Any], key: str, *, default: float) -> float:
    value = _finite_config_float(mapping.get(key, default), key=key)
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    return value


def _positive_int(mapping: dict[str, Any], key: str, *, default: int) -> int:
    value = _strict_config_int(mapping.get(key, default), key=key)
    if value < 1:
        raise ConfigError(f"{key} must be >= 1")
    return value


def _non_negative_int(mapping: dict[str, Any], key: str, *, default: int) -> int:
    value = _strict_config_int(mapping.get(key, default), key=key)
    if value < 0:
        raise ConfigError(f"{key} must be non-negative")
    return value


def _finite_config_float(value: object, *, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{key} must be finite")
    return result


def _strict_config_int(value: object, *, key: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{key} must be an integer")
    return value


def _strict_bool(
    mapping: dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = mapping.get(key, default)
    if type(value) is not bool:
        raise ConfigError(f"{key} must be a boolean")
    return value


def _required_config_text(value: object, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


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
