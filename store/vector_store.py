"""
ChromaDB vector store with deterministic IDs.

Features:
- Deterministic IDs: uses chunk.node_id instead of UUIDs, so re-indexing
  the same chunk REPLACES it instead of creating duplicates.
- upsert (not add): uses ChromaDB's upsert semantics.
- count, clear, and stats methods.
"""
from __future__ import annotations

import logging
import os
from typing import Any, cast

import chromadb
from chromadb.config import Settings

from kitsune.models.chunk import Chunk

logger = logging.getLogger(__name__)

COLLECTION_NAME = "kitsune_chunks"


class VectorStore:
    """Wraps ChromaDB for storing and searching chunk embeddings.

    Key improvement: uses deterministic IDs (chunk.node_id) so re-indexing
    a repo replaces stale chunks instead of creating duplicates (S3).
    """

    def __init__(self, path: str | None = None):
        chroma_path = path or os.getenv("CHROMA_PATH", "./data/kitsune/chroma")
        os.makedirs(chroma_path, exist_ok=True)

        self._client = chromadb.PersistentClient(
            path=chroma_path,
            settings=Settings(anonymized_telemetry=False),
        )
        logger.info(f"ChromaDB connected at: {chroma_path}")

        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"Collection '{COLLECTION_NAME}' ready ({self._collection.count()} items)")

    def upsert_batch(self, chunks: list[Chunk], vectors: list[list[float]]):
        """Store or update chunks + their vectors.

        Uses deterministic IDs (chunk.node_id) so re-indexing replaces
        stale chunks instead of creating duplicates.

        Parameters
        ----------
        chunks:
            Chunk objects from the parser.
        vectors:
            Embedding vectors (same order as chunks).
        """
        if not chunks:
            return

        ids = [c.node_id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [c.to_metadata() for c in chunks]

        self._collection.upsert(
            ids=ids,
            embeddings=cast(Any, vectors),
            documents=documents,
            metadatas=cast(Any, metadatas),
        )
        logger.info(f"Upserted {len(chunks)} chunks. Total in DB: {self._collection.count()}")

    def search(self, query_vector: list[float], k: int = 5) -> list[dict]:
        """Find the k most similar chunks to the query vector.

        Returns
        -------
        List of result dicts — each contains chunk metadata + 'text' + 'score'.
        Score is cosine similarity (higher = more similar).
        """
        if self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_embeddings=[query_vector],
            n_results=min(k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results["distances"] else []

        output = []
        for doc, meta, dist in zip(docs, metas, distances):
            score = round(1.0 - dist, 4)  # distance → similarity
            output.append({**meta, "text": doc, "score": score})

        return output

    def get_by_name(self, name: str) -> list[dict]:
        """Fetch chunks by exact function/class name using metadata filtering."""
        results = self._collection.get(
            where={"name": name},
            include=["documents", "metadatas"]
        )

        output = []
        if not results["documents"] or not results["metadatas"]:
            return output

        docs = results["documents"]
        metas = results["metadatas"]

        for doc, meta in zip(docs, metas):
            if doc and meta:
                output.append({**meta, "text": doc})

        return output

    def get_by_id(self, node_id: str) -> dict | None:
        """Fetch a single chunk by its node ID."""
        results = self._collection.get(
            ids=[node_id],
            include=["documents", "metadatas"]
        )
        if not results["documents"]:
            return None
        return {**results["metadatas"][0], "text": results["documents"][0]}

    def get_by_file(self, file_path: str) -> list[dict]:
        """Fetch all chunks from a specific file."""
        results = self._collection.get(
            where={"file_path": file_path},
            include=["documents", "metadatas"]
        )
        output = []
        if not results["documents"]:
            return output
        for doc, meta in zip(results["documents"], results["metadatas"]):
            if doc and meta:
                output.append({**meta, "text": doc})
        return output

    def delete_by_file(self, file_path: str):
        """Delete all chunks from a specific file (for incremental indexing)."""
        self._collection.delete(where={"file_path": file_path})

    def get_hashes(self) -> dict[str, str]:
        """Return a mapping of node_id → content hash for all stored chunks.

        Used by incremental indexing to detect which chunks have changed.
        """
        results = self._collection.get(
            include=["metadatas"]
        )
        hashes = {}
        if not results["metadatas"]:
            return hashes
        for chunk_id, meta in zip(results["ids"], results["metadatas"]):
            hashes[chunk_id] = meta.get("hash", "")
        return hashes

    def count(self) -> int:
        """Return the total number of stored chunks."""
        return self._collection.count()

    def get_all(self) -> list[dict]:
        """Return all stored chunks as a list of dicts (text + metadata).

        Used by concept extraction to avoid re-parsing the repo.
        """
        if self._collection.count() == 0:
            return []

        results = self._collection.get(include=["documents", "metadatas"])
        output = []
        for doc, meta in zip(results["documents"], results["metadatas"]):
            if doc and meta:
                output.append({**meta, "text": doc})
        return output

    def clear(self):
        """Delete all chunks from the collection."""
        self._collection.delete(where={"file_path": {"$ne": "__nonexistent__"}})
        # If the above didn't work (some ChromaDB versions), delete all by IDs
        all_ids = self._collection.get(include=[]).get("ids", [])
        if all_ids:
            self._collection.delete(ids=all_ids)
        logger.info("Vector store cleared.")
