"""
Indexer — orchestrates the full K1+K2+K3 pipeline with incremental indexing.

Improvements over Kitsune 1.0:
- Incremental indexing (S7): only re-indexes files that changed since last run.
- Extracts calls/inheritance/imports/decorators DURING K1 (S1): no re-parsing.
- Stores richer graph edges: CALLS, EXTENDS, IMPORTS, DECORATED_BY (S9).
- Populates the BM25 keyword index (S5).
- Collects and reports indexing metrics (S14).
- Persists the NetworkX graph to disk (S4).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from kitsune_2_0.embedder.contextualizer import contextualize_chunk
from kitsune_2_0.embedder.embedder import embed_batch
from kitsune_2_0.models.chunk import Chunk
from kitsune_2_0.models.metrics import IndexMetrics, Timer
from kitsune_2_0.parser.chunker import chunk_file
from kitsune_2_0.parser.treesitter_parser import SUPPORTED_EXTENSIONS, get_language_name
from kitsune_2_0.store.graph_store import (
    EDGE_CALLS,
    EDGE_DECORATED_BY,
    EDGE_EXTENDS,
    EDGE_IMPORTS,
    GraphStore,
)
from kitsune_2_0.store.keyword_store import KeywordStore
from kitsune_2_0.store.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Directories to skip during indexing
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox",
              ".eggs", "build", "dist", ".mypy_cache", ".pytest_cache"}


class Indexer:
    """Index source files into the vector store, graph store, and keyword index.

    Usage
    -----
    indexer = Indexer()
    metrics = indexer.index_directory("path/to/repo")
    print(metrics.summary())
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        graph_store: GraphStore | None = None,
        keyword_store: KeywordStore | None = None,
    ):
        self._vector = vector_store or VectorStore()
        self._graph = graph_store or GraphStore()
        self._keyword = keyword_store or KeywordStore()

    def index_file(self, file_path: str | Path) -> tuple[int, IndexMetrics]:
        """Parse, contextualize, embed, and store all chunks from a file.

        Parameters
        ----------
        file_path:
            Path to a source file.

        Returns
        -------
        Tuple of (number of chunks indexed, metrics).
        """
        path = Path(file_path)
        metrics = IndexMetrics()
        metrics.language_distribution[get_language_name(path.suffix)] = 1

        with Timer(metrics, "indexing_time_seconds"):
            # ── K1: Parse → Chunks (with calls/inheritance/imports extracted) ──
            chunks = chunk_file(path)
            if not chunks:
                logger.warning(f"No chunks found in {path}")
                return 0, metrics

            for chunk in chunks:
                metrics.record_chunk_type(chunk.chunk_type)

            logger.info(f"  {len(chunks)} chunks from K1 parser ({path.name})")

            # ── K2: Contextualize → Embed → Store in ChromaDB ─────────────────
            contextualized_texts = [contextualize_chunk(c) for c in chunks]
            vectors = embed_batch(contextualized_texts)
            self._vector.upsert_batch(chunks, vectors)

            # Populate BM25 keyword index (S5)
            for chunk in chunks:
                self._keyword.upsert(chunk.node_id, chunk.text, chunk.to_metadata())

            # ── K3: Build Dependency Graph (richer edges) ─────────────────────
            graph_edges = 0
            for chunk in chunks:
                if chunk.name == "<module>":
                    # Module chunks still get import edges
                    for imp in chunk.imports:
                        self._graph.add_edge(chunk.node_id, imp, edge_type=EDGE_IMPORTS)
                        graph_edges += 1
                    continue

                # Add the chunk node
                self._graph.upsert_chunk_node(chunk)

                # CALLS edges
                for callee in chunk.calls:
                    self._graph.add_edge(chunk.node_id, callee, edge_type=EDGE_CALLS)
                    graph_edges += 1

                # EXTENDS edges (inheritance)
                for parent in chunk.inherits:
                    self._graph.add_edge(chunk.node_id, parent, edge_type=EDGE_EXTENDS)
                    graph_edges += 1

                # DECORATED_BY edges
                for decorator in chunk.decorators:
                    self._graph.add_edge(chunk.node_id, decorator, edge_type=EDGE_DECORATED_BY)
                    graph_edges += 1

            metrics.total_edges += graph_edges
            metrics.total_chunks += len(chunks)
            metrics.total_files += 1

        logger.info(f"  Embedded {len(vectors)} vectors, added {graph_edges} graph edges")
        return len(chunks), metrics

    def index_directory(
        self,
        dir_path: str | Path,
        extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS,
        incremental: bool = True,
    ) -> IndexMetrics:
        """Index all files with the given extensions in a directory.

        Parameters
        ----------
        dir_path:
            Root directory to walk.
        extensions:
            File extensions to index.
        incremental:
            If True (default), only re-index files that changed since last run (S7).
            If False, re-index everything.

        Returns
        -------
        IndexMetrics with full indexing statistics.
        """
        total_metrics = IndexMetrics()
        dir_path = Path(dir_path)

        # Load existing file hashes for incremental indexing
        existing_hashes: dict[str, str] = {}
        if incremental:
            existing_hashes = self._load_file_hashes(dir_path)
            logger.info(f"Incremental mode: {len(existing_hashes)} files previously indexed.")

        with Timer(total_metrics, "indexing_time_seconds"):
            files_to_index: list[Path] = []
            skipped = 0

            for path in dir_path.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in extensions:
                    continue
                # Skip hidden dirs, venvs, cache folders
                if any(part in _SKIP_DIRS or part.startswith(".")
                       for part in path.relative_to(dir_path).parts[:-1]):
                    continue

                # Incremental: check if file changed (S7)
                if incremental:
                    file_hash = self._compute_file_hash(path)
                    rel_path = str(path.relative_to(dir_path))
                    if existing_hashes.get(rel_path) == file_hash:
                        skipped += 1
                        continue

                files_to_index.append(path)

            total_metrics.skipped_files = skipped
            logger.info(f"Indexing {len(files_to_index)} files ({skipped} unchanged, skipped).")

            # ── Parallel parsing (CPU-bound, GIL-released by tree-sitter) ──
            # Parse all files in parallel, then store sequentially.
            # Parsing is the slowest step and is safe to parallelize.
            all_parsed_chunks: list[list[Chunk]] = []
            max_workers = min(os.cpu_count() or 4, 8)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_path = {
                    executor.submit(self._parse_file_safe, path): path
                    for path in files_to_index
                }
                for future in as_completed(future_to_path):
                    path = future_to_path[future]
                    try:
                        chunks = future.result()
                        if chunks:
                            all_parsed_chunks.append(chunks)
                    except Exception as e:
                        logger.error(f"Failed to parse {path}: {e}")
                        total_metrics.errors.append(f"{path}: {e}")

            logger.info(f"Parsed {len(all_parsed_chunks)} files in parallel "
                        f"({max_workers} workers). Storing...")

            # ── Sequential storage (thread-safe store writes) ─────────────
            for chunks in all_parsed_chunks:
                try:
                    file_metrics = self._store_chunks(chunks, incremental)
                    self._merge_metrics(total_metrics, file_metrics)
                except Exception as e:
                    logger.error(f"Failed to store chunks: {e}")
                    total_metrics.errors.append(str(e))

            # Build the BM25 index after all upserts
            self._keyword.build()

            # Persist the graph to disk (S4)
            self._graph.save()

            # Save file hashes for next incremental run
            if incremental:
                self._save_file_hashes(dir_path)

        logger.info(f"\n{total_metrics.summary()}")
        return total_metrics

    # ── Incremental indexing helpers (S7) ──────────────────────────────────

    def _parse_file_safe(self, path: Path) -> list[Chunk]:
        """Parse a file into chunks. Safe to call in parallel (read-only)."""
        try:
            return chunk_file(path)
        except Exception as e:
            logger.error(f"Parse failed for {path}: {e}")
            return []

    def _store_chunks(self, chunks: list[Chunk], incremental: bool) -> IndexMetrics:
        """Store parsed chunks into all backends. Sequential (thread-safe writes).

        This is separated from parsing so parsing can run in parallel while
        storage (which writes to ChromaDB, Neo4j, BM25) runs sequentially.
        """
        metrics = IndexMetrics()
        if not chunks:
            return metrics

        path = chunks[0].file_path
        metrics.language_distribution[get_language_name(Path(path).suffix)] = 1

        with Timer(metrics, "indexing_time_seconds"):
            # For incremental: delete old chunks from this file first
            if incremental:
                try:
                    self._vector.delete_by_file(path)
                except Exception:
                    pass

            for chunk in chunks:
                metrics.record_chunk_type(chunk.chunk_type)

            # K2: Contextualize → Embed → Store
            contextualized_texts = [contextualize_chunk(c) for c in chunks]
            vectors = embed_batch(contextualized_texts)
            self._vector.upsert_batch(chunks, vectors)

            # Populate BM25 keyword index
            for chunk in chunks:
                self._keyword.upsert(chunk.node_id, chunk.text, chunk.to_metadata())

            # K3: Build Dependency Graph
            graph_edges = 0
            for chunk in chunks:
                if chunk.name == "<module>":
                    for imp in chunk.imports:
                        self._graph.add_edge(chunk.node_id, imp, edge_type=EDGE_IMPORTS)
                        graph_edges += 1
                    continue

                self._graph.upsert_chunk_node(chunk)

                for callee in chunk.calls:
                    self._graph.add_edge(chunk.node_id, callee, edge_type=EDGE_CALLS)
                    graph_edges += 1

                for parent in chunk.inherits:
                    self._graph.add_edge(chunk.node_id, parent, edge_type=EDGE_EXTENDS)
                    graph_edges += 1

                for decorator in chunk.decorators:
                    self._graph.add_edge(chunk.node_id, decorator, edge_type=EDGE_DECORATED_BY)
                    graph_edges += 1

            metrics.total_edges += graph_edges
            metrics.total_chunks += len(chunks)
            metrics.total_files += 1

        return metrics

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute a hash of a file's contents for change detection."""
        h = hashlib.sha256()
        try:
            h.update(file_path.read_bytes())
        except Exception:
            return ""
        return h.hexdigest()[:16]

    def _load_file_hashes(self, dir_path: Path) -> dict[str, str]:
        """Load previously saved file hashes from disk."""
        hash_file = dir_path / ".kitsune_hashes.json"
        if not hash_file.exists():
            return {}
        try:
            import json
            return json.loads(hash_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_file_hashes(self, dir_path: Path):
        """Save current file hashes to disk for next incremental run."""
        import json
        hash_file = dir_path / ".kitsune_hashes.json"
        hashes: dict[str, str] = {}
        for path in dir_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in SUPPORTED_EXTENSIONS:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".")
                   for part in path.relative_to(dir_path).parts[:-1]):
                continue
            rel_path = str(path.relative_to(dir_path))
            hashes[rel_path] = self._compute_file_hash(path)
        hash_file.write_text(json.dumps(hashes, indent=2), encoding="utf-8")

    def _merge_metrics(self, total: IndexMetrics, file: IndexMetrics):
        """Merge file-level metrics into the total."""
        total.total_files += file.total_files
        total.total_chunks += file.total_chunks
        total.total_edges += file.total_edges
        total.indexing_time_seconds += file.indexing_time_seconds
        for lang, count in file.language_distribution.items():
            total.language_distribution[lang] = (
                total.language_distribution.get(lang, 0) + count
            )
        for ct, count in file.chunk_type_distribution.items():
            total.chunk_type_distribution[ct] = (
                total.chunk_type_distribution.get(ct, 0) + count
            )
