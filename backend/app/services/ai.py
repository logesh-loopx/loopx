"""Claude access for the agents and the console's AI features.

Without ANTHROPIC_API_KEY every caller gets `None` and falls back to rules,
so LOOPX keeps working (just without AI-written text).
"""
from __future__ import annotations

import json
import logging
import re

from ..config import settings

log = logging.getLogger(__name__)
_client = None


def client():
    global _client
    if _client is None and settings.anthropic_api_key:
        import anthropic

        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60)
    return _client


def available() -> bool:
    return client() is not None


def _model(tier: str) -> str:
    return settings.fast_model if tier == "quick" else settings.smart_model


def text(prompt: str | list, *, tier: str = "default", system: str = "", max_tokens: int = 1200) -> str | None:
    c = client()
    if not c:
        return None
    messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
    try:
        kw = dict(model=_model(tier), max_tokens=max_tokens, messages=messages)
        if system:
            kw["system"] = system
        r = c.messages.create(**kw)
        return "".join(b.text for b in r.content if b.type == "text").strip()
    except Exception as e:  # never let an AI hiccup break an agent run
        log.warning("Claude call failed: %s", e)
        return None


def structured(prompt: str, schema: dict, *, tier: str = "quick", system: str = "", name: str = "submit",
               max_tokens: int = 800) -> dict | None:
    """Force a JSON answer that matches `schema` (via a forced tool call)."""
    c = client()
    if not c:
        return None
    try:
        kw = dict(model=_model(tier), max_tokens=max_tokens,
                  tools=[{"name": name, "description": "Submit the result.", "input_schema": schema}],
                  tool_choice={"type": "tool", "name": name},
                  messages=[{"role": "user", "content": prompt}])
        if system:
            kw["system"] = system
        r = c.messages.create(**kw)
        for b in r.content:
            if b.type == "tool_use":
                return dict(b.input)
    except Exception as e:
        log.warning("Claude structured call failed: %s", e)
    return None


def loose_json(s: str | None):
    """Pull the first JSON object out of a model answer."""
    if not s:
        return None
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
