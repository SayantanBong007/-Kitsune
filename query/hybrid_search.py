"""
Hybrid search — combines semantic, graph, and keyword retrieval.

This is the heart of Kitsune's retrieval. It combines three "tails":
1. Semantic search (ChromaDB/Qdrant) — finds chunks with similar MEANING
2. Graph traversal (Neo4j/NetworkX) — finds chunks with structural RELATIONSHIPS
3. Keyword search (BM25) — finds chunks with exact KEYWORD matches

Features:
- Multi-hop graph traversal: configurable depth.
- BM25 keyword search: the third retrieval "tail".
- Cross-encoder reranking: re-ranks results for better relevance.
- Query routing: uses QueryPlan to select the best strategy.
- Reuses the singleton embedding model.
"""
from __future__ import annotations

import logging
import time
from typing import TypedDict

from kitsune.embedder.embedder import get_model
from kitsune.embedder.reranker import rerank
from kitsune.models.metrics import QueryMetrics, Timer
from kitsune.query.cache import QueryCache
from kitsune.query.context_budget import apply_context_budget, DEFAULT_BUDGET
from kitsune.query.router import QueryPlan, route_query
from kitsune.store.graph_store import GraphStore
from kitsune.store.keyword_store import KeywordStore
from kitsune.store.vector_store import VectorStore

logger = logging.getLogger(__name__)


class SearchResult(TypedDict):
    primary_chunks: list[dict]
    dependency_chunks: list[dict]
    keyword_chunks: list[dict]
    graph_paths: dict
    query_type: str
    metrics: QueryMetrics


