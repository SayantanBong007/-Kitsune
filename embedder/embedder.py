"""
Embedder — converts text into vectors using sentence-transformers.

Uses BAAI/bge-small-en-v1.5 (384-dim, local, free, offline).

Features:
- Single module-level singleton shared by ALL components
- HybridSearcher reuses this singleton instead of loading its own copy
- Batch embedding with progress bar
"""
from __future__ import annotations

import logging
from typing import Optional

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# The model name — downloaded once and cached by sentence-transformers.
MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Module-level singleton — loaded ONCE per process, shared everywhere.
# Single source of truth — loaded once, shared everywhere.
_model: Optional[SentenceTransformer] = None


def get_model() -> SentenceTransformer:
    """Load the model on first call; return cached instance afterwards.

    This is the single source of truth for the embedding model.
    All components (embedder, hybrid_search, reranker) should use this.
    """
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {MODEL_NAME}")
        _model = SentenceTransformer(MODEL_NAME)
        logger.info(f"Embedding model loaded (dim={_model.get_sentence_embedding_dimension()}).")
    return _model


def embed(text: str) -> list[float]:
    """Embed a single string into a 384-dim vector.

    Vectors are normalized so cosine similarity = dot product (faster search).
    """
    model = get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed multiple strings at once — 10-20x faster than one-by-one.

    Memory-safe: processes in chunks of `batch_size` to avoid OOM on
    large codebases (50,000+ chunks). Each batch is embedded and freed
    before the next one loads.

    Parameters
    ----------
    texts:
        List of strings to embed.
    batch_size:
        Number of texts to embed per forward pass. 64 is a good default
        for bge-small on CPU. Reduce to 32 if memory is tight.

    Returns
    -------
    List of embedding vectors, one per input text (same order).
    """
    model = get_model()
    all_vectors: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        vectors = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        all_vectors.extend(vectors.tolist() if hasattr(vectors, 'tolist') else
                           [v.tolist() for v in vectors])

        if i % (batch_size * 10) == 0 and i > 0:
            logger.info(f"  Embedded {i}/{len(texts)} chunks...")

    return all_vectors


def embedding_dim() -> int:
    """Return the number of dimensions in each embedding vector (384)."""
    return get_model().get_sentence_embedding_dimension() or 384
