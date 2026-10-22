"""
Chunk — the atomic unit Kitsune works with.

A Chunk is a single, self-contained piece of code extracted from a source file
at an AST boundary (function, class, method, or module-level block).

Fields:
- `calls` field: function calls are extracted DURING chunking (K1), not re-parsed
  later in K3. This eliminates N re-parses per file.
- `hash` field: content hash for deduplication and incremental indexing.
- `concept` field: optional link to a business concept (e.g. "Payment").
- `embedding` field: cached embedding vector (avoids re-embedding unchanged chunks).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class Chunk:
    """A single extracted code unit with full context metadata.

    Attributes
    ----------
    text:
        The raw source code of this chunk — the function body, class
        body, or module-level block as it appears in the file.
    chunk_type:
        One of "function", "class", "method", or "module".
    name:
        The identifier name (e.g. "process_payment", "PaymentService").
        Empty string for anonymous module-level blocks.
    file_path:
        Path to the source file this chunk came from.
    start_line:
        1-indexed line number where this chunk starts in the source file.
    end_line:
        1-indexed line number where this chunk ends (inclusive).
    scope:
        The dotted path of parent scopes, e.g. "PaymentService.process"
        for a method. Empty string for top-level constructs.
    docstring:
        The docstring of this function or class, if present.
    language:
        Source language (e.g. "python", "javascript").
    calls:
        Set of function names called within this chunk. Extracted during
        K1 parsing — no re-parsing needed in K3.
    inherits:
        List of parent class names (for classes). Empty for non-class chunks.
    imports:
        List of module names imported within this chunk.
    decorators:
        List of decorator names applied to this chunk.
    hash:
        SHA-256 hash of the chunk text. Used for deduplication and
        incremental indexing (skip re-embedding if hash matches).
    concept:
        Optional concept name this chunk belongs to (e.g. "Payment").
        Populated by the concept extraction phase.
    """

    text: str
    chunk_type: str          # "function" | "class" | "method" | "module"
    name: str
    file_path: str
    start_line: int
    end_line: int
    scope: str = ""
    docstring: str = ""
    language: str = "python"
    calls: set[str] = field(default_factory=set)
    inherits: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    hash: str = ""
    concept: str = ""
    id_suffix: str = ""      # disambiguates repeated names in one file (overloads, setters)

    def __post_init__(self):
        if not self.hash:
            self.hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    @property
    def qualified_name(self) -> str:
        """Fully qualified name: 'PaymentService.process_payment'."""
        if self.scope and self.name:
            return f"{self.scope}.{self.name}"
        return self.name or "<module>"

    @property
    def node_id(self) -> str:
        """Stable unique ID for graph nodes: 'file_path::qualified_name'."""
        return f"{self.file_path}::{self.qualified_name}{self.id_suffix}"

    @property
    def loc(self) -> int:
        """Lines of code in this chunk."""
        return self.end_line - self.start_line + 1

    def to_metadata(self) -> dict:
        """Convert to a metadata dict for vector store storage."""
        return {
            "chunk_type": self.chunk_type,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "scope": self.scope,
            "docstring": self.docstring[:500],
            "language": self.language,
            "calls": list(self.calls),
            "inherits": self.inherits,
            "imports": self.imports,
            "decorators": self.decorators,
            "hash": self.hash,
            "concept": self.concept,
            "loc": self.loc,
        }

    def __repr__(self) -> str:
        loc = f"{self.file_path}:{self.start_line}-{self.end_line}"
        return f"Chunk({self.chunk_type} '{self.qualified_name}' @ {loc})"
