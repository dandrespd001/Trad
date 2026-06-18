"""Shared helpers for paper-trading artifacts and status handling."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping


PAPER_OK = "OK"
PAPER_WARN = "WARN"
PAPER_CRITICAL = "CRITICAL"
PAPER_BLOCKED = "BLOCKED"
PAPER_ERROR = "ERROR"

ALPACA_PAPER_API_KEY_ENV = "ALPACA_PAPER_API_KEY"
ALPACA_PAPER_SECRET_KEY_ENV = "ALPACA_PAPER_SECRET_KEY"
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

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
    redacted = re.sub(r"(api[_-]?key|secret[_-]?key|token)=([^,\s]+)", r"\1=[redacted]", redacted, flags=re.I)
    redacted = re.sub(r"Bearer\s+sk-[A-Za-z0-9_-]+", "Bearer [redacted-api-key]", redacted)
    redacted = re.sub(r"\bsk-(?:proj|live|test)?-[A-Za-z0-9_-]+", "[redacted-api-key]", redacted)
    return redacted
