"""Shared paper-only safety and authority aggregation."""

from __future__ import annotations

from collections.abc import Mapping

SAFETY_FLAGS = (
    "broker_client_built",
    "credentials_read",
    "orders_submitted",
    "live_trading_authorized",
    "live_trading_allowed",
)


def aggregate_safety(
    *payloads: Mapping[str, object] | None,
    component_safety: Mapping[str, object] | None = None,
) -> dict[str, object]:
    component = _base_safety()
    if component_safety is not None:
        for name in SAFETY_FLAGS:
            component[name] = bool(component_safety.get(name))
    observed = _base_safety()
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        safety = _mapping(payload.get("safety"))
        for name in SAFETY_FLAGS:
            observed[name] = bool(observed[name] or safety.get(name))
    aggregate = {
        "paper_only": True,
        **{name: bool(component[name] or observed[name]) for name in SAFETY_FLAGS},
    }
    aggregate["component_safety"] = {"paper_only": True, **component}
    aggregate["observed_child_safety"] = {"paper_only": True, **observed}
    return aggregate


def aggregate_authority(
    *,
    orders_submitted: bool = False,
    risk_changed: bool = False,
    broker_client_built: bool = False,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "llm_authority": "none",
        "orders_submitted": bool(orders_submitted),
        "observed_child_orders_submitted": bool(orders_submitted),
        "broker_client_built": bool(broker_client_built),
        "risk_changed": bool(risk_changed),
        "live_trading_authorized": False,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _base_safety() -> dict[str, bool]:
    return {name: False for name in SAFETY_FLAGS}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
