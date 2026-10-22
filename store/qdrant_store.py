"""
Qdrant vector store — server-based, horizontally scalable.

Replaces ChromaDB for large-scale deployments. Qdrant runs as a server
(can be local Docker or Qdrant Cloud), enabling:
- Horizontal scaling across multiple machines
- Concurrent read/write access from multiple ArchFox instances
- Built-in payload filtering (metadata-based queries)
- HNSW index with cosine similarity

This is a drop-in replacement for VectorStore (ChromaDB). The interface
is identical — only the backend changes.

Connection modes:
1. Qdrant Cloud:  QDRANT_URL=https://xyz.qdrant.io  QDRANT_API_KEY=...
2. Local Docker:  QDRANT_URL=http://localhost:6333  (no API key needed)
3. Fallback:      If Qdrant is unavailable, falls back to ChromaDB automatically.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from kitsune.models.chunk import Chunk

logger = logging.getLogger(__name__)

COLLECTION_NAME = "kitsune_chunks"
VECTOR_SIZE = 384  # bge-small-en-v1.5 dimension


def _point_id(node_id: str) -> str:
    """Map a chunk node_id ('file::name') to a deterministic Qdrant point ID.

    Qdrant only accepts unsigned integers or UUIDs as point IDs, so the
    node_id is hashed into a UUIDv5. The original node_id is kept in the payload.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, node_id))


