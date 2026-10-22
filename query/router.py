"""
Query router — classifies queries and selects the best retrieval strategy.

Features:
- Detects query intent: lookup, dependency, impact, or complex question.
- Routes to the most efficient retrieval strategy.
"""
from __future__ import annotations

import os
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

# "What calls X?" / "Who uses X?" / "X's callers" / "What does X call?"
_SYMBOL = r"[\w.`'\"()]+"
_DEPENDENCY_PATTERNS = [
    re.compile(r"(what|who).*(calls?|uses?|invokes?|triggers?)\s+\w+", re.I),
    re.compile(r"(callers?|dependents?|upstream)\s+(of|for)?\s*\w+", re.I),
    re.compile(r"what.*depends?\s+on\s+\w+", re.I),
    re.compile(r"\b(callees?|usages?|references?|dependencies|call sites?)\b", re.I),
    re.compile(rf"\bwhat\s+(does|do)\s+{_SYMBOL}\s+(call|use|invoke|depend\s+on)\b", re.I),
    re.compile(rf"\bwhat\s+(functions?|methods?|code)\s+(does|do)\s+{_SYMBOL}\s+(call|use|invoke)\b", re.I),
    re.compile(r"\b(which|what)\s+(functions?|methods?|code|classes?|modules?|files?)\s+"
               r"(calls?|uses?|invokes?|references?|depends?)\b", re.I),
    re.compile(rf"\bwhere\s+(is|are)\s+{_SYMBOL}\s+(used|called|invoked|referenced)\b", re.I),
    re.compile(r"\b(invoked|called|used)\s+by\b", re.I),
    re.compile(r"\b(functions?|methods?|code|classes?|modules?)\s+(that|which)\s+"
               r"(calls?|uses?|invokes?|depends?\s+on|references?|relies?\s+on)\b", re.I),
    re.compile(r"\bwho\s+(references?|imports?)\b", re.I),
]

# "What happens if I change X?" / "Impact of X" / "Break if X" / "Is it safe to rename X?"
_CHANGE = r"(change|changing|modify|modifying|rename|renaming|delete|deleting|remove|removing|refactor|refactoring|edit|editing|touch)"
_IMPACT_PATTERNS = [
    re.compile(r"(impact|blast radius|affect|break).*(if|of|when|changing?)", re.I),
    re.compile(r"what.*(happens|breaks?).*(if|when|change)", re.I),
    re.compile(rf"\b(safe|risky|risk)\b.*\b{_CHANGE}\b", re.I),
    re.compile(rf"\b(if|when)\s+(i|we|you)\s+{_CHANGE}\b.*\b(affected|impacted|break|happens?)\b", re.I),
    re.compile(r"\bwhat\b.*\b(affected|impacted)\b", re.I),
]

# "Where is X defined?" / "definition of X" — checked before concept patterns
_DEFINITION_PATTERNS = [
    re.compile(rf"\bwhere\s+(is|are)\s+{_SYMBOL}\s+(defined|declared|located)\b", re.I),
    re.compile(r"\b(definition|declaration|source code)\s+(of|for)\b", re.I),
]

# "How does X work?" / "Explain the X system" / "Architecture of X"
_CONCEPT_PATTERNS = [
    re.compile(r"how\s+(does|do|is|are)\s+", re.I),
    re.compile(r"(explain|describe|architecture|overview|design)\s+(of\s+)?(the\s+)?\w+", re.I),
    re.compile(r"(how|where)\s+is\s+\w+\s+(handled|implemented|done)", re.I),
]


_PLANS = {
    "lookup": dict(strategy="direct_lookup", use_graph=False, use_reranker=False, graph_depth=0, top_k=3),
    "dependency": dict(strategy="graph_only", use_graph=True, use_reranker=False, graph_depth=2, top_k=3),
    "impact": dict(strategy="graph_only", use_graph=True, use_reranker=False, graph_depth=3, top_k=3),
    "concept": dict(strategy="hybrid", use_graph=True, use_reranker=True, graph_depth=2, top_k=5),
    "complex": dict(strategy="hybrid", use_graph=True, use_reranker=True, graph_depth=2, top_k=5),
}

# ── Semantic fallback ─────────────────────────────────────────────────────────
# When no pattern matches, the question is compared (with the embedding model
# Kitsune already loads) to example questions of each type. Only a confident
# match re-routes; otherwise it stays "complex" (full hybrid search).
_EXAMPLES = {
    "dependency": [
        "what calls this function", "who uses this method", "find every place this is called",
        "which parts of the code depend on this", "what does this function call internally",
        "list everything that invokes this", "show me the call sites", "trace who triggers this",
    ],
    "impact": [
        "what will break if I change this", "is it safe to modify this function",
        "what is affected by editing this", "consequences of removing this method",
        "blast radius of this change", "can I delete this without breaking anything",
    ],
    "lookup": [
        "show me the code for this function", "where is this class defined",
        "print the source of this method", "open the implementation of this",
    ],
    "concept": [
        "how does this feature work", "explain the architecture of this system",
        "walk me through the flow", "give me an overview of this module",
        "describe how data moves through the app",
    ],
}
SEMANTIC_THRESHOLD = 0.65   # min cosine similarity to an example (bge-small scale)
SEMANTIC_MARGIN = 0.03      # and this much better than the best other type
_example_vectors = None


def _semantic_type(query: str) -> str | None:
    """Nearest example type for a query, or None if not confident."""
    global _example_vectors
    if os.getenv("KITSUNE_ROUTER_SEMANTIC", "1") == "0":
        return None
    try:
        import numpy as np
        from kitsune.embedder.embedder import get_model
        model = get_model()
        if _example_vectors is None:
            _example_vectors = {
                t: np.asarray(model.encode(examples, normalize_embeddings=True))
                for t, examples in _EXAMPLES.items()
            }
        q = np.asarray(model.encode(query, normalize_embeddings=True))
        scores = {t: float((vecs @ q).max()) for t, vecs in _example_vectors.items()}
    except Exception:
        return None
    best = max(scores, key=scores.get)
    runner_up = max(v for t, v in scores.items() if t != best)
    if scores[best] >= SEMANTIC_THRESHOLD and scores[best] - runner_up >= SEMANTIC_MARGIN:
        return best
    return None


def classify_query(query: str) -> str:
    """Return the query type: lookup, dependency, impact, concept, or complex."""
    q = query.strip()
    # Most specific first: impact > dependency > "where is X defined" > concept > lookup
    for query_type, patterns in (("impact", _IMPACT_PATTERNS),
                                 ("dependency", _DEPENDENCY_PATTERNS),
                                 ("lookup", _DEFINITION_PATTERNS),
                                 ("concept", _CONCEPT_PATTERNS),
                                 ("lookup", _LOOKUP_PATTERNS)):
        if any(p.search(q) for p in patterns):
            return query_type
    return _semantic_type(q) or "complex"


def route_query(query: str) -> QueryPlan:
    """Classify a query and return a retrieval plan.

    The router matches intent patterns first, then (for anything they miss)
    compares the question to example questions using the embedding model,
    and selects the most efficient retrieval strategy:

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
    query_type = classify_query(query)
    return QueryPlan(query_type=query_type, **_PLANS[query_type])
