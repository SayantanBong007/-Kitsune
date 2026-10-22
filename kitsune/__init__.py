"""
Kitsune 2.0 — A Repository Knowledge Base System

The next-generation knowledge engine for ArchFox. Combines:
- AST-aware multi-language chunking (tree-sitter)
- Contextualized embeddings (BAAI/bge-small-en-v1.5)
- Hybrid retrieval: semantic + graph + keyword (BM25)
- Cross-encoder reranking for precision
- Multi-hop graph traversal with cycle detection
- Business concept extraction
- Incremental indexing with change detection
- Query routing for intelligent strategy selection
- Disk persistence for CI environments

Quick Start
-----------
    from kitsune_2_0.engine import RepositoryKnowledgeEngine

    engine = RepositoryKnowledgeEngine()
    metrics = engine.index_repository("/path/to/repo")
    context = engine.search_semantic("how does authentication work?")
    answer = engine.ask("how does authentication work?")

Supported Languages
-------------------
Python, JavaScript, TypeScript, C, C++, Go, Java, Rust
"""
from kitsune_2_0.engine.knowledge_engine import RepositoryKnowledgeEngine
from kitsune_2_0.models.chunk import Chunk
from kitsune_2_0.models.concept import Concept
from kitsune_2_0.models.metrics import IndexMetrics, QueryMetrics

__version__ = "2.0.0"

__all__ = [
    "RepositoryKnowledgeEngine",
    "Chunk",
    "Concept",
    "IndexMetrics",
    "QueryMetrics",
    "__version__",
]
