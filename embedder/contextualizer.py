"""
Contextualizer — prepends a context note to each chunk before embedding.

This is the Anthropic Contextual Retrieval technique. Before embedding a
bare code chunk, we prepend a context note that tells the embedding model
WHERE the chunk lives and WHAT it does.

Features:
- Synchronous LLM mode: the LLM contextualizer is callable from
  the sync indexing pipeline.
- Richer template notes: includes scope, calls, inherits, decorators.
- Batch LLM contextualization for efficiency.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from kitsune.models.chunk import Chunk

logger = logging.getLogger(__name__)

ContextMode = Literal["template", "llm"]


def build_context_note(chunk: Chunk) -> str:
    """Build a context note from chunk metadata — no LLM needed.

    This is the default/fallback. Even without an LLM it's far better than
    bare embedding because we attach:
    - the file path (where does this live in the project?)
    - the chunk type and qualified name (is it a function? a method?)
    - the docstring (what does it do, in the author's own words?)
    - the scope (what class does this method belong to?)
    - what it calls and inherits (structural context)
    """
    parts = [f"CONTEXT: This is a {chunk.chunk_type}"]

    if chunk.name and chunk.name != "<module>":
        parts.append(f"named '{chunk.qualified_name}'")

    parts.append(f"in {chunk.file_path}")
    parts.append(f"(lines {chunk.start_line}-{chunk.end_line}).")

    if chunk.docstring:
        doc = chunk.docstring[:200].replace("\n", " ")
        parts.append(f"Description: {doc}")

    if chunk.inherits:
        parts.append(f"Inherits from: {', '.join(chunk.inherits)}.")

    if chunk.calls:
        # Show up to 5 calls to keep the note concise
        calls_str = ", ".join(sorted(chunk.calls)[:5])
        parts.append(f"Calls: {calls_str}.")

    if chunk.decorators:
        parts.append(f"Decorators: {', '.join(chunk.decorators)}.")

    return " ".join(parts)


def build_llm_context_note(chunk: Chunk, repo_summary: str = "") -> str:
    """Use an LLM to write a richer context note for the chunk.

    This is the LLM-powered version. It reads the actual code and produces
    a description like:
        "This function validates a payment amount and triggers a charge
         via the Stripe API. It belongs to the billing module."

    Falls back to the template version if no API key is configured or if
    the LLM call fails — so the pipeline always completes.

    NOTE: This is synchronous and usable from the indexing pipeline.
    """
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return build_context_note(chunk)

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        )

        repo_ctx = f"Repository summary: {repo_summary}\n\n" if repo_summary else ""
        prompt = (
            f"{repo_ctx}"
            f"Here is a {chunk.chunk_type} from {chunk.file_path}:\n\n"
            f"```{chunk.language}\n{chunk.text[:1500]}\n```\n\n"
            "Write ONE sentence (max 40 words) explaining what this code does "
            "and what larger feature or system it belongs to. "
            "Start with 'CONTEXT:'. Be specific. Do not repeat the code."
        )

        response = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "openai/gpt-oss-20b"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.0,
            reasoning_effort="low",
        )
        note = (response.choices[0].message.content or "").strip()
        if note:
            return note
        return build_context_note(chunk)

    except Exception as exc:
        logger.warning(f"LLM contextualizer failed for '{chunk.name}': {exc} — using template")
        return build_context_note(chunk)


def contextualize_chunk(chunk: Chunk, mode: ContextMode = "template",
                         repo_summary: str = "") -> str:
    """Return the full contextualized text for embedding.

    Format:
        [context note]

        [original chunk code]

    This is the string that gets embedded — not the raw chunk text.

    Parameters
    ----------
    chunk:
        The Chunk object to contextualize.
    mode:
        "template" (default, no API call) or "llm" (richer, uses Groq).
    repo_summary:
        Optional short description of the overall repository, injected
        into the LLM prompt so it can relate the chunk to the bigger picture.
    """
    if mode == "llm":
        note = build_llm_context_note(chunk, repo_summary)
    else:
        note = build_context_note(chunk)
    return f"{note}\n\n{chunk.text}"
