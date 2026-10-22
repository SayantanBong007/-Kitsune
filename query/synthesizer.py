"""
Synthesizer — formats retrieved context and generates LLM answers.

Features:
- Formats keyword chunks alongside primary and dependency chunks.
- Includes graph traversal metadata (depth, paths).
- Supports both streaming and non-streaming LLM calls.
- Richer context formatting with concept annotations.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

from kitsune.query.hybrid_search import SearchResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Kitsune, an expert AI software architect.
You are answering a question about a codebase.

You have been provided with three types of context:
1. PRIMARY CHUNKS: The core code blocks most relevant to the question (semantic match).
2. GRAPH DEPENDENCIES: Code blocks that the primary chunks call/depend on (structural match).
3. KEYWORD MATCHES: Code blocks containing exact keyword matches from the question.

RULES:
1. Answer the question comprehensively using ONLY the provided code context.
2. If you don't know the answer based on the context, say so. Do not guess.
3. Cite the functions/classes you are referencing (use qualified names).
4. Keep your answer clear, structured, and developer-friendly.
5. Use markdown formatting for code references.
"""


class Synthesizer:
    """Takes retrieved context and generates an LLM response."""

    def __init__(self):
        base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
        api_key = os.getenv("LLM_API_KEY", os.getenv("GROQ_API_KEY", ""))
        self.model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")

        if not api_key:
            logger.warning("No LLM_API_KEY or GROQ_API_KEY found! LLM queries will fail.")

        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def format_context(self, search_results: SearchResult) -> str:
        """Format the search results into a readable context string for the LLM.

        This is used by the RepositoryKnowledgeEngine to produce a context
        string that gets injected into agent prompts.
        """
        context = ""

        # Primary chunks (semantic entry points)
        primary = search_results.get("primary_chunks", [])
        if primary:
            context += "--- PRIMARY CHUNKS (semantic match) ---\n"
            for chunk in primary:
                name = chunk.get("qualified_name") or chunk.get("name", "Unknown")
                file_path = chunk.get("file_path", "Unknown")
                score = chunk.get("rerank_score") or chunk.get("score", 0)
                lang = chunk.get("language", "python")
                context += f"\nFile: {file_path} | Symbol: {name} | Score: {score}\n"
                context += f"```{lang}\n{chunk.get('text', '')}\n```\n"

        # Graph dependencies
        deps = search_results.get("dependency_chunks", [])
        if deps:
            context += "\n--- GRAPH DEPENDENCIES (called by primary chunks) ---\n"
            for chunk in deps:
                name = chunk.get("qualified_name") or chunk.get("name", "Unknown")
                file_path = chunk.get("file_path", "Unknown")
                lang = chunk.get("language", "python")
                context += f"\nFile: {file_path} | Symbol: {name}\n"
                context += f"```{lang}\n{chunk.get('text', '')}\n```\n"

        # Keyword matches
        keywords = search_results.get("keyword_chunks", [])
        if keywords:
            context += "\n--- KEYWORD MATCHES (exact text match) ---\n"
            for chunk in keywords:
                name = chunk.get("qualified_name") or chunk.get("name", "Unknown")
                file_path = chunk.get("file_path", "Unknown")
                lang = chunk.get("language", "python")
                context += f"\nFile: {file_path} | Symbol: {name}\n"
                context += f"```{lang}\n{chunk.get('text', '')}\n```\n"

        # Graph metadata
        graph_paths = search_results.get("graph_paths", {})
        if graph_paths:
            context += f"\n--- GRAPH TRAVERSAL INFO ---\n{graph_paths}\n"

        return context

    def generate_answer(self, question: str, search_results: SearchResult) -> Iterator[str]:
        """Send prompt to LLM and yield streaming response chunks.

        Parameters
        ----------
        question:
            The user's question.
        search_results:
            The hybrid search results.

        Yields
        ------
        Response text chunks as they arrive from the LLM.
        """
        context_str = self.format_context(search_results)
        user_message = f"CONTEXT:\n{context_str}\n\nQUESTION:\n{question}"

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                stream=True,
                temperature=0.1,
                reasoning_effort="low",
            )

            for chunk in response:
                if chunk.choices[0].delta.content is not None:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            yield f"\n\n[LLM Error: {e}]"

    def generate_answer_sync(self, question: str, search_results: SearchResult) -> str:
        """Non-streaming version of generate_answer.

        Returns the full response as a single string.
        """
        return "".join(self.generate_answer(question, search_results))
