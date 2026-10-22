"""
Concept — a business-level entity extracted from the codebase.

While Chunks operate at the code-symbol level (functions, classes), Concepts
operate at the business-domain level (Payment, Authentication, User, Invoice).

A Concept aggregates related chunks and carries structured memory:
- Purpose: what does this concept do?
- Used by: which other concepts depend on it?
- Depends on: which concepts does it depend on?
- Risk: how critical is it? (based on dependent count)

This is the key innovation from the Kitsune VISION.md — retrieval should
operate on business concepts, not just code symbols.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Concept:
    """A business concept extracted from the repository.

    Attributes
    ----------
    name:
        The concept name (e.g. "Payment", "Authentication", "User").
    description:
        LLM-generated or template-generated description of what this concept does.
    chunk_ids:
        Set of chunk node IDs that belong to this concept.
    keywords:
        Keywords associated with this concept (from naming patterns, docstrings).
    depends_on:
        Names of other concepts this concept depends on.
    used_by:
        Names of other concepts that depend on this concept.
    risk_level:
        "critical" | "high" | "medium" | "low" — based on dependent count.
    """

    name: str
    description: str = ""
    chunk_ids: set[str] = field(default_factory=set)
    keywords: set[str] = field(default_factory=set)
    depends_on: set[str] = field(default_factory=set)
    used_by: set[str] = field(default_factory=set)
    risk_level: str = "low"

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_ids)

    def calculate_risk(self):
        """Calculate risk level based on how many concepts depend on this one."""
        dependent_count = len(self.used_by)
        if dependent_count >= 5:
            self.risk_level = "critical"
        elif dependent_count >= 3:
            self.risk_level = "high"
        elif dependent_count >= 1:
            self.risk_level = "medium"
        else:
            self.risk_level = "low"

    def to_metadata(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "chunk_ids": list(self.chunk_ids),
            "keywords": list(self.keywords),
            "depends_on": list(self.depends_on),
            "used_by": list(self.used_by),
            "risk_level": self.risk_level,
            "chunk_count": self.chunk_count,
        }

    def __repr__(self) -> str:
        return f"Concept('{self.name}', chunks={self.chunk_count}, risk={self.risk_level})"
