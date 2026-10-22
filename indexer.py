"""
Indexer — orchestrates the full K1+K2+K3 pipeline with incremental indexing.

Features:
- Incremental indexing: only re-indexes files that changed since last run.
- Extracts calls/inheritance/imports/decorators DURING K1: no re-parsing.
- Stores richer graph edges: CALLS, EXTENDS, IMPORTS, DECORATED_BY.
- Populates the BM25 keyword index.
- Collects and reports indexing metrics.
- Persists the NetworkX graph to disk.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from kitsune.embedder.contextualizer import contextualize_chunk
from kitsune.embedder.embedder import embed_batch
from kitsune.graph.resolver import CallResolver
from kitsune.models.chunk import Chunk
from kitsune.models.metrics import IndexMetrics, Timer
from kitsune.parser.chunker import chunk_file
from kitsune.parser.treesitter_parser import SUPPORTED_EXTENSIONS, get_language_name
from kitsune.store.graph_store import (
    EDGE_DECORATED_BY,
    EDGE_EXTENDS,
    EDGE_IMPORTS,
    GraphStore,
)
from kitsune.store.keyword_store import KeywordStore
from kitsune.store.vector_store import VectorStore

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
        hash_dir: str | Path | None = None,
    ):
        self._vector = vector_store or VectorStore()
        self._graph = graph_store or GraphStore()
        self._keyword = keyword_store or KeywordStore()
        # Where incremental-indexing file hashes are saved. None = inside the
        # indexed repo (.kitsune_hashes.json), the original location.
        self._hash_dir = Path(hash_dir) if hash_dir is not None else None

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

        # ── K1: Parse → Chunks (with calls/inheritance/imports extracted) ──
        chunks = chunk_file(path)
        if not chunks:
            logger.warning(f"No chunks found in {path}")
            metrics = IndexMetrics()
            metrics.language_distribution[get_language_name(path.suffix)] = 1
            return 0, metrics

        logger.info(f"  {len(chunks)} chunks from K1 parser ({path.name})")

        # ── K2 + K3: replace this file's old data in every store ──────────
        metrics = self._store_chunks(chunks, incremental=True)
        self._keyword.build()
        metrics.total_edges += self._link_calls()
        self._graph.save()

        logger.info(f"  Embedded {len(chunks)} vectors, added {metrics.total_edges} graph edges")
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
            if existing_hashes and self._vector.count() == 0:
                # Hash file says "indexed" but the store is empty (new data_dir,
                # cleared store, moved ChromaDB path) — trusting it would skip everything.
                logger.info("Vector store is empty — ignoring saved file hashes, full re-index.")
                existing_hashes = {}
            logger.info(f"Incremental mode: {len(existing_hashes)} files previously indexed.")

        with Timer(total_metrics, "indexing_time_seconds"):
            files_to_index: list[Path] = []
            skipped = 0
            current_hashes: dict[str, str] = {}   # rel_path → hash, every file on disk
            indexed_hashes: dict[str, str] = {}   # rel_path → hash, files safely in the index

            for path in self._iter_source_files(dir_path, extensions):
                rel_path = str(path.relative_to(dir_path))
                file_hash = self._compute_file_hash(path)
                current_hashes[rel_path] = file_hash

                # Incremental: check if file changed (S7)
                if incremental and existing_hashes.get(rel_path) == file_hash:
                    indexed_hashes[rel_path] = file_hash
                    skipped += 1
                    continue

                files_to_index.append(path)

            # Files indexed last run but gone now — drop them from every store
            for rel_path in set(existing_hashes) - set(current_hashes):
                logger.info(f"Removing deleted file from index: {rel_path}")
                self._remove_file(str(dir_path / rel_path))

            total_metrics.skipped_files = skipped
            logger.info(f"Indexing {len(files_to_index)} files ({skipped} unchanged, skipped).")

            # ── Parallel parsing (CPU-bound, GIL-released by tree-sitter) ──
            # Parse all files in parallel, then store sequentially.
            # Parsing is the slowest step and is safe to parallelize.
            all_parsed_chunks: list[tuple[Path, list[Chunk]]] = []
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
                        if chunks is None:
                            total_metrics.errors.append(f"{path}: parse failed")
                            continue
                        all_parsed_chunks.append((path, chunks))
                    except Exception as e:
                        logger.error(f"Failed to parse {path}: {e}")
                        total_metrics.errors.append(f"{path}: {e}")

            logger.info(f"Parsed {len(all_parsed_chunks)} files in parallel "
                        f"({max_workers} workers). Storing...")

            # ── Sequential storage (thread-safe store writes) ─────────────
            for path, chunks in all_parsed_chunks:
                try:
                    if chunks:
                        file_metrics = self._store_chunks(chunks, incremental)
                        self._merge_metrics(total_metrics, file_metrics)
                    else:
                        # File now has nothing to index — clear what it used to have
                        self._remove_file(str(path))
                    rel_path = str(path.relative_to(dir_path))
                    indexed_hashes[rel_path] = current_hashes[rel_path]
                except Exception as e:
                    logger.error(f"Failed to store chunks from {path}: {e}")
                    total_metrics.errors.append(f"{path}: {e}")

            # Build the BM25 index after all upserts
            self._keyword.build()

            # Resolve every call site against the whole (updated) codebase
            total_metrics.total_edges += self._link_calls()

            # Persist the graph to disk (S4)
            self._graph.save()

            # Save file hashes for next incremental run. Files that failed are
            # left out so they are retried next time.
            if incremental:
                self._save_file_hashes(dir_path, indexed_hashes)

        logger.info(f"\n{total_metrics.summary()}")
        return total_metrics

    # ── Incremental indexing helpers (S7) ──────────────────────────────────

    def _iter_source_files(self, dir_path: Path, extensions: tuple[str, ...]):
        """Yield indexable source files, skipping hidden dirs, venvs and caches."""
        for path in dir_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in extensions:
                continue
            if any(part in _SKIP_DIRS or part.startswith(".")
                   for part in path.relative_to(dir_path).parts[:-1]):
                continue
            yield path

    def _parse_file_safe(self, path: Path) -> list[Chunk] | None:
        """Parse a file into chunks. Safe to call in parallel (read-only).

        Returns None if parsing failed (as opposed to [] for a file with no chunks).
        """
        try:
            return chunk_file(path)
        except Exception as e:
            logger.error(f"Parse failed for {path}: {e}")
            return None

    def _link_calls(self) -> int:
        """(Re)build all CALLS edges with the call resolver. Returns the edge count.

        Runs over the whole project, not just changed files: a new or renamed
        definition can change what calls in *unchanged* files resolve to.
        """
        resolution = CallResolver(self._graph.all_definitions()).resolve_all()
        return self._graph.replace_call_edges(resolution.edges, resolution.externals)

    def _remove_file(self, file_path: str, keep_ids: set[str] | None = None):
        """Remove a file's old data from the vector, keyword, and graph stores."""
        self._vector.delete_by_file(file_path)
        self._keyword.delete_by_file(file_path)
        self._graph.remove_file(file_path, keep_ids=keep_ids)

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
            # Delete this file's old chunks from every store first, so renamed
            # or removed functions don't linger. Graph nodes that still exist
            # are kept so edges from other files' callers survive.
            self._remove_file(path, keep_ids={c.node_id for c in chunks})

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
                # Module chunks get a real node too (so their import edges
                # carry a file_path and are cleaned up on re-index)
                self._graph.upsert_chunk_node(chunk)

                if chunk.name == "<module>":
                    for imp in chunk.imports:
                        self._graph.add_edge(chunk.node_id, imp, edge_type=EDGE_IMPORTS)
                        graph_edges += 1
                    continue

                # CALLS edges are added by _link_calls() once every file is
                # stored, so a call can be resolved to the right definition.

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

    def _hash_file(self, dir_path: Path) -> Path:
        """Path of the saved file-hash record for the repo at dir_path."""
        if self._hash_dir is None:
            return dir_path / ".kitsune_hashes.json"
        repo_key = hashlib.sha256(str(dir_path.resolve()).encode("utf-8")).hexdigest()[:12]
        return self._hash_dir / f"file_hashes_{repo_key}.json"

    def _load_file_hashes(self, dir_path: Path) -> dict[str, str]:
        """Load previously saved file hashes from disk."""
        hash_file = self._hash_file(dir_path)
        if not hash_file.exists():
            return {}
        try:
            import json
            return json.loads(hash_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_file_hashes(self, dir_path: Path, hashes: dict[str, str]):
        """Save the hashes of successfully indexed files for next incremental run."""
        import json
        hash_file = self._hash_file(dir_path)
        hash_file.parent.mkdir(parents=True, exist_ok=True)
        hash_file.write_text(json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8")

    def _merge_metrics(self, total: IndexMetrics, file: IndexMetrics):
        """Merge file-level metrics into the total."""
        total.total_files += file.total_files
        total.total_chunks += file.total_chunks
        total.total_edges += file.total_edges
        # indexing_time_seconds is not summed: the total is already measured
        # by the Timer around the whole run, so adding per-file time double-counts
        for lang, count in file.language_distribution.items():
            total.language_distribution[lang] = (
                total.language_distribution.get(lang, 0) + count
            )
        for ct, count in file.chunk_type_distribution.items():
            total.chunk_type_distribution[ct] = (
                total.chunk_type_distribution.get(ct, 0) + count
            )
