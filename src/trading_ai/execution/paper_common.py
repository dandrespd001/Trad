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
    "NVIDIA_API_KEY",
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
    redacted = re.sub(r"\bnvapi-[A-Za-z0-9_-]+", "[redacted-nvidia-api-key]", redacted)
    redacted = re.sub(r"\bsk-(?:proj|live|test)?-[A-Za-z0-9_-]+", "[redacted-api-key]", redacted)
    redacted = re.sub(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,255}\b", "[redacted-github-token]", redacted)
    redacted = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b", "[redacted-github-token]", redacted)
    redacted = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[redacted-aws-access-key]", redacted)
    redacted = re.sub(r"\b(?:xoxb|xoxa|xoxp|xoxr)-[0-9]+-[0-9]+-[A-Za-z0-9_-]+", "[redacted-slack-token]", redacted)
    redacted = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted-jwt]", redacted)
    return redacted


def redact_payload(value: object, *, env: Mapping[str, str] | None = None) -> object:
    """Centralize recursive payload redaction so modules don't keep divergent copies.

    Walks ``value`` recursively:
    - ``Mapping``: dict with stringified keys passed through ``redact_secrets``
      and values redacted recursively.
    - ``list``: list with elements redacted recursively.
    - ``tuple``: converted to ``list`` (JSON has no tuples) with elements
      redacted recursively.
    - ``str``: passed through ``redact_secrets`` with ``env``.
    - any other scalar (``int``, ``float``, ``bool``, ``None``): returned intact.

    Returns ``object`` so callers can redact any payload shape. Modules that
    need a dict-root guarantee must enforce it at the call site.
    """
    if isinstance(value, Mapping):
        return {redact_secrets(str(key), env=env): redact_payload(item, env=env) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, env=env) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item, env=env) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, env=env)
    return value


def redact_payload_json(payload: Mapping[str, object], *, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Variant of :func:`redact_payload` that normalizes the result through JSON.

    Runs the same recursive redaction walk as :func:`redact_payload` and then
    applies a ``json.loads(json.dumps(...))`` round-trip with default kwargs.
    That round-trip introduces observable differences from the plain walk:

    - ``tuple`` nodes collapse to ``list`` via serialization (JSON has no
      tuples); the walk in :func:`redact_payload` also returns ``list`` for
      tuples, but here the conversion happens through JSON rather than the
      Python ``isinstance`` branch.
    - Non-JSON-serializable scalars (``datetime``, ``Path``, custom objects)
      propagate the default ``json.dumps`` ``TypeError`` because no
      ``default=`` callable is supplied and ``sort_keys`` is not requested.
    - ``dict`` keys preserve insertion order; ``sort_keys`` is NOT applied, so
      ordering matches the input mapping rather than alphabetical.
    - The root is always a ``dict`` because the round-trip is performed on
      the value returned by the walk, which for a ``Mapping`` input is a dict.

    Use this helper when downstream consumers expect JSON-shaped payloads
    (e.g. artifact writers, status JSON dumps). Use :func:`redact_payload`
    when the call site only needs secret redaction without the JSON
    normalization.
    """
    redacted = redact_payload(payload, env=env)
    return json.loads(json.dumps(redacted))
