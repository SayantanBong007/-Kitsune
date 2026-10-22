"""
Context budget manager — prevents LLM context window overflow.

On a large codebase, a "hub" function (like `main()` or `handle_request`)
can have 50+ callers and 20+ callees. Without budget management, the
hybrid search would pull in ALL of their source code — easily 100,000+
characters — blowing the LLM's context window.

This module enforces a character budget, dropping whole chunks (never
cutting mid-function) when the budget is exceeded. It prioritizes:
1. Primary chunks (semantic matches) — highest priority
2. Direct dependencies (depth=1) — medium priority
3. Transitive dependencies (depth=2+) — lowest priority
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default budget: ~5000 chars ≈ ~1250 tokens (safe for Groq free tier)
DEFAULT_BUDGET = 5000


@dataclass
class BudgetResult:
    """Result of applying a context budget."""
    primary_chunks: list[dict] = field(default_factory=list)
    dependency_chunks: list[dict] = field(default_factory=list)
    keyword_chunks: list[dict] = field(default_factory=list)
    total_chars: int = 0
    omitted: int = 0
    budget: int = 0

    @property
    def all_chunks(self) -> list[dict]:
        return self.primary_chunks + self.dependency_chunks + self.keyword_chunks


def apply_context_budget(
    primary_chunks: list[dict],
    dependency_chunks: list[dict],
    keyword_chunks: list[dict] | None = None,
    max_chars: int = DEFAULT_BUDGET,
) -> BudgetResult:
    """Apply a character budget to retrieved chunks.

    Drops whole chunks (never cuts mid-function) when the budget is exceeded.
    Priority: primary > direct deps > keyword > transitive deps.

    Parameters
    ----------
    primary_chunks:
        Semantic search results (highest priority).
    dependency_chunks:
        Graph-traversed dependencies (medium priority).
    keyword_chunks:
        BM25 keyword matches (lower priority).
    max_chars:
        Maximum total characters across all included chunks.

    Returns
    -------
    BudgetResult with only the chunks that fit within the budget.
    """
    keyword_chunks = keyword_chunks or []
    result = BudgetResult(budget=max_chars)
    used = 0

    def _try_add(chunk: dict, target: list) -> bool:
        nonlocal used
        chunk_len = len(chunk.get("text", ""))
        if chunk_len == 0:
            return False
        # Always include the first primary chunk even if it alone exceeds budget
        if used > 0 and used + chunk_len > max_chars:
            result.omitted += 1
            return False
        target.append(chunk)
        used += chunk_len
        return True

    # Priority 1: Primary chunks (semantic matches)
    for chunk in primary_chunks:
        _try_add(chunk, result.primary_chunks)

    # Priority 2: Keyword chunks (exact matches, usually small)
    for chunk in keyword_chunks:
        _try_add(chunk, result.keyword_chunks)

    # Priority 3: Dependency chunks (graph-traversed)
    for chunk in dependency_chunks:
        _try_add(chunk, result.dependency_chunks)

    result.total_chars = used

    if result.omitted > 0:
        logger.warning(
            f"Context budget: included {len(result.all_chunks)} chunks "
            f"({used} chars), omitted {result.omitted} chunks to stay "
            f"within {max_chars} char budget."
        )

    return result
