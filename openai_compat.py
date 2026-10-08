"""Shared OpenAI model-family compatibility rules for request builders."""

from __future__ import annotations

from typing import Optional


def model_uses_reasoning(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith(("gpt-5", "gpt-6", "o"))


def model_prefers_responses(model: str) -> bool:
    """Use Responses by default for current reasoning-model families."""
    return model_uses_reasoning(model)


def default_reasoning_effort(model: str) -> Optional[str]:
    name = (model or "").strip().lower()
    if name.startswith("gpt-5"):
        return "minimal"
    if name.startswith("gpt-6"):
        return "low"
    return None


def supports_temperature(model: str, reasoning_effort: str) -> bool:
    """Whether temperature is valid for this model and requested effort."""
    name = (model or "").strip().lower()
    effort = (reasoning_effort or "").strip().lower()

    if name.startswith("gpt-6"):
        # GPT-6 rejects sampling parameters when reasoning is enabled. An empty
        # effort uses the model default, so only an explicit "none" is safe.
        return effort == "none"
    if name.startswith("gpt-5.2"):
        return effort in ("", "none")
    if name.startswith("gpt-5"):
        return False
    return True


def chat_completion_token_limit_param(model: str) -> str:
    """Return the token-limit field accepted by this Chat Completions model."""
    if model_uses_reasoning(model):
        return "max_completion_tokens"
    return "max_tokens"
