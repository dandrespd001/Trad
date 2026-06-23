"""OpenAI model resolution policy for paper-only LLM workflows."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_OPENAI_MODEL = "gpt-5.5"
ENV_OPENAI_MODEL = "TRADING_AI_OPENAI_MODEL"


def resolve_openai_model(cli_model: str | None = None, *, env: Mapping[str, str] | None = None) -> dict[str, object]:
    environment = os.environ if env is None else env
    if cli_model is not None and str(cli_model).strip():
        model = str(cli_model).strip()
        source = "cli"
    elif str(environment.get(ENV_OPENAI_MODEL) or "").strip():
        model = str(environment.get(ENV_OPENAI_MODEL) or "").strip()
        source = "env"
    else:
        model = DEFAULT_OPENAI_MODEL
        source = "default"
    if any(character.isspace() for character in model):
        return {"status": "BLOCKED", "model": model, "source": source, "reason": "invalid_model_slug"}
    return {"status": "OK", "model": model, "source": source, "reason": "resolved"}
