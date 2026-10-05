"""Thin wrapper around the Anthropic API.

Returns None / raises LLMUnavailable when no key is configured so callers can fall back to
deterministic rules. The API key is only ever read server-side from the environment.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


_client = None


def get_client():
    global _client
    if not settings.use_llm:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set (or LLM_ENABLED=false)")
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
    return _client


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost JSON array/object in the reply.
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            s, e = text.find(open_ch), text.rfind(close_ch)
            if s != -1 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except json.JSONDecodeError:
                    continue
        raise


def complete_json(model: str, system: str, user: str, max_tokens: int = 2048) -> Any:
    client = get_client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    return _extract_json(text)
