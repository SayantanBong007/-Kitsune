"""
Concept store — persists and queries business concepts.

Stores concepts as JSON on disk, with an in-memory index for fast lookups.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from kitsune.models.concept import Concept

logger = logging.getLogger(__name__)


class ConceptStore:
    """Persists and queries business concepts.

    Concepts are stored as JSON on disk and loaded into memory for fast access.
    """

    def __init__(self, persist_path: str | None = None):
        self._persist_path = persist_path or os.path.join(
            os.getcwd(), "data", "kitsune", "concepts.json"
        )
        self._concepts: dict[str, Concept] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        """Load concepts from disk if the file exists."""
        path = Path(self._persist_path)
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for concept_data in data.get("concepts", []):
                concept = Concept(
                    name=concept_data["name"],
                    description=concept_data.get("description", ""),
                    chunk_ids=set(concept_data.get("chunk_ids", [])),
                    keywords=set(concept_data.get("keywords", [])),
                    depends_on=set(concept_data.get("depends_on", [])),
                    used_by=set(concept_data.get("used_by", [])),
                    risk_level=concept_data.get("risk_level", "low"),
                )
                self._concepts[concept.name] = concept
            logger.info(f"Loaded {len(self._concepts)} concepts from disk.")
        except Exception as e:
            logger.warning(f"Could not load concepts from disk: {e}")

    def save(self, concepts: list[Concept]):
        """Save concepts to disk."""
        self._concepts = {c.name: c for c in concepts}

        path = Path(self._persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "concepts": [
                {
                    "name": c.name,
                    "description": c.description,
                    "chunk_ids": list(c.chunk_ids),
                    "keywords": list(c.keywords),
                    "depends_on": list(c.depends_on),
                    "used_by": list(c.used_by),
                    "risk_level": c.risk_level,
                }
                for c in concepts
            ]
        }
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        logger.info(f"Saved {len(concepts)} concepts to disk.")

    def get(self, name: str) -> Concept | None:
        """Fetch a concept by name."""
        return self._concepts.get(name)

    def get_all(self) -> list[Concept]:
        """Return all concepts, sorted by chunk count (descending)."""
        return sorted(self._concepts.values(), key=lambda c: c.chunk_count, reverse=True)

    def get_by_chunk(self, chunk_id: str) -> Concept | None:
        """Find which concept a chunk belongs to."""
        for concept in self._concepts.values():
            if chunk_id in concept.chunk_ids:
                return concept
        return None

    def get_high_risk_concepts(self) -> list[Concept]:
        """Return all critical or high-risk concepts."""
        return [
            c for c in self._concepts.values()
            if c.risk_level in ("critical", "high")
        ]

    def get_concept_dependencies(self, name: str) -> dict:
        """Return a concept's dependency tree."""
        concept = self.get(name)
        if not concept:
            return {}

        return {
            "name": concept.name,
            "depends_on": list(concept.depends_on),
            "used_by": list(concept.used_by),
            "risk_level": concept.risk_level,
            "chunk_count": concept.chunk_count,
        }

    def clear(self):
        """Delete all concepts."""
        self._concepts.clear()
        path = Path(self._persist_path)
        if path.exists():
            path.unlink()

    def count(self) -> int:
        """Return the number of stored concepts."""
        return len(self._concepts)
