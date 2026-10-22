"""
Concept extractor — discovers business concepts from the codebase.

This implements the core idea from Kitsune's VISION.md: retrieval should
operate on business concepts (Payment, Authentication, User), not just
code symbols (functions, classes).

Strategy:
1. Collect all chunk names, docstrings, and file paths.
2. Extract candidate concept names using naming patterns and keyword frequency.
3. Cluster chunks by semantic similarity to assign them to concepts.
4. Build concept-to-concept dependency edges.
5. Calculate risk levels based on dependent count.

Features:
- Concepts are first-class entities with structured memory.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Dict, List, Set

from kitsune.models.chunk import Chunk
from kitsune.models.concept import Concept

logger = logging.getLogger(__name__)

# Common suffixes/prefixes to strip when extracting concept names
_CONCEPT_SUFFIXES = re.compile(
    r"(Service|Controller|Manager|Handler|Repository|Factory|Builder|"
    r"Provider|Adapter|Wrapper|Client|Agent|Helper|Util|Utils|Config|"
    r"Model|Schema|View|Router|Middleware|Interceptor|Listener|Observer)$"
)

# Stopwords for keyword extraction
_STOPWORDS = {
    "get", "set", "is", "has", "the", "a", "an", "for", "to", "in", "of",
    "and", "or", "not", "with", "from", "by", "on", "at", "this", "that",
    "self", "cls", "init", "main", "run", "start", "stop", "new", "old",
    "data", "value", "result", "output", "input", "args", "kwargs",
    "true", "false", "none", "null", "return", "yield", "raise", "try",
    "except", "finally", "if", "else", "elif", "while", "for", "def",
    "class", "import", "from", "as", "pass", "break", "continue",
}


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text (names, docstrings, paths)."""
    words = re.findall(r"[A-Z][a-z]+|[a-z]+", text)
    return {
        w.lower() for w in words
        if len(w) > 2 and w.lower() not in _STOPWORDS
    }


def _name_to_concept(name: str) -> str:
    """Convert a class/function name to a concept name.

    Examples:
        PaymentService → Payment
        UserController → User
        AuthenticationManager → Authentication
        process_payment → Payment
        get_user_by_id → User
    """
    # Handle snake_case: process_payment → Payment
    if "_" in name:
        parts = name.split("_")
        # Take the most meaningful part (not get/set/is/has)
        meaningful = [p for p in parts if p.lower() not in _STOPWORDS and len(p) > 2]
        if meaningful:
            return meaningful[0].capitalize()

    # Handle PascalCase: PaymentService → Payment
    concept = _CONCEPT_SUFFIXES.sub("", name)
    # Split CamelCase and take first meaningful part
    parts = re.findall(r"[A-Z][a-z]+", concept)
    if parts:
        meaningful = [p for p in parts if p.lower() not in _STOPWORDS]
        if meaningful:
            return meaningful[0]

    return concept.capitalize() if concept else name.capitalize()


class ConceptExtractor:
    """Extracts business concepts from indexed chunks.

    Usage
    -----
    extractor = ConceptExtractor()
    concepts = extractor.extract(chunks)
    for concept in concepts:
        print(f"{concept.name}: {concept.chunk_count} chunks, risk={concept.risk_level}")
    """

    def extract(self, chunks: list[Chunk]) -> list[Concept]:
        """Extract concepts from a list of chunks.

        Parameters
        ----------
        chunks:
            All chunks from the indexed repository.

        Returns
        -------
        List of Concept objects, sorted by chunk count (descending).
        """
        # Step 1: Assign chunks to concepts based on naming patterns
        concept_chunks: dict[str, set[str]] = defaultdict(set)
        concept_keywords: dict[str, set[str]] = defaultdict(set)

        for chunk in chunks:
            if chunk.name == "<module>" or not chunk.name:
                continue

            concept_name = _name_to_concept(chunk.name)
            concept_chunks[concept_name].add(chunk.node_id)

            # Collect keywords from name, docstring, and file path
            keywords = _extract_keywords(
                f"{chunk.name} {chunk.docstring} {chunk.file_path}"
            )
            concept_keywords[concept_name].update(keywords)

        # Step 2: Create Concept objects
        concepts: list[Concept] = []
        for name, chunk_ids in concept_chunks.items():
            # Only keep concepts with at least 1 chunk
            if not chunk_ids:
                continue

            keywords = concept_keywords[name]
            description = self._build_description(name, keywords)

            concept = Concept(
                name=name,
                description=description,
                chunk_ids=chunk_ids,
                keywords=keywords,
            )
            concepts.append(concept)

        # Step 3: Build concept-to-concept dependencies
        self._build_concept_dependencies(concepts, chunks)

        # Step 4: Calculate risk levels
        for concept in concepts:
            concept.calculate_risk()

        # Sort by chunk count (most chunks first)
        concepts.sort(key=lambda c: c.chunk_count, reverse=True)

        logger.info(f"Extracted {len(concepts)} concepts from {len(chunks)} chunks.")
        return concepts

    def _build_description(self, name: str, keywords: set[str]) -> str:
        """Build a template description for a concept."""
        top_keywords = sorted(keywords)[:5]
        kw_str = ", ".join(top_keywords) if top_keywords else "none"
        return f"The '{name}' concept. Related terms: {kw_str}."

    def _build_concept_dependencies(self, concepts: list[Concept], chunks: list[Chunk]):
        """Build dependency edges between concepts.

        If a chunk in concept A calls a function that belongs to concept B,
        then concept A depends on concept B.
        """
        # Build a map: function name → concept name
        func_to_concept: dict[str, str] = {}
        for concept in concepts:
            for chunk_id in concept.chunk_ids:
                # Extract the function name from the chunk ID
                # Format: "file_path::qualified_name"
                parts = chunk_id.split("::")
                if len(parts) >= 2:
                    # last part of qualified name, minus any "@L<line>" id suffix
                    name = parts[-1].split("@")[0].split(".")[-1]
                    func_to_concept[name] = concept.name

        # For each chunk, check if its calls belong to another concept
        concept_by_name = {c.name: c for c in concepts}
        for chunk in chunks:
            if chunk.name == "<module>":
                continue

            # Find which concept this chunk belongs to
            source_concept_name = None
            for concept in concepts:
                if chunk.node_id in concept.chunk_ids:
                    source_concept_name = concept.name
                    break

            if not source_concept_name:
                continue

            source_concept = concept_by_name[source_concept_name]

            # Check each call
            for callee in chunk.calls:
                target_concept_name = func_to_concept.get(callee)
                if target_concept_name and target_concept_name != source_concept_name:
                    source_concept.depends_on.add(target_concept_name)
                    concept_by_name[target_concept_name].used_by.add(source_concept_name)
