"""
RepositoryKnowledgeEngine — the unified facade for Kitsune.

This is the single entry point that any consumer uses to interact with
Kitsune. It wraps all subsystems behind a clean interface.

Features:
- Concept-level queries: search by business concept, not just code symbols.
- Multi-hop graph traversal: configurable depth.
- Query routing: intelligent strategy selection.
- Metrics and observability.
- Incremental indexing.
"""
from __future__ import annotations

import logging
import os

from kitsune.concepts.extractor import ConceptExtractor
from kitsune.concepts.store import ConceptStore
from kitsune.indexer import Indexer
from kitsune.models.metrics import IndexMetrics
from kitsune.query.cache import QueryCache
from kitsune.query.hybrid_search import HybridSearcher
from kitsune.query.router import route_query
from kitsune.query.synthesizer import Synthesizer
from kitsune.store.graph_store import GraphStore
from kitsune.store.keyword_store import KeywordStore
from kitsune.store.qdrant_store import QdrantVectorStore

logger = logging.getLogger(__name__)


class RepositoryKnowledgeEngine:
    """The unified programmatic interface for Kitsune.

    Usage
    -----
    engine = RepositoryKnowledgeEngine()
    metrics = engine.index_repository("/path/to/repo")
    context = engine.search_semantic("how does authentication work?")
    answer = engine.ask("how does authentication work?")
    """

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            data_dir = os.path.join(base_dir, "data", "kitsune")

        os.makedirs(data_dir, exist_ok=True)

        # Initialize vector store — Qdrant (server-based, scalable) with
        # automatic ChromaDB fallback if Qdrant is not configured.
        # Set QDRANT_URL env var to enable Qdrant; otherwise uses ChromaDB.
        self.vector_store = QdrantVectorStore()
        if self.vector_store.using_fallback:
            logger.info("Vector store: ChromaDB (embedded mode)")
        else:
            logger.info("Vector store: Qdrant (server mode)")

        self.graph_store = GraphStore(persist_path=os.path.join(data_dir, "graph.pkl"))
        self.keyword_store = KeywordStore(persist_path=os.path.join(data_dir, "bm25_index.pkl"))
        self.concept_store = ConceptStore(persist_path=os.path.join(data_dir, "concepts.json"))

        # Query cache — caches retrieval results for repeated/similar queries.
        # Returns instantly for cache hits, avoiding the full hybrid search.
        self.cache = QueryCache(persist_path=os.path.join(data_dir, "query_cache.pkl"))

        # Initialize the indexer and searcher
        self.indexer = Indexer(
            vector_store=self.vector_store,
            graph_store=self.graph_store,
            keyword_store=self.keyword_store,
        )
        self.searcher = HybridSearcher(
            vector_store=self.vector_store,
            graph_store=self.graph_store,
            keyword_store=self.keyword_store,
            cache=self.cache,
        )
        self.synthesizer = Synthesizer()
        self.concept_extractor = ConceptExtractor()

        # All extensions the tree-sitter parser supports
        self.SUPPORTED_EXTENSIONS = (
            ".py", ".js", ".jsx", ".ts", ".tsx",
            ".c", ".h", ".cpp", ".hpp", ".cc",
            ".go", ".java", ".rs",
        )

    # ── Indexing ──────────────────────────────────────────────────────────

    def index_repository(self, repo_path: str, incremental: bool = True) -> IndexMetrics:
        """Parse, chunk, and index an entire repository.

        Parameters
        ----------
        repo_path:
            Path to the repository root.
        incremental:
            If True (default), only re-index files that changed (S7).

        Returns
        -------
        IndexMetrics with full statistics.
        """
        logger.info(f"Indexing repository: {repo_path}")

        # Index all files
        metrics = self.indexer.index_directory(
            repo_path, extensions=self.SUPPORTED_EXTENSIONS, incremental=incremental
        )

        # Extract and store concepts (S12) — from ALREADY INDEXED data,
        # not by re-parsing the repo (which would defeat incremental indexing)
        chunks_for_concepts = self._collect_chunks_from_store()
        if chunks_for_concepts:
            concepts = self.concept_extractor.extract(chunks_for_concepts)
            self.concept_store.save(concepts)
            metrics.total_concepts = len(concepts)

        logger.info(f"Indexed {repo_path}: {metrics.total_chunks} chunks, "
                     f"{metrics.total_edges} edges, {metrics.total_concepts} concepts.")
        return metrics

    def _collect_chunks_from_store(self) -> list:
        """Collect chunks from the vector store for concept extraction.

        This reuses already-indexed data instead of re-parsing the repo.
        On a 50,000-file codebase, this saves minutes of redundant parsing.
        Works with both Qdrant and ChromaDB backends via the get_all() interface.
        """
        from kitsune.models.chunk import Chunk

        all_records = self.vector_store.get_all()
        if not all_records:
            return []

        chunks = []
        for record in all_records:
            meta = record
            doc = record.get("text", "")
            if not doc:
                continue
            chunk = Chunk(
                text=doc,
                chunk_type=meta.get("chunk_type", "function"),
                name=meta.get("name", ""),
                file_path=meta.get("file_path", ""),
                start_line=meta.get("start_line", 0),
                end_line=meta.get("end_line", 0),
                scope=meta.get("scope", ""),
                docstring=meta.get("docstring", ""),
                language=meta.get("language", "python"),
                calls=set(meta.get("calls", [])),
                inherits=meta.get("inherits", []),
                imports=meta.get("imports", []),
                decorators=meta.get("decorators", []),
            )
            chunks.append(chunk)
        return chunks

    # ── Querying ──────────────────────────────────────────────────────────

    def search_semantic(self, query: str, top_k: int = 5) -> str:
        """Run hybrid search and return a formatted context string.

        This is the main entry point for ArchFox's review agents.
        """
        plan = route_query(query)
        results = self.searcher.search(query, k_primary=top_k, plan=plan)
        return self.synthesizer.format_context(results)

    def search(self, query: str, top_k: int = 5) -> dict:
        """Run hybrid search and return raw results (not formatted).

        Returns the full SearchResult dict for programmatic use.
        """
        plan = route_query(query)
        return self.searcher.search(query, k_primary=top_k, plan=plan)

    def ask(self, question: str) -> str:
        """Ask a question and get a streaming LLM answer.

        This runs the full pipeline: route → search → synthesize.
        """
        plan = route_query(question)
        results = self.searcher.search(question, k_primary=plan.top_k, plan=plan)
        return self.synthesizer.generate_answer_sync(question, results)

    def ask_streaming(self, question: str):
        """Ask a question and yield streaming response chunks."""
        plan = route_query(question)
        results = self.searcher.search(question, k_primary=plan.top_k, plan=plan)
        yield from self.synthesizer.generate_answer(question, results)

    # ── Code-level queries ────────────────────────────────────────────────

    def get_source_code(self, entity_name: str) -> str | None:
        """Retrieve the exact source code for a specific function/class."""
        results = self.vector_store.get_by_name(entity_name)
        if results and len(results) > 0:
            return results[0].get("text")
        return None

    def get_downstream_dependencies(self, entity_name: str, depth: int = 2) -> list[str]:
        """Find all functions that this entity calls (multi-hop)."""
        return self.graph_store.get_dependencies(entity_name, depth=depth)

    def get_upstream_callers(self, entity_name: str, depth: int = 2) -> list[str]:
        """Find all functions that call this entity (multi-hop)."""
        return self.graph_store.get_upstream_callers(entity_name, depth=depth)

    def get_call_graph(self, entity_name: str, depth: int = 2) -> dict:
        """Return a structured topology of the call graph (S15)."""
        return self.graph_store.get_call_graph(entity_name, depth=depth)

    def get_inheritance_tree(self, class_name: str) -> list[str]:
        """Find all parent classes of a class."""
        return self.graph_store.get_inheritance_tree(class_name)

    # ── Concept-level queries (S12) ───────────────────────────────────────

    def get_concept(self, concept_name: str) -> dict | None:
        """Fetch a business concept by name."""
        concept = self.concept_store.get(concept_name)
        if not concept:
            return None
        return concept.to_metadata()

    def get_all_concepts(self) -> list[dict]:
        """Return all business concepts."""
        return [c.to_metadata() for c in self.concept_store.get_all()]

    def get_high_risk_concepts(self) -> list[dict]:
        """Return all critical or high-risk concepts."""
        return [c.to_metadata() for c in self.concept_store.get_high_risk_concepts()]

    def get_concept_dependencies(self, concept_name: str) -> dict:
        """Return a concept's dependency tree."""
        return self.concept_store.get_concept_dependencies(concept_name)

    # ── Impact analysis ───────────────────────────────────────────────────

    def get_impact_analysis(self, function_name: str, max_depth: int = 3) -> dict:
        """Analyze the blast radius of changing a function."""
        from kitsune.graph.traversal import get_impact_analysis
        return get_impact_analysis(self.graph_store, function_name, max_depth)

    # ── Maintenance ───────────────────────────────────────────────────────

    def clear_all(self):
        """Delete all indexed data from all stores."""
        self.vector_store.clear()
        self.graph_store.clear_all()
        self.keyword_store.clear()
        self.concept_store.clear()
        self.cache.clear()
        logger.info("All Kitsune stores cleared.")

    def stats(self) -> dict:
        """Return statistics about all stores."""
        return {
            "vector_store": {
                "count": self.vector_store.count(),
                "backend": "chromadb (fallback)" if self.vector_store.using_fallback else "qdrant",
            },
            "graph_store": self.graph_store.stats(),
            "keyword_store": {"count": self.keyword_store.count()},
            "concept_store": {"count": self.concept_store.count()},
            "query_cache": self.cache.stats(),
        }
