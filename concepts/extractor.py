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
import os
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


# Folder/file/word parts that describe code structure, not a business domain
_GENERIC = _STOPWORDS | {
    "src", "lib", "libs", "app", "apps", "pkg", "internal", "cmd", "core", "common", "shared",
    "util", "utils", "utility", "utilities", "helper", "helpers", "misc", "base", "abstract",
    "model", "models", "type", "types", "interface", "interfaces", "schema", "schemas",
    "test", "tests", "spec", "specs", "index", "mod", "legacy", "vendor", "dist", "build",
    "scripts", "script", "store", "stores", "service", "services", "controller", "controllers",
    "handler", "handlers", "view", "views", "component", "components", "ui", "page", "pages",
    "public", "static", "assets", "impl", "manager", "managers", "module", "modules", "package",
    "constructor", "default", "global", "globals", "constants", "const", "http", "io",
    "create", "update", "delete", "remove", "add", "build", "make", "load", "save", "fetch",
    "send", "handle", "process", "parse", "render", "check", "validate", "compute", "find",
    "list", "show", "do", "apply", "full", "name", "new", "item", "items", "obj", "object",
    "dict", "str", "int", "bool", "json", "to", "as", "repr", "eq", "hash",
}
_SUFFIXES = re.compile(
    r"(Service|Controller|Manager|Handler|Repository|Repo|Factory|Builder|Provider|Adapter|"
    r"Wrapper|Client|Agent|Helper|Util|Utils|Config|Model|Schema|View|Router|Middleware|"
    r"Interceptor|Listener|Observer|Store|Gateway|Impl|Base|Mixin|Interface)$"
)


def _domain_word(text: str) -> str:
    """First non-generic word of a name, singular and capitalized ("" if none).

    "payments" → Payment, "notesStore" → Note, "send_receipt" → Receipt, "utils" → "".
    """
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", text.replace("-", "_"))
    for word in words:
        w = word.lower()
        if len(w) < 2 or w in _GENERIC or w.isdigit():
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        if w in _GENERIC:
            continue
        return w.capitalize()
    return ""


def _concept_for(chunk: Chunk, root: str) -> str:
    """Concept a chunk belongs to: domain folder → owning class → file → name."""
    rel = os.path.relpath(chunk.file_path, root) if root else chunk.file_path
    parts = rel.replace("\\", "/").split("/")
    for folder in reversed(parts[:-1]):
        word = _domain_word(folder)
        if word:
            return word
    owner = chunk.scope.split(".")[0] if chunk.scope else (
        chunk.name if chunk.chunk_type == "class" else "")
    if owner:
        word = _domain_word(_SUFFIXES.sub("", owner) or owner)
        if word:
            return word
    word = _domain_word(os.path.splitext(parts[-1])[0])
    return word or _domain_word(chunk.name)


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
        # Step 1: Assign chunks to concepts by where the code lives:
        # domain folder (payments/) → owning class (StripeGateway) → file name
        concept_chunks: dict[str, set[str]] = defaultdict(set)
        concept_keywords: dict[str, set[str]] = defaultdict(set)
        dirs = {os.path.dirname(c.file_path) for c in chunks if c.file_path}
        root = os.path.commonpath(list(dirs)) if dirs else ""

        for chunk in chunks:
            if chunk.name == "<module>" or not chunk.name:
                continue

            concept_name = _concept_for(chunk, root)
            if not concept_name:
                continue  # nothing domain-like about it (e.g. utils/helpers.py:main)
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
