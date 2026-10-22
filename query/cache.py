"""
Query cache — caches retrieval results for repeated queries.

On a large codebase, many questions are similar ("how does X work?", "what
calls Y?"). Without a cache, every question triggers a full hybrid search
(embed query → ChromaDB → graph traversal → BM25 → rerank). With a cache,
repeated or similar questions return instantly.

Two cache strategies:
1. Exact match:    query string hash → cached result (instant, O(1))
2. Semantic match:  query embedding similarity → cached result (near-instant)

The cache uses a simple LRU (Least Recently Used) eviction policy with a
configurable max size. It's in-memory by default, but can be persisted to
disk for reuse across restarts.

Use cases:
- Multiple ArchFox agents asking similar questions about the same PR
- Chat bot users asking the same question in different words
- CI/CD pipelines reviewing similar PRs repeatedly
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import os
import pickle
import re
import time
from collections import OrderedDict
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 500           # Max cached queries
DEFAULT_TTL_SECONDS = 3600       # 1 hour — stale entries are evicted
SIMILARITY_THRESHOLD = 0.92      # Cosine similarity for semantic cache hit

# Code-like tokens: snake_case, camelCase/PascalCase, dotted paths, `backticked`, foo()
_IDENTIFIER_PATTERN = re.compile(
    r"`([^`]+)`"
    r"|\b(\w+(?:\.\w+)+)\b"
    r"|\b(\w*_\w*|[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*)\b"
    r"|\b(\w+)\("
)


def _identifiers(query: str) -> frozenset[str]:
    """Return the code identifiers mentioned in a query."""
    return frozenset(
        next(g for g in m.groups() if g) for m in _IDENTIFIER_PATTERN.finditer(query)
    )


class QueryCache:
    """LRU cache for hybrid search results with semantic similarity matching.

    The cache stores:
    - Exact query hash → result (instant lookup)
    - Query embedding → result (semantic similarity lookup)

    When a new query comes in:
    1. Check exact hash match → return immediately if found
    2. Check semantic similarity → return if above threshold
    3. Otherwise: cache miss, execute the real search

    Usage
    -----
    cache = QueryCache()

    # Check cache first
    cached = cache.get("how does authentication work?")
    if cached:
        return cached  # Instant!

    # Cache miss — run the real search
    result = hybrid_searcher.search("how does authentication work?")

    # Store in cache for next time
    cache.put("how does authentication work?", result, query_vector=vec)
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        persist_path: str | None = None,
    ):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._similarity_threshold = similarity_threshold
        self._persist_path = persist_path or os.path.join(
            os.getcwd(), "data", "kitsune", "query_cache.pkl"
        )

        # OrderedDict for LRU: key = query_hash, value = cache entry
        self._cache: OrderedDict[str, dict] = OrderedDict()

        # Stats
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0

        self._load_from_disk()
        # put() is hot, so persist once at interpreter exit (and on flush/clear)
        atexit.register(self._save_to_disk)

    def get(self, query: str, query_vector: list[float] | None = None) -> Any | None:
        """Check the cache for a matching result.

        Parameters
        ----------
        query:
            The user's query string.
        query_vector:
            Optional embedding of the query. If provided, enables semantic
            similarity matching (catches paraphrased queries).

        Returns
        -------
        The cached result if found, None if cache miss.
        """
        query_hash = self._hash_query(query)

        # ── Strategy 1: Exact match (instant, O(1)) ──────────────────────
        if query_hash in self._cache:
            entry = self._cache[query_hash]
            if self._is_fresh(entry):
                # Move to end (most recently used)
                self._cache.move_to_end(query_hash)
                self._hits += 1
                logger.debug(f"Cache HIT (exact): '{query[:50]}...'")
                return entry["result"]
            else:
                # Stale entry — remove it
                del self._cache[query_hash]

        # ── Strategy 2: Semantic similarity match ────────────────────────
        if query_vector is not None:
            cached_result = self._semantic_lookup(query_vector, _identifiers(query))
            if cached_result is not None:
                self._semantic_hits += 1
                logger.debug(f"Cache HIT (semantic): '{query[:50]}...'")
                return cached_result

        self._misses += 1
        return None

    def put(self, query: str, result: Any, query_vector: list[float] | None = None):
        """Store a result in the cache.

        Parameters
        ----------
        query:
            The query string.
        result:
            The search result to cache.
        query_vector:
            Optional embedding of the query for semantic matching.
        """
        query_hash = self._hash_query(query)

        # Evict LRU entry if at capacity
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)  # Remove oldest

        self._cache[query_hash] = {
            "result": result,
            "query": query,
            "vector": query_vector,
            "timestamp": time.time(),
        }

    def _semantic_lookup(self, query_vector: list[float],
                         identifiers: frozenset[str] = frozenset()) -> Any | None:
        """Find a cached result with a semantically similar query.

        Compares the query embedding against all cached query embeddings
        using cosine similarity. Returns the result if similarity exceeds
        the threshold AND both queries name the same code identifiers —
        "what calls get_user?" and "what calls get_order?" embed almost
        identically but need different answers.
        """
        query_vec = np.array(query_vector, dtype=np.float32)

        best_score = 0.0
        best_result = None

        for entry in self._cache.values():
            if not self._is_fresh(entry):
                continue
            cached_vec = entry.get("vector")
            if cached_vec is None:
                continue
            if _identifiers(entry.get("query", "")) != identifiers:
                continue

            cached_vec = np.array(cached_vec, dtype=np.float32)
            # Cosine similarity (vectors are already normalized)
            score = float(np.dot(query_vec, cached_vec))

            if score > best_score:
                best_score = score
                best_result = entry["result"]

        if best_score >= self._similarity_threshold:
            return best_result
        return None

    def _hash_query(self, query: str) -> str:
        """Create a stable hash for a query string."""
        normalized = query.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _is_fresh(self, entry: dict) -> bool:
        """Check if a cache entry is still within its TTL."""
        age = time.time() - entry.get("timestamp", 0)
        return age < self._ttl_seconds

    def stats(self) -> dict:
        """Return cache statistics."""
        total = self._hits + self._semantic_hits + self._misses
        hit_rate = (self._hits + self._semantic_hits) / total * 100 if total > 0 else 0
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "exact_hits": self._hits,
            "semantic_hits": self._semantic_hits,
            "misses": self._misses,
            "hit_rate_percent": round(hit_rate, 1),
        }

    def clear(self):
        """Clear all cached entries."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0
        self._save_to_disk()

    def _load_from_disk(self):
        """Load cached entries from disk."""
        from pathlib import Path
        path = Path(self._persist_path)
        if not path.exists():
            return

        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._cache = data.get("cache", OrderedDict())
            logger.info(f"Loaded {len(self._cache)} cached queries from disk.")
        except Exception as e:
            logger.warning(f"Could not load query cache from disk: {e}")

    def _save_to_disk(self):
        """Persist cached entries to disk."""
        from pathlib import Path
        path = Path(self._persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(path, "wb") as f:
                pickle.dump({"cache": self._cache}, f)
        except Exception as e:
            logger.warning(f"Could not save query cache to disk: {e}")

    def flush(self):
        """Manually persist the cache to disk."""
        self._save_to_disk()
