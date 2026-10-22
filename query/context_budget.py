"""
Context budget manager — prevents LLM context window overflow.

On a large codebase, a "hub" function (like `main()` or `handle_request`)
can have 50+ callers and 20+ callees. Without budget management, the
hybrid search would pull in ALL of their source code — easily 100,000+
characters — blowing the LLM's context window.

This module enforces a character budget. Chunks that don't fit are dropped,
and oversized ones are condensed (signature + most relevant lines + a pointer
to the full code) instead of crowding out everything else. It prioritizes:
1. Primary chunks (semantic matches) — highest priority
2. Direct dependencies (depth=1) — medium priority
3. Transitive dependencies (depth=2+) — lowest priority
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default budget: ~10000 chars ≈ ~2500 tokens. Chosen with eval/run_eval.py:
# retrieval recall 0.925 at 5000, 0.950 at 8000, 0.975 at 10000.
DEFAULT_BUDGET = 10000

# No single chunk may take more than this share of the budget (it's condensed)
MAX_CHUNK_SHARE = 0.4
# Smallest useful condensed chunk; less room than this and the chunk is omitted
MIN_CONDENSED_CHARS = 600

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_QUERY_STOPWORDS = {"how", "does", "the", "what", "are", "and", "for", "with", "this",
                    "that", "from", "into", "when", "where", "which", "who", "why", "work"}


def _terms(text: str) -> set[str]:
    """Lowercase word parts (snake_case and camelCase split) for matching."""
    terms = set()
    for word in _WORD.findall(text):
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", word) + [word]:
            part = part.lower()
            if len(part) >= 3 and part not in _QUERY_STOPWORDS:
                terms.add(part)
    return terms


def condense_chunk(chunk: dict, query: str, limit: int) -> dict:
    """Shrink a chunk to about `limit` chars: head + most query-relevant lines.

    Keeps the signature/docstring (head), then the contiguous window of lines
    sharing the most terms with the query, and notes what was left out and
    where the full code lives.
    """
    lines = chunk.get("text", "").splitlines()
    location = f"{chunk.get('file_path', '?')}:{chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}"
    footer = f"... [condensed — full code: {location}]"
    budget = max(limit - len(footer) - 80, 200)

    head: list[str] = []
    for line in lines[:12]:
        if sum(len(h) + 1 for h in head) + len(line) + 1 > budget * 0.35:
            break
        head.append(line)
    rest = lines[len(head):]
    room = budget - sum(len(h) + 1 for h in head)

    q_terms = _terms(query)
    scores = [len(_terms(line) & q_terms) for line in rest]
    best_start, best_end, best_score = 0, 0, -1
    start, size, score = 0, 0, 0
    for end, line in enumerate(rest):  # sliding window that fits in `room`
        size += len(line) + 1
        score += scores[end]
        while size > room and start <= end:
            size -= len(rest[start]) + 1
            score -= scores[start]
            start += 1
        if score > best_score:
            best_start, best_end, best_score = start, end + 1, score

    out = list(head)
    if best_start > 0:
        out.append(f"... [{best_start} lines omitted]")
    out.extend(rest[best_start:best_end])
    if best_end < len(rest):
        out.append(f"... [{len(rest) - best_end} lines omitted]")
    out.append(footer)
    condensed = dict(chunk)
    condensed["text"] = "\n".join(out)
    condensed["condensed"] = True
    return condensed


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
    query: str = "",
) -> BudgetResult:
    """Apply a character budget to retrieved chunks.

    Chunks are added by priority: primary > keyword > dependencies (in the
    order given). A chunk bigger than MAX_CHUNK_SHARE of the budget, or one
    that no longer fits whole, is *condensed* — signature/docstring plus the
    lines most relevant to `query`, with a pointer to the full code — rather
    than letting one huge function crowd out everything else.

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
    query:
        The question, used to pick which lines of a condensed chunk to keep.

    Returns
    -------
    BudgetResult with only the chunks that fit within the budget.
    """
    keyword_chunks = keyword_chunks or []
    result = BudgetResult(budget=max_chars)
    used = 0
    per_chunk_cap = max(MIN_CONDENSED_CHARS, int(max_chars * MAX_CHUNK_SHARE))

    def _try_add(chunk: dict, target: list) -> bool:
        nonlocal used
        chunk_len = len(chunk.get("text", ""))
        if chunk_len == 0:
            return False
        if chunk_len > per_chunk_cap:
            chunk = condense_chunk(chunk, query, per_chunk_cap)
            chunk_len = len(chunk["text"])
        remaining = max_chars - used
        if chunk_len > remaining:
            if remaining < MIN_CONDENSED_CHARS:
                result.omitted += 1
                return False
            chunk = condense_chunk(chunk, query, remaining)
            chunk_len = len(chunk["text"])
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
