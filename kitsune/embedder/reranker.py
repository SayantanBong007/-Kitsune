"""
Cross-encoder reranker — re-ranks retrieval results for better relevance.

A cross-encoder processes the query and document TOGETHER in a single
forward pass, producing a relevance score. This is much more accurate than
a bi-encoder (which embeds query and document separately and compares vectors).

Improvements over Kitsune 1.0:
- New component (S6): Kitsune 1.0 had no reranking at all.
- Uses a lightweight model (ms-marco-MiniLM-L-6-v2) for speed.
- Falls back gracefully if the model can't be loaded.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Lightweight cross-encoder model for relevance scoring.
# ~80MB, runs on CPU, much more accurate than bi-encoder similarity.
RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_reranker = None


def _get_reranker():
    """Load the cross-encoder model on first call; cache afterwards."""
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"Loading reranker model: {RERANKER_MODEL_NAME}")
            _reranker = CrossEncoder(RERANKER_MODEL_NAME)
            logger.info("Reranker model loaded.")
        except Exception as e:
            logger.warning(f"Could not load reranker model: {e}")
            _reranker = False  # Mark as unavailable (not None, so we don't retry)
    return _reranker if _reranker is not False else None


def rerank(query: str, documents: list[dict], top_k: int = 5) -> list[dict]:
    """Re-rank documents by relevance to the query using a cross-encoder.

    Parameters
    ----------
    query:
        The user's question or search query.
    documents:
        List of document dicts, each must have a 'text' key.
    top_k:
        Maximum number of documents to return after reranking.

    Returns
    -------
        Re-ranked list of document dicts (highest relevance first),
        with an added 'rerank_score' field. If the reranker is unavailable,
        returns the original list (truncated to top_k).
    """
    if not documents:
        return []

    reranker = _get_reranker()
    if reranker is None:
        # Fallback: return original order, truncated
        logger.debug("Reranker unavailable — returning original order.")
        return documents[:top_k]

    try:
        # Build query-document pairs
        pairs = [(query, doc.get("text", "")) for doc in documents]

        # Score all pairs
        scores = reranker.predict(pairs)

        # Attach scores and sort
        scored = list(zip(documents, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        result = []
        for doc, score in scored[:top_k]:
            doc = dict(doc)  # shallow copy
            doc["rerank_score"] = float(score)
            result.append(doc)

        return result

    except Exception as e:
        logger.warning(f"Reranking failed: {e} — returning original order.")
        return documents[:top_k]
