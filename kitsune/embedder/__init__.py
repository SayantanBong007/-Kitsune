"""Kitsune 2.0 embedder — contextualization, embedding, and reranking."""
from kitsune_2_0.embedder.embedder import (
    get_model,
    embed,
    embed_batch,
    embedding_dim,
)
from kitsune_2_0.embedder.contextualizer import (
    contextualize_chunk,
    build_context_note,
    build_llm_context_note,
)
from kitsune_2_0.embedder.reranker import rerank

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
