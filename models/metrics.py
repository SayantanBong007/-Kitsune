"""
Metrics — indexing and query telemetry.

Tracks performance and quality metrics during indexing and querying.
Enables observability into Kitsune's behavior (suggestion S14).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IndexMetrics:
    """Metrics collected during repository indexing."""

    total_files: int = 0
    skipped_files: int = 0
    total_chunks: int = 0
    total_edges: int = 0
    total_concepts: int = 0
    indexing_time_seconds: float = 0.0
    language_distribution: dict[str, int] = field(default_factory=dict)
    chunk_type_distribution: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def avg_chunks_per_file(self) -> float:
        if self.total_files == 0:
            return 0.0
        return self.total_chunks / self.total_files

    @property
    def chunks_per_second(self) -> float:
        if self.indexing_time_seconds == 0:
            return 0.0
        return self.total_chunks / self.indexing_time_seconds

    def record_language(self, language: str):
        self.language_distribution[language] = (
            self.language_distribution.get(language, 0) + 1
        )

    def record_chunk_type(self, chunk_type: str):
        self.chunk_type_distribution[chunk_type] = (
            self.chunk_type_distribution.get(chunk_type, 0) + 1
        )

    def summary(self) -> str:
        lines = [
            "Kitsune Indexing Summary",
            "=" * 40,
            f"  Files indexed:    {self.total_files}",
            f"  Files skipped:    {self.skipped_files}",
            f"  Total chunks:     {self.total_chunks}",
            f"  Graph edges:      {self.total_edges}",
            f"  Concepts:         {self.total_concepts}",
            f"  Indexing time:    {self.indexing_time_seconds:.2f}s",
            f"  Chunks/sec:       {self.chunks_per_second:.1f}",
            f"  Avg chunks/file:  {self.avg_chunks_per_file:.1f}",
            "",
            "  Language distribution:",
        ]
        for lang, count in sorted(self.language_distribution.items(),
                                   key=lambda x: -x[1]):
            lines.append(f"    {lang}: {count}")
        lines.append("")
        lines.append("  Chunk type distribution:")
        for ct, count in sorted(self.chunk_type_distribution.items(),
                                 key=lambda x: -x[1]):
            lines.append(f"    {ct}: {count}")
        if self.errors:
            lines.append("")
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors[:5]:
                lines.append(f"    - {err}")
        return "\n".join(lines)


@dataclass
class QueryMetrics:
    """Metrics collected during a single query."""

    query: str = ""
    query_type: str = ""           # "semantic", "graph", "hybrid", "lookup"
    retrieval_time_seconds: float = 0.0
    synthesis_time_seconds: float = 0.0
    primary_chunks_found: int = 0
    dependency_chunks_found: int = 0
    total_context_chars: int = 0
    graph_depth_reached: int = 0

    @property
    def total_time(self) -> float:
        return self.retrieval_time_seconds + self.synthesis_time_seconds

    def summary(self) -> str:
        return (
            f"Query: '{self.query[:50]}...' | "
            f"Type: {self.query_type} | "
            f"Retrieval: {self.retrieval_time_seconds:.3f}s | "
            f"Primary: {self.primary_chunks_found} | "
            f"Deps: {self.dependency_chunks_found} | "
            f"Depth: {self.graph_depth_reached} | "
            f"Context: {self.total_context_chars} chars"
        )


class Timer:
    """Context manager for timing code blocks."""

    def __init__(self, metrics_obj: Any, attr: str):
        self.metrics_obj = metrics_obj
        self.attr = attr
        self.start = 0.0

    def __enter__(self):
        self.start = time.time()
        return self

    def __exit__(self, *args):
        elapsed = time.time() - self.start
        current = getattr(self.metrics_obj, self.attr, 0.0)
        setattr(self.metrics_obj, self.attr, current + elapsed)
