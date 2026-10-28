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
import os
from typing import Optional

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# The model name — downloaded once and cached by sentence-transformers.
MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Module-level singleton — loaded ONCE per process, shared everywhere.
# Single source of truth — loaded once, shared everywhere.
_model: Optional[SentenceTransformer] = None


def _dimension(model) -> int:
    """Vector size; the method was renamed in sentence-transformers 5."""
    fn = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return fn()


def get_model() -> SentenceTransformer:
    """Load the model on first call; return cached instance afterwards.

    This is the single source of truth for the embedding model.
    All components (embedder, hybrid_search, reranker) should use this.
    """
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {MODEL_NAME}")
        try:
            _model = SentenceTransformer(MODEL_NAME)
        except Exception as exc:
            # Network trouble while checking the Hub for updates: use the cached copy
            logger.warning(f"Could not reach the model hub ({exc}); loading {MODEL_NAME} from cache")
            _model = SentenceTransformer(MODEL_NAME, local_files_only=True)
        # Speed vs. quality on CPU: embedding only the first N tokens of each chunk is
        # much faster (256 ≈ 1.7× vs the model's 512). Default: the model's own limit.
        max_tokens = os.getenv("KITSUNE_EMBED_MAX_TOKENS")
        if max_tokens and max_tokens.isdigit():
            _model.max_seq_length = min(int(max_tokens), _model.max_seq_length)
        logger.info(f"Embedding model loaded (dim={_dimension(_model)}, "
                    f"max tokens={_model.max_seq_length}).")
    return _model


def embed(text: str) -> list[float]:
    """Embed a single string into a 384-dim vector.

    Vectors are normalized so cosine similarity = dot product (faster search).
    """
    model = get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


# Texts handed to the model per encode() call. Within a call the model sorts
# texts by length, so short chunks are not padded up to a long neighbour's size.
_ENCODE_WINDOW = 1024


def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed multiple strings at once — 10-20x faster than one-by-one.

    Memory-safe: processes `_ENCODE_WINDOW` texts at a time, so large
    codebases (50,000+ chunks) never hold every activation at once.

    Parameters
    ----------
    texts:
        List of strings to embed.
    batch_size:
        Number of texts per forward pass. 32 measured fastest for bge-small
        on CPU (larger batches pad more).

    Returns
    -------
    List of embedding vectors, one per input text (same order).
    """
    model = get_model()
    all_vectors: list[list[float]] = []

    for i in range(0, len(texts), _ENCODE_WINDOW):
        window = texts[i:i + _ENCODE_WINDOW]
        vectors = model.encode(window, batch_size=batch_size,
                               normalize_embeddings=True, show_progress_bar=False)
        all_vectors.extend(vectors.tolist() if hasattr(vectors, 'tolist') else
                           [v.tolist() for v in vectors])

        if i > 0:
            logger.info(f"  Embedded {i}/{len(texts)} chunks...")

    return all_vectors


def embedding_dim() -> int:
    """Return the number of dimensions in each embedding vector (384)."""
    return _dimension(get_model()) or 384
