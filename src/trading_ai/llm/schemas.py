"""Structured Output schemas used with the OpenAI Responses API."""

from __future__ import annotations

from typing import Any


SCHEMAS: dict[str, dict[str, Any]] = {
    "BacktestSummary": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "key_metrics": {"type": "object", "additionalProperties": {"type": ["number", "string"]}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "requires_human_review": {"type": "boolean"},
        },
        "required": ["summary", "key_metrics", "risks", "requires_human_review"],
        "additionalProperties": False,
    },
    "ResearchHypothesis": {
        "type": "object",
        "properties": {
            "hypothesis": {"type": "string"},
            "data_required": {"type": "array", "items": {"type": "string"}},
            "test_proposal": {"type": "string"},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
            "risk_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["hypothesis", "data_required", "test_proposal", "success_criteria", "risk_notes"],
        "additionalProperties": False,
    },
    "RiskReview": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pass", "review", "fail"]},
            "limit_breaches": {"type": "array", "items": {"type": "string"}},
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
            "human_review_required": {"type": "boolean"},
        },
        "required": ["status", "limit_breaches", "recommended_actions", "human_review_required"],
        "additionalProperties": False,
    },
    "EventExtraction": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp": {"type": "string"},
                        "symbol": {"type": "string"},
                        "event_type": {"type": "string"},
                        "source": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["timestamp", "symbol", "event_type", "source", "summary"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["events"],
        "additionalProperties": False,
    },
    "TradeExplanation": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "decision_inputs": {"type": "array", "items": {"type": "string"}},
            "deterministic_reason": {"type": "string"},
            "risk_gate_status": {"type": "string"},
            "llm_authority": {"type": "string", "enum": ["none"]},
        },
        "required": ["symbol", "decision_inputs", "deterministic_reason", "risk_gate_status", "llm_authority"],
        "additionalProperties": False,
    },
    "PaperOpsReview": {
        "type": "object",
        "properties": {
            "operational_status": {"type": "string", "enum": ["OK", "WARN", "BLOCKED", "ERROR", "UNKNOWN"]},
            "risks": {"type": "array", "items": {"type": "string"}},
            "blockers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "severity": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["code", "severity", "message"],
                    "additionalProperties": False,
                },
            },
            "recommendation": {
                "type": "string",
                "enum": ["CONTINUE_OFFLINE", "DEFER_MODEL", "READY_FOR_PAPER_CONFIRMATION", "BLOCK"],
            },
            "reasoning": {"type": "string"},
            "human_review_required": {"type": "boolean"},
            "llm_authority": {"type": "string", "enum": ["none"]},
        },
        "required": [
            "operational_status",
            "risks",
            "blockers",
            "recommendation",
            "reasoning",
            "human_review_required",
            "llm_authority",
        ],
        "additionalProperties": False,
    },
    "LLMSignalProposal": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "action": {"type": "string", "enum": ["buy", "hold"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "thesis": {"type": "string"},
            "risk_notes": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "llm_authority": {"type": "string", "enum": ["none"]},
        },
        "required": [
            "symbol",
            "action",
            "confidence",
            "thesis",
            "risk_notes",
            "evidence_refs",
            "llm_authority",
        ],
        "additionalProperties": False,
    },
}


def schema_for(name: str) -> dict[str, Any]:
    if name not in SCHEMAS:
        raise KeyError(f"unknown structured output schema: {name}")
    return SCHEMAS[name]


def validate_against_schema(name: str, payload: dict[str, Any]) -> None:
    schema = schema_for(name)
    missing = [key for key in schema["required"] if key not in payload]
    if missing:
        raise ValueError(f"{name} missing required keys: {', '.join(missing)}")
    extra = set(payload) - set(schema["properties"])
    if extra and schema.get("additionalProperties") is False:
        raise ValueError(f"{name} contains unexpected keys: {', '.join(sorted(extra))}")
    _validate_object(name, payload, schema)


def _validate_object(path: str, payload: dict[str, Any], schema: dict[str, Any]) -> None:
    properties = schema.get("properties", {})
    for key, value in payload.items():
        field_schema = properties.get(key)
        if isinstance(field_schema, dict):
            _validate_value(f"{path}.{key}", value, field_schema)


def _validate_value(path: str, value: Any, schema: dict[str, Any]) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        raise ValueError(f"{path} must be {_type_label(expected_type)}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"{path} must be one of: {', '.join(str(item) for item in enum)}")
    if expected_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} must be <= {maximum}")
    if expected_type == "array":
        items_schema = schema.get("items")
        if isinstance(value, list) and isinstance(items_schema, dict):
            for index, item in enumerate(value):
                _validate_value(f"{path}[{index}]", item, items_schema)
    if expected_type == "object" and isinstance(value, dict):
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"{path} missing required keys: {', '.join(missing)}")
        extra = set(value) - set(schema.get("properties", {}))
        if extra and schema.get("additionalProperties") is False:
            raise ValueError(f"{path} contains unexpected keys: {', '.join(sorted(extra))}")
        _validate_object(path, value, schema)


def _matches_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_type(value, item) for item in expected_type)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


def _type_label(expected_type: Any) -> str:
    if isinstance(expected_type, list):
        return " or ".join(str(item) for item in expected_type)
    return str(expected_type)
