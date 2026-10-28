"""
BM25 keyword index stored in Qdrant, shared by everyone using the same server.

Same interface as store/keyword_store.py (the local file version, still the fallback).
Each chunk is a sparse vector of its terms: Kitsune sends the BM25 term-frequency
part and Qdrant applies the IDF part itself (sparse vectors with the IDF modifier),
so scores stay correct as the collection grows, without rebuilding anything.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter

from kitsune.store.keyword_store import _tokenize

logger = logging.getLogger(__name__)

K1, B = 1.5, 0.75   # same constants as rank_bm25 (the local fallback)
AVG_DOC_LEN = 150      # typical chunk length in tokens; the BM25 length normalisation uses it
_VECTOR = "bm25"
_BATCH = 256


def _term_id(term: str) -> int:
    return int.from_bytes(hashlib.md5(term.encode("utf-8")).digest()[:4], "little")


def _doc_vector(tokens: list[str]):
    from qdrant_client import models
    counts = Counter(tokens)
    norm = K1 * (1 - B + B * len(tokens) / AVG_DOC_LEN)
    weights: dict[int, float] = {}
    for term, tf in counts.items():
        weights[_term_id(term)] = weights.get(_term_id(term), 0.0) + tf * (K1 + 1) / (tf + norm)
    return models.SparseVector(indices=list(weights), values=list(weights.values()))


class QdrantKeywordStore:
    """BM25 search over a Qdrant collection named `<vector collection>_bm25`."""

    def __init__(self, client, collection_name: str):
        self._client = client
        self._collection = collection_name
        self._pending: dict[str, object] = {}
        self._ensure_collection()

    def _ensure_collection(self):
        from qdrant_client import models
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config={},
                sparse_vectors_config={_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            self._client.create_payload_index(self._collection, "file_path",
                                              field_schema=models.PayloadSchemaType.KEYWORD)

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))

    def upsert(self, chunk_id: str, text: str, metadata: dict):
        from qdrant_client import models
        tokens = _tokenize(text)
        if not tokens:
            return
        payload = {k: v for k, v in metadata.items() if isinstance(v, (str, int, float, bool))}
        payload.update({"chunk_id": chunk_id, "text": text})
        self._pending[chunk_id] = models.PointStruct(
            id=self._point_id(chunk_id), vector={_VECTOR: _doc_vector(tokens)}, payload=payload)
        if len(self._pending) >= _BATCH:
            self._flush()

    def _flush(self):
        if self._pending:
            self._client.upsert(self._collection, points=list(self._pending.values()))
            self._pending = {}

    def delete_by_file(self, file_path: str):
        from qdrant_client import models
        self._flush()
        self._client.delete(self._collection, points_selector=models.FilterSelector(filter=models.Filter(
            must=[models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path))])))

    def build(self):
        """Send buffered chunks. (Qdrant keeps IDF up to date itself; nothing to rebuild.)"""
        self._flush()

    def search(self, query: str, k: int = 5) -> list[dict]:
        from qdrant_client import models
        terms = sorted(set(_tokenize(query)))
        if not terms:
            return []
        self._flush()
        ids = sorted({_term_id(t) for t in terms})
        hits = self._client.query_points(
            self._collection, query=models.SparseVector(indices=ids, values=[1.0] * len(ids)),
            using=_VECTOR, limit=k, with_payload=True).points
        results = []
        for h in hits:
            if h.score <= 0:
                continue
            r = dict(h.payload or {})
            r["score"] = float(h.score)
            results.append(r)
        return results

    def clear(self):
        self._pending = {}
        if self._client.collection_exists(self._collection):
            self._client.delete_collection(self._collection)
        self._ensure_collection()

    def count(self) -> int:
        self._flush()
        return self._client.count(self._collection, exact=True).count
