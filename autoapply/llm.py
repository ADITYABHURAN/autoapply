"""Thin wrapper around the Anthropic API. Swap the body of `complete` to use another provider."""
from __future__ import annotations

import json
import re

from .config import settings


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(settings.anthropic_api_key)


def complete(system: str, user: str, max_tokens: int = 4000) -> str:
    if not available():
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def extract_json(text: str):
    """Parse the first JSON object or array in a model reply (handles ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("no JSON in model reply")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def complete_json(system: str, user: str, max_tokens: int = 4000):
    return extract_json(complete(system, user, max_tokens))