class QdrantVectorStore:
    """Qdrant-backed vector store with automatic ChromaDB fallback.

    This is a drop-in replacement for the ChromaDB VectorStore. If Qdrant
    is unavailable, it transparently falls back to ChromaDB so the system
    never fails.

    Environment variables:
        QDRANT_URL:     Qdrant server URL (e.g. https://xyz.qdrant.io)
        QDRANT_API_KEY: API key for Qdrant Cloud (optional for local)
        QDRANT_LOCAL_PATH: If set, use embedded Qdrant (no server needed)
    """

    def __init__(self, url: str | None = None, api_key: str | None = None,
                 collection_name: str = COLLECTION_NAME, chroma_path: str | None = None):
        self._collection_name = collection_name
        self._chroma_path = chroma_path  # used only by the ChromaDB fallback
        self._client = None
        self._fallback = None  # ChromaDB fallback
        self._use_fallback = False

        qdrant_url = url or os.getenv("QDRANT_URL")
        qdrant_key = api_key or os.getenv("QDRANT_API_KEY")
        qdrant_local = os.getenv("QDRANT_LOCAL_PATH")

        # Try Qdrant first
        if qdrant_url or qdrant_local:
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.models import Distance, VectorParams

                if qdrant_local:
                    # Embedded mode (no server, like ChromaDB but Qdrant engine)
                    self._client = QdrantClient(path=qdrant_local)
                    logger.info(f"Qdrant embedded mode at: {qdrant_local}")
                else:
                    # Server mode (local Docker or Qdrant Cloud)
                    self._client = QdrantClient(
                        url=qdrant_url,
                        api_key=qdrant_key,
                        timeout=30,
                    )
                    logger.info(f"Qdrant connected at: {qdrant_url}")

                # Create collection if it doesn't exist
                collections = self._client.get_collections().collections
                exists = any(c.name == self._collection_name for c in collections)
                if not exists:
                    self._client.create_collection(
                        collection_name=self._collection_name,
                        vectors_config=VectorParams(
                            size=VECTOR_SIZE,
                            distance=Distance.COSINE,
                        ),
                    )
                    logger.info(f"Created Qdrant collection '{self._collection_name}'")
                else:
                    logger.info(f"Qdrant collection '{self._collection_name}' ready")

            except Exception as e:
                logger.warning(f"Qdrant connection failed: {e}")
                self._client = None
                self._use_fallback = True
        else:
            # No Qdrant configured — use ChromaDB fallback
            self._use_fallback = True

        if self._use_fallback:
            logger.info("Falling back to ChromaDB vector store.")
            from kitsune.store.vector_store import VectorStore
            self._fallback = VectorStore(path=self._chroma_path)

    @property
    def using_fallback(self) -> bool:
        """True if using ChromaDB fallback instead of Qdrant."""
        return self._use_fallback

    def upsert_batch(self, chunks: list[Chunk], vectors: list[list[float]]):
        """Store or update chunks + their vectors.

        Uses deterministic IDs (chunk.node_id) so re-indexing replaces
        stale chunks instead of creating duplicates.
        """
        if not chunks:
            return

        if self._use_fallback:
            self._fallback.upsert_batch(chunks, vectors)
            return

        from qdrant_client.models import PointStruct

        points = []
        for chunk, vector in zip(chunks, vectors):
            points.append(PointStruct(
                id=_point_id(chunk.node_id),
                vector=vector,
                payload={
                    "text": chunk.text,
                    "node_id": chunk.node_id,
                    **chunk.to_metadata(),
                },
            ))

        self._client.upsert(
            collection_name=self._collection_name,
            points=points,
        )
        logger.info(f"Upserted {len(chunks)} chunks to Qdrant.")

    def search(self, query_vector: list[float], k: int = 5) -> list[dict]:
        """Find the k most similar chunks to the query vector.

        Returns
        -------
        List of result dicts — each contains chunk metadata + 'text' + 'score'.
        Score is cosine similarity (higher = more similar).
        """
        if self._use_fallback:
            return self._fallback.search(query_vector, k)

        # qdrant-client >= 1.10 uses query_points; `search` was removed in later versions
        if hasattr(self._client, "query_points"):
            results = self._client.query_points(
                collection_name=self._collection_name,
                query=query_vector,
                limit=k,
                with_payload=True,
            ).points
        else:
            results = self._client.search(
                collection_name=self._collection_name,
                query_vector=query_vector,
                limit=k,
                with_payload=True,
            )

        output = []
        for hit in results:
            payload = hit.payload or {}
            output.append({
                **payload,
                "text": payload.get("text", ""),
                "score": round(hit.score, 4),
            })

        return output

    def get_by_name(self, name: str) -> list[dict]:
        """Fetch chunks by exact function/class name using payload filtering."""
        if self._use_fallback:
            return self._fallback.get_by_name(name)

        from qdrant_client.models import Filter, FieldCondition, MatchValue

        results = self._client.scroll(
            collection_name=self._collection_name,
            scroll_filter=Filter(
                must=[FieldCondition(key="name", match=MatchValue(value=name))]
            ),
            limit=100,
            with_payload=True,
        )

        output = []
        for point in results[0]:  # scroll returns (points, next_offset)
            payload = point.payload or {}
            output.append({**payload, "text": payload.get("text", "")})

        return output

    def get_by_id(self, node_id: str) -> dict | None:
        """Fetch a single chunk by its node ID."""
        if self._use_fallback:
            return self._fallback.get_by_id(node_id)

        try:
            points = self._client.retrieve(
                collection_name=self._collection_name,
                ids=[_point_id(node_id)],
                with_payload=True,
            )
            if points:
                payload = points[0].payload or {}
                return {**payload, "text": payload.get("text", "")}
        except Exception:
            pass
        return None

    def get_by_file(self, file_path: str) -> list[dict]:
        """Fetch all chunks from a specific file."""
        if self._use_fallback:
            return self._fallback.get_by_file(file_path)

        from qdrant_client.models import Filter, FieldCondition, MatchValue

        results = self._client.scroll(
            collection_name=self._collection_name,
            scroll_filter=Filter(
                must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]
            ),
            limit=1000,
            with_payload=True,
        )

        output = []
        for point in results[0]:
            payload = point.payload or {}
            output.append({**payload, "text": payload.get("text", "")})
        return output

    def delete_by_file(self, file_path: str):
        """Delete all chunks from a specific file (for incremental indexing)."""
        if self._use_fallback:
            self._fallback.delete_by_file(file_path)
            return

        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self._client.delete(
            collection_name=self._collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="file_path", match=MatchValue(value=file_path))]
            ),
        )

    def get_hashes(self) -> dict[str, str]:
        """Return a mapping of node_id → content hash for all stored chunks."""
        if self._use_fallback:
            return self._fallback.get_hashes()

        # Scroll through all points and extract hashes
        hashes = {}
        offset = None
        while True:
            results = self._client.scroll(
                collection_name=self._collection_name,
                limit=1000,
                offset=offset,
                with_payload=True,
            )
            points, next_offset = results
            if not points:
                break
            for point in points:
                payload = point.payload or {}
                hashes[payload.get("node_id", point.id)] = payload.get("hash", "")
            offset = next_offset
            if not next_offset:
                break
        return hashes

    def count(self) -> int:
        """Return the total number of stored chunks."""
        if self._use_fallback:
            return self._fallback.count()

        result = self._client.count(
            collection_name=self._collection_name,
            exact=True,
        )
        return result.count

    def get_all(self) -> list[dict]:
        """Return all stored chunks as a list of dicts (text + metadata).

        Used by concept extraction to avoid re-parsing the repo.
        """
        if self._use_fallback:
            return self._fallback.get_all()

        output = []
        offset = None
        while True:
            results = self._client.scroll(
                collection_name=self._collection_name,
                limit=1000,
                offset=offset,
                with_payload=True,
            )
            points, next_offset = results
            if not points:
                break
            for point in points:
                payload = point.payload or {}
                output.append({**payload, "text": payload.get("text", "")})
            offset = next_offset
            if not next_offset:
                break
        return output

    def clear(self):
        """Delete all chunks from the collection."""
        if self._use_fallback:
            self._fallback.clear()
            return

        # Delete and recreate the collection
        try:
            self._client.delete_collection(self._collection_name)
        except Exception:
            pass

        from qdrant_client.models import Distance, VectorParams
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Qdrant collection cleared and recreated.")

    # ── Compatibility property for engine code that accesses _collection ──

    @property
    def _collection(self):
        """Compatibility shim for code that accesses ChromaDB's _collection.

        When using Qdrant, this raises a clear error directing the caller
        to use the proper methods instead.
        """
        if self._use_fallback:
            return self._fallback._collection
        raise AttributeError(
            "Direct _collection access is not supported with Qdrant. "
            "Use the VectorStore methods (search, get_by_name, etc.) instead."
        )
