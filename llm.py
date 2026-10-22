"""
LLM client settings shared by the synthesizer, contextualizer, and agents.

All LLM calls go through an OpenAI-compatible API (Groq by default), configured
via environment variables:
- LLM_BASE_URL:          API base URL (default: Groq)
- LLM_API_KEY:           API key (falls back to GROQ_API_KEY)
- LLM_MODEL:             model name (default: openai/gpt-oss-20b)
- LLM_REASONING_EFFORT:  "low" / "medium" / "high", or "none" to never send it.
                         If unset, "low" is sent only to reasoning models —
                         most other models reject the parameter.
"""
from __future__ import annotations

import os
import re

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"

# Model families that accept `reasoning_effort`
_REASONING_MODEL_PATTERN = re.compile(r"gpt-oss|(^|/)(o\d|gpt-5)", re.I)


def llm_api_key() -> str:
    return os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", "")


def llm_model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def get_llm_client():
    """Return an OpenAI-compatible client for the configured provider.

    With no key configured, a placeholder is used so the engine can still be
    constructed for indexing and search (which need no LLM); only LLM calls
    then fail, and callers report that as an error string.
    """
    from openai import OpenAI
    return OpenAI(base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
                  api_key=llm_api_key() or "not-configured")


def llm_extra_kwargs(model: str) -> dict:
    """Extra chat.completions.create() kwargs supported by `model`."""
    effort = os.getenv("LLM_REASONING_EFFORT")
    if effort is None:
        effort = "low" if _REASONING_MODEL_PATTERN.search(model) else ""
    if not effort or effort.lower() == "none":
        return {}
    return {"reasoning_effort": effort}