class HybridSearcher:
    """Combines Vector, Graph, and Keyword search with reranking.

    This is the 2.0 evolution of Kitsune's hybrid search. It adds:
    - BM25 keyword search as a third retrieval signal
    - Cross-encoder reranking for better precision
    - Multi-hop graph traversal
    - Query routing for efficiency
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        graph_store: GraphStore | None = None,
        keyword_store: KeywordStore | None = None,
        cache: QueryCache | None = None,
    ):
        self._vector = vector_store or VectorStore()
        self._graph = graph_store or GraphStore()
        self._keyword = keyword_store or KeywordStore()
        self._cache = cache  # Optional query cache

        # Reuse the singleton embedding model (S13 fix)
        logger.info("HybridSearcher: using shared embedding model singleton.")
        self._embedder = get_model()

    def search(self, query: str, k_primary: int = 5, plan: QueryPlan | None = None,
               max_context_chars: int = DEFAULT_BUDGET) -> SearchResult:
        """Run the hybrid search pipeline.

        If a QueryPlan is provided, uses it to select the retrieval strategy.
        Otherwise, routes the query automatically (S10).

        Parameters
        ----------
        query:
            Natural language question or code query.
        k_primary:
            Number of primary chunks to retrieve (overridden by plan if provided).
        plan:
            Optional QueryPlan from the query router.
        max_context_chars:
            Maximum total characters of retrieved context. Prevents LLM context
            window overflow on large codebases where hub functions have many deps.

        Returns
        -------
        SearchResult with primary, dependency, and keyword chunks (budget-limited).
        """
        metrics = QueryMetrics(query=query)
        metrics.query_type = "unknown"

        if plan is None:
            plan = route_query(query)
            metrics.query_type = plan.query_type
        else:
            metrics.query_type = plan.query_type

        # ── Cache check: return instantly for repeated/similar queries ──
        # Embed the query once for both cache lookup and semantic search
        query_vector = None
        if plan.strategy in ("hybrid", "direct_lookup"):
            query_vector = self._embedder.encode(
                query, normalize_embeddings=True
            ).tolist()

        if self._cache is not None:
            cached = self._cache.get(query, query_vector=query_vector)
            if cached is not None:
                logger.info(f"Cache HIT — returning cached result for '{query[:50]}...'")
                cached["metrics"].query_type = metrics.query_type + " (cached)"
                return cached

        # Use plan's top_k if not explicitly overridden
        effective_k = k_primary if k_primary != 5 else plan.top_k

        primary_chunks: list[dict] = []
        dependency_chunks: list[dict] = []
        keyword_chunks: list[dict] = []
        graph_paths: dict = {}

        with Timer(metrics, "retrieval_time_seconds"):
            # ── Step 1: Semantic search (Qdrant/ChromaDB) ────────────────
            if plan.strategy in ("hybrid", "direct_lookup"):
                # query_vector was already computed for cache lookup
                primary_chunks = self._vector.search(query_vector, k=effective_k)
                logger.info(f"Semantic search: {len(primary_chunks)} primary chunks")

            # ── Step 2: Keyword search (BM25) ─────────────────────────────
            if plan.strategy == "hybrid":
                keyword_results = self._keyword.search(query, k=effective_k)
                # Filter out chunks already in primary results
                primary_ids = {c.get("qualified_name") or c.get("name") for c in primary_chunks}
                keyword_chunks = [
                    c for c in keyword_results
                    if (c.get("qualified_name") or c.get("name")) not in primary_ids
                ]
                logger.info(f"Keyword search: {len(keyword_chunks)} additional chunks")

            # ── Step 3: Graph traversal (multi-hop) ───────────────────────
            if plan.use_graph and primary_chunks:
                dependency_names: set[str] = set()
                for chunk in primary_chunks:
                    chunk_name = chunk.get("name") or chunk.get("qualified_name", "")
                    if chunk_name:
                        deps = self._graph.get_dependencies(chunk_name, depth=plan.graph_depth)
                        dependency_names.update(deps)

                # Fetch dependency code from vector store
                primary_names = {c.get("name") for c in primary_chunks}
                for dep_name in dependency_names:
                    if dep_name in primary_names:
                        continue
                    dep_results = self._vector.get_by_name(dep_name)
                    if dep_results:
                        dependency_chunks.extend(dep_results)

                metrics.graph_depth_reached = plan.graph_depth
                logger.info(f"Graph traversal (depth={plan.graph_depth}): "
                            f"{len(dependency_chunks)} dependency chunks")

            # ── Step 4: Cross-encoder reranking (S6) ──────────────────────
            if plan.use_reranker and primary_chunks:
                primary_chunks = rerank(query, primary_chunks, top_k=effective_k)
                logger.info(f"Reranked primary chunks")

            # ── Step 5: Context budget management ─────────────────────────
            # On large codebases, a hub function can pull in hundreds of
            # dependency chunks. Apply a character budget to prevent LLM
            # context window overflow — drops whole chunks, never cuts mid-function.
            budget_result = apply_context_budget(
                primary_chunks, dependency_chunks, keyword_chunks,
                max_chars=max_context_chars
            )
            primary_chunks = budget_result.primary_chunks
            dependency_chunks = budget_result.dependency_chunks
            keyword_chunks = budget_result.keyword_chunks

        metrics.primary_chunks_found = len(primary_chunks)
        metrics.dependency_chunks_found = len(dependency_chunks)

        # Calculate total context size
        total_chars = sum(len(c.get("text", "")) for c in primary_chunks)
        total_chars += sum(len(c.get("text", "")) for c in dependency_chunks)
        total_chars += sum(len(c.get("text", "")) for c in keyword_chunks)
        metrics.total_context_chars = total_chars

        logger.info(metrics.summary())

        result = {
            "primary_chunks": primary_chunks,
            "dependency_chunks": dependency_chunks,
            "keyword_chunks": keyword_chunks,
            "graph_paths": graph_paths,
            "query_type": metrics.query_type,
            "metrics": metrics,
        }

        # ── Cache write: store result for future repeated/similar queries ──
        if self._cache is not None:
            self._cache.put(query, result, query_vector=query_vector)

        return result
