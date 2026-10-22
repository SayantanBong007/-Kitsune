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
import os as _os

# Load configuration from kitsune/.env (as the README describes), then from a
# .env in the working directory. Existing environment variables win.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_os.path.join(_os.path.dirname(__file__), ".env"))
    _load_dotenv()
except ImportError:
    pass

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
