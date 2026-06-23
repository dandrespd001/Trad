"""Shared helpers for paper-trading artifacts and status handling."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

PAPER_OK = "OK"
PAPER_WARN = "WARN"
PAPER_CRITICAL = "CRITICAL"
PAPER_BLOCKED = "BLOCKED"
PAPER_ERROR = "ERROR"

ALPACA_PAPER_API_KEY_ENV = "ALPACA_PAPER_API_KEY"
ALPACA_PAPER_SECRET_KEY_ENV = "ALPACA_PAPER_SECRET_KEY"  # noqa: S105
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"  # noqa: S105

_EXIT_CODES = {
    PAPER_OK: 0,
    PAPER_WARN: 0,
    PAPER_CRITICAL: 1,
    PAPER_BLOCKED: 1,
    PAPER_ERROR: 2,
}
_SECRET_KEYS = (
    ALPACA_PAPER_API_KEY_ENV,
    ALPACA_PAPER_SECRET_KEY_ENV,
    TELEGRAM_BOT_TOKEN_ENV,
    "OPENAI_API_KEY",
    "PAPER_MODEL_ALIAS_SIGNING_KEY",
)


def write_json_artifact(payload: Mapping[str, object], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")


def read_json_artifact(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_text_artifact(payload: str, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")


def read_text_artifact(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def as_of_date_to_iso(value: str | date) -> str:
    """Return a strict ``YYYY-MM-DD`` date string."""
    parsed = as_of_date_to_date(value)
    return parsed.isoformat()


def as_of_date_to_date(value: str | date) -> date:
    """Resolve a paper date input to a strict ``datetime.date`` object."""
    if isinstance(value, date):
        return value

    candidate = str(value).strip()
    if candidate == "today":
        from datetime import date as _date

        return _date.today()

    parsed = date.fromisoformat(candidate)
    if parsed.isoformat() != candidate:
        raise ValueError("as_of_date must be an ISO date in YYYY-MM-DD format")
    return parsed


def reason_codes(value: object) -> list[str]:
    """Normalize blocker/reason collections to a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def paper_exit_code(status: str) -> int:
    return _EXIT_CODES.get(str(status).upper(), 2)


def redact_secrets(text: object, *, env: Mapping[str, str] | None = None) -> str:
    redacted = str(text)
    values = os.environ if env is None else env
    for key in _SECRET_KEYS:
        try:
            secret = values.get(key, "")
        except Exception:
            secret = ""
        if secret:
            redacted = redacted.replace(secret, f"[redacted-{key.lower()}]")
    redacted = re.sub(r"bot[^/\s]+/sendMessage", "bot[redacted]/sendMessage", redacted)
    redacted = re.sub(r"(api[_-]?key|secret(?:[_-]?key)?|token)=([^,\s]+)", r"\1=[redacted]", redacted, flags=re.I)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._-]{20,}", "Bearer [redacted-bearer-token]", redacted)
    redacted = re.sub(r"\bsk-(?:proj|live|test)?-[A-Za-z0-9_-]+", "[redacted-api-key]", redacted)
    redacted = re.sub(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,255}\b", "[redacted-github-token]", redacted)
    redacted = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b", "[redacted-github-token]", redacted)
    redacted = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[redacted-aws-access-key]", redacted)
    redacted = re.sub(r"\b(?:xoxb|xoxa|xoxp|xoxr)-[0-9]+-[0-9]+-[A-Za-z0-9_-]+", "[redacted-slack-token]", redacted)
    redacted = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted-jwt]", redacted)
    return redacted
