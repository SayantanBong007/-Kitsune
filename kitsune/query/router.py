"""
Query router — classifies queries and selects the best retrieval strategy.

Improvements over Kitsune 1.0:
- New component (S10): Kitsune 1.0 sent every query through the same pipeline.
- Detects query intent: lookup, dependency, impact, or complex question.
- Routes to the most efficient retrieval strategy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

QueryType = Literal["lookup", "dependency", "impact", "complex", "concept"]


@dataclass
class QueryPlan:
    """The routing decision for a query.

    Attributes
    ----------
    query_type:
        The detected query category.
    strategy:
        Which retrieval strategy to use:
        - "direct_lookup": fetch by name from vector store
        - "graph_only": traverse the graph only
        - "keyword_only": BM25 search only
        - "hybrid": full semantic + graph + keyword + rerank
    use_graph:
        Whether to traverse the dependency graph.
    use_reranker:
        Whether to apply cross-encoder reranking.
    graph_depth:
        How many hops to traverse in the graph.
    top_k:
        How many primary chunks to retrieve.
    """
    query_type: QueryType
    strategy: str
    use_graph: bool
    use_reranker: bool
    graph_depth: int
    top_k: int


# ── Pattern-based query classification ────────────────────────────────────────

# "What does X do?" / "Show me X" / "Find X"
_LOOKUP_PATTERNS = [
    re.compile(r"^(what|show|find|get|display|explain)\s+(me\s+)?(the\s+)?\w+", re.I),
    re.compile(r"^(what is|what does|what's)\s+", re.I),
]

# "What calls X?" / "Who uses X?" / "X's callers"
_DEPENDENCY_PATTERNS = [
    re.compile(r"(what|who).*(calls?|uses?|invokes?|triggers?)\s+\w+", re.I),
    re.compile(r"(callers?|dependents?|upstream)\s+(of|for)?\s*\w+", re.I),
    re.compile(r"what.*depends?\s+on\s+\w+", re.I),
]

# "What happens if I change X?" / "Impact of X" / "Break if X"
_IMPACT_PATTERNS = [
    re.compile(r"(impact|blast radius|affect|break).*(if|of|when|changing?)", re.I),
    re.compile(r"what.*(happens|breaks?).*(if|when|change)", re.I),
]

# "How does X work?" / "Explain the X system" / "Architecture of X"
_CONCEPT_PATTERNS = [
    re.compile(r"how\s+(does|do|is|are)\s+", re.I),
    re.compile(r"(explain|describe|architecture|overview|design)\s+(of\s+)?(the\s+)?\w+", re.I),
    re.compile(r"(how|where)\s+is\s+\w+\s+(handled|implemented|done)", re.I),
]


def route_query(query: str) -> QueryPlan:
    """Classify a query and return a retrieval plan.

    The router uses pattern matching to detect the query's intent and
    selects the most efficient retrieval strategy:

    | Query Type   | Strategy         | Graph? | Reranker? | Depth | Top-K |
    |--------------|------------------|--------|-----------|-------|-------|
    | lookup       | direct_lookup    | No     | No        | 0     | 3     |
    | dependency   | graph_only       | Yes    | No        | 2     | 3     |
    | impact       | graph_only       | Yes    | No        | 3     | 3     |
    | concept      | hybrid           | Yes    | Yes       | 2     | 5     |
    | complex      | hybrid           | Yes    | Yes       | 2     | 5     |

    Parameters
    ----------
    query:
        The user's natural language question.

    Returns
    -------
    A QueryPlan describing how to retrieve context for this query.
    """
    query_stripped = query.strip()

    # Check impact queries first (most specific)
    for pattern in _IMPACT_PATTERNS:
        if pattern.search(query_stripped):
            return QueryPlan(
                query_type="impact",
                strategy="graph_only",
                use_graph=True,
                use_reranker=False,
                graph_depth=3,
                top_k=3,
            )

    # Check dependency queries
    for pattern in _DEPENDENCY_PATTERNS:
        if pattern.search(query_stripped):
            return QueryPlan(
                query_type="dependency",
                strategy="graph_only",
                use_graph=True,
                use_reranker=False,
                graph_depth=2,
                top_k=3,
            )

    # Check concept queries
    for pattern in _CONCEPT_PATTERNS:
        if pattern.search(query_stripped):
            return QueryPlan(
                query_type="concept",
                strategy="hybrid",
                use_graph=True,
                use_reranker=True,
                graph_depth=2,
                top_k=5,
            )

    # Check lookup queries
    for pattern in _LOOKUP_PATTERNS:
        if pattern.search(query_stripped):
            return QueryPlan(
                query_type="lookup",
                strategy="direct_lookup",
                use_graph=False,
                use_reranker=False,
                graph_depth=0,
                top_k=3,
            )

    # Default: complex question → full hybrid search
    return QueryPlan(
        query_type="complex",
        strategy="hybrid",
        use_graph=True,
        use_reranker=True,
        graph_depth=2,
        top_k=5,
    )
