"""
BM25 keyword index — exact-match search for code symbols.

This is the "third tail" of Kitsune (S5). While semantic search finds
chunks with similar MEANING, BM25 finds chunks with exact KEYWORD matches.

Use case: searching for "SQL injection" should find chunks that literally
contain "SQL injection" even if their embedding is dissimilar to the query.

Scalability fixes for large codebases:
- Dict-based storage (O(1) upsert) instead of list rebuild (was O(N) per upsert)
- Pickle persistence instead of JSON (faster for large corpora)
"""
from __future__ import annotations

import logging
import os
import pickle
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Tokenize: split on non-alphanumeric, lowercase, filter short tokens
_TOKEN_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase terms for BM25 indexing."""
    return [t.lower() for t in _TOKEN_PATTERN.findall(text)]


class KeywordStore:
    """BM25 keyword index for exact-match search.

    Uses dict-based storage for O(1) upsert (was O(N) list rebuild).
    Persists to disk as pickle for fast load/save on large corpora.
    """

    def __init__(self, persist_path: str | None = None):
        self._persist_path = persist_path or os.path.join(
            os.getcwd(), "data", "kitsune", "bm25_index.pkl"
        )
        # Dict-based: chunk_id → {tokens, metadata} — O(1) upsert
        self._corpus: dict[str, dict] = {}
        self._bm25 = None
        self._tokenized_cache: list[list[str]] = []  # rebuilt on build()

        try:
            from rank_bm25 import BM25Okapi
            self._bm25_class = BM25Okapi
        except ImportError:
            import math
            from collections import Counter

            class _PureBM25:
                def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
                    self.k1 = k1
                    self.b = b
                    self.corpus_size = len(corpus)
                    self.doc_lens = [len(d) for d in corpus]
                    self.avgdl = sum(self.doc_lens) / (self.corpus_size or 1)
                    self.doc_freqs = []
                    self.idf = {}
                    df = Counter()
                    for d in corpus:
                        cnt = Counter(d)
                        self.doc_freqs.append(cnt)
                        for tok in cnt:
                            df[tok] += 1
                    for tok, freq in df.items():
                        self.idf[tok] = math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1.0)

                def get_scores(self, query: list[str]) -> list[float]:
                    scores = [0.0] * self.corpus_size
                    for tok in query:
                        if tok not in self.idf:
                            continue
                        idf_val = self.idf[tok]
                        for i, freqs in enumerate(self.doc_freqs):
                            tf = freqs.get(tok, 0)
                            if tf > 0:
                                denom = tf + self.k1 * (1 - self.b + self.b * (self.doc_lens[i] / (self.avgdl or 1)))
                                scores[i] += idf_val * (tf * (self.k1 + 1)) / (denom or 1)
                    return scores

            logger.info("rank_bm25 not installed — using built-in pure-Python BM25 fallback.")
            self._bm25_class = _PureBM25

        self._load_from_disk()

    def _load_from_disk(self):
        """Load the BM25 corpus from disk if it exists."""
        path = Path(self._persist_path)
        if not path.exists():
            return

        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._corpus = data.get("corpus", {})
            self._rebuild_index()
            logger.info(f"Loaded BM25 index from disk: {len(self._corpus)} documents.")
        except Exception as e:
            logger.warning(f"Could not load BM25 index from disk: {e}")

    def _save_to_disk(self):
        """Persist the BM25 corpus to disk as pickle."""
        path = Path(self._persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"corpus": self._corpus}, f)

    def _rebuild_index(self):
        """Rebuild the BM25 index from the in-memory corpus."""
        if not self._bm25_class or not self._corpus:
            self._bm25 = None
            self._tokenized_cache = []
            return

        # Build tokenized corpus in deterministic order
        self._tokenized_cache = [doc["tokens"] for doc in self._corpus.values()]
        self._bm25 = self._bm25_class(self._tokenized_cache)

    def upsert(self, chunk_id: str, text: str, metadata: dict):
        """Add or update a document in the BM25 index.

        O(1) dict insertion.
        """
        tokens = _tokenize(text)
        self._corpus[chunk_id] = {
            "id": chunk_id,
            "tokens": tokens,
            "text": text,
            "metadata": metadata,
        }

    def delete_by_file(self, file_path: str):
        """Remove all documents from a file (for incremental indexing).

        Call build() afterwards to refresh the BM25 index.
        """
        stale = [cid for cid, doc in self._corpus.items()
                 if doc.get("metadata", {}).get("file_path") == file_path]
        for cid in stale:
            del self._corpus[cid]

    def build(self):
        """Rebuild the BM25 index and persist to disk. Call after all upserts."""
        self._rebuild_index()
        self._save_to_disk()

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Search for documents matching the query keywords.

        Parameters
        ----------
        query:
            Natural language or code query.
        k:
            Number of results to return.

        Returns
        -------
        List of result dicts with metadata + 'text' + 'score'.
        """
        if not self._bm25 or not self._corpus:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)

        # Sort by score descending — scores align with self._tokenized_cache order
        corpus_values = list(self._corpus.values())
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results = []
        for idx, score in ranked[:k]:
            if score <= 0:
                break
            doc = corpus_values[idx]
            result = dict(doc.get("metadata", {}))
            result["text"] = doc.get("text") or doc.get("metadata", {}).get("text", "")
            result["score"] = float(score)
            results.append(result)

        return results

    def clear(self):
        """Delete all documents from the index."""
        self._corpus = {}
        self._bm25 = None
        self._tokenized_cache = []
        self._save_to_disk()

    def count(self) -> int:
        """Return the number of indexed documents."""
        return len(self._corpus)
