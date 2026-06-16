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
