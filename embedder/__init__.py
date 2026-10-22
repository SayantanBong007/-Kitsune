"""Kitsune embedder — contextualization, embedding, and reranking."""
from kitsune.embedder.embedder import (
    get_model,
    embed,
    embed_batch,
    embedding_dim,
)
from kitsune.embedder.contextualizer import (
    contextualize_chunk,
    build_context_note,
    build_llm_context_note,
)
from kitsune.embedder.reranker import rerank

__all__ = [
    "get_model",
    "embed",
    "embed_batch",
    "embedding_dim",
    "contextualize_chunk",
    "build_context_note",
    "build_llm_context_note",
    "rerank",
]
