"""
Kitsune — A Repository Knowledge Base System

A system that makes AI truly understand your codebase — not just search it.
Kitsune indexes source code into a hybrid knowledge graph combining semantic
embeddings, a real dependency graph, and keyword search.

Quick Start
-----------
    from kitsune.engine import RepositoryKnowledgeEngine

    engine = RepositoryKnowledgeEngine()
    metrics = engine.index_repository("/path/to/repo")
    context = engine.search_semantic("how does authentication work?")
    answer = engine.ask("how does authentication work?")

Supported Languages
-------------------
Python, JavaScript, TypeScript, C, C++, Go, Java, Rust
"""
from kitsune.engine.knowledge_engine import RepositoryKnowledgeEngine
from kitsune.models.chunk import Chunk
from kitsune.models.concept import Concept
from kitsune.models.metrics import IndexMetrics, QueryMetrics

__version__ = "1.0.0"

__all__ = [
    "RepositoryKnowledgeEngine",
    "Chunk",
    "Concept",
    "IndexMetrics",
    "QueryMetrics",
    "__version__",
]
