"""LLM access, provider-agnostic.

Supports Anthropic (ANTHROPIC_API_KEY) and Gemini (GEMINI_API_KEY — free tier, no card,
from https://aistudio.google.com/apikey). Pick one by setting its key; if both are set,
AUTOAPPLY_LLM_PROVIDER decides, else Anthropic wins.
"""
from __future__ import annotations

import json
import re

from .config import settings

DEFAULT_MODEL = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-3.5-flash-lite"}


class LLMUnavailable(RuntimeError):
    pass


def provider() -> str:
    if settings.llm_provider:
        return settings.llm_provider
    if settings.anthropic_api_key:
        return "anthropic"
    if settings.gemini_api_key:
        return "gemini"
    return "anthropic"  # no key either way; error message below names this one


def model() -> str:
    return settings.model or DEFAULT_MODEL.get(provider(), "claude-sonnet-5-5")


def available() -> bool:
    return bool(settings.anthropic_api_key if provider() == "anthropic" else settings.gemini_api_key)


def _complete_anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=model(), max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    return "".join(block.text for block in msg.content if block.type == "text")


def _complete_gemini(system: str, user: str, max_tokens: int) -> str:
    import time

    import httpx

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model()}:generateContent"
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}}
    # Free tier: per-minute limits (429) and occasional overload (503) are normal; wait and retry.
    for attempt in range(4):
        r = httpx.post(url, headers={"x-goog-api-key": settings.gemini_api_key}, json=body, timeout=90)
        if r.status_code not in (429, 503) or attempt == 3:
            break
        time.sleep(15 * (attempt + 1))
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            detail = r.text[:200]
        hint = " (free-tier quota used up for now; it resets daily)" if r.status_code == 429 else ""
        raise LLMUnavailable(f"Gemini error {r.status_code}{hint}: {detail}")
    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason", "no candidates returned")
        raise LLMUnavailable(f"Gemini returned no output ({reason})")
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def complete(system: str, user: str, max_tokens: int = 4000) -> str:
    if not available():
        raise LLMUnavailable(
            "No LLM key set. Put ANTHROPIC_API_KEY or GEMINI_API_KEY in your .env "
            "(Gemini has a free tier: https://aistudio.google.com/apikey)")
    return _complete_anthropic(system, user, max_tokens) if provider() == "anthropic" else _complete_gemini(system, user, max_tokens)


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
