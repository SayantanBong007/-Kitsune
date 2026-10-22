"""
AST-aware chunker with nested method support.

Improvements over Kitsune 1.0:
- Extracts calls/inheritance/imports/decorators DURING chunking (S1)
- Recursively chunks methods inside classes (S8)
- Produces method-level chunks with proper scope tracking
- Module-level code is preserved as a separate chunk
"""
from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from kitsune_2_0.models.chunk import Chunk
from kitsune_2_0.parser.treesitter_parser import (
    CHUNK_NODE_TYPES,
    FUNCTION_NODE_TYPES,
    CLASS_NODE_TYPES,
    STRUCT_NODE_TYPES,
    get_node_end_line,
    get_node_start_line,
    get_node_text,
    parse_file,
    get_language_name,
)
from kitsune_2_0.parser.extractor import (
    extract_calls,
    extract_decorators,
    extract_docstring,
    extract_imports,
    extract_inheritance,
    extract_name,
)


# Maximum depth for recursive class chunking.
# depth=0 → whole class is one chunk (Kitsune 1.0 behavior)
# depth=1 → class + its methods as separate chunks (default)
MAX_RECURSION_DEPTH = 1


def _node_type_to_chunk_type(node_type: str) -> str:
    """Map a tree-sitter node type to a Kitsune chunk type."""
    if node_type in FUNCTION_NODE_TYPES:
        return "function"
    if node_type in CLASS_NODE_TYPES:
        return "class"
    if node_type in STRUCT_NODE_TYPES:
        return "class"  # treat structs/interfaces as classes
    return "module"


def chunk_file(file_path: str | Path, max_depth: int = MAX_RECURSION_DEPTH) -> list[Chunk]:
    """Parse a source file and return a list of AST-aware Chunks.

    Strategy
    --------
    1. Parse the file with tree-sitter → get the root node.
    2. Walk the *top-level* children of the module node.
    3. For each top-level function/class → produce a Chunk.
    4. For classes, recursively extract methods as separate Chunks (up to max_depth).
    5. For runs of non-function/non-class nodes (imports, constants) → collect into
       a single "module" Chunk so nothing is silently discarded.

    Parameters
    ----------
    file_path:
        Path to a source file.
    max_depth:
        How deep to recursively chunk inside classes. 0 = no recursion
        (whole class is one chunk). 1 = extract methods (default).

    Returns
    -------
    List of Chunk objects, ordered by their position in the file.
    """
    path = Path(file_path)
    ext = path.suffix
    language = get_language_name(ext)

    root_node, source = parse_file(path)

    chunks: list[Chunk] = []
    module_level_nodes: list[Node] = []
    file_imports = extract_imports(root_node, source)

    def flush_module_nodes():
        """Turn any accumulated module-level nodes into a single Chunk."""
        if not module_level_nodes:
            return
        meaningful = [n for n in module_level_nodes if n.text and n.text.strip()]
        if not meaningful:
            module_level_nodes.clear()
            return

        combined_text = "\n".join(get_node_text(n, source) for n in meaningful)
        first = meaningful[0]
        last = meaningful[-1]

        # Extract calls from module-level code too
        calls = extract_calls(first, source)
        for n in meaningful[1:]:
            calls |= extract_calls(n, source)

        chunks.append(Chunk(
            text=combined_text,
            chunk_type="module",
            name="<module>",
            file_path=str(path),
            start_line=get_node_start_line(first),
            end_line=get_node_end_line(last),
            language=language,
            calls=calls,
            imports=file_imports,
        ))
        module_level_nodes.clear()

    def process_node(node: Node, scope: str = "", depth: int = 0):
        """Process a chunk-boundary node, optionally recursing into classes."""
        chunk_type = _node_type_to_chunk_type(node.type)
        name = extract_name(node, source)
        docstring = extract_docstring(node, source)
        text = get_node_text(node, source)

        # Extract all relationships DURING chunking (no re-parsing later!)
        calls = extract_calls(node, source)
        inherits = extract_inheritance(node, source) if chunk_type == "class" else []
        decorators = extract_decorators(node, source)

        chunk = Chunk(
            text=text,
            chunk_type=chunk_type,
            name=name,
            file_path=str(path),
            start_line=get_node_start_line(node),
            end_line=get_node_end_line(node),
            scope=scope,
            docstring=docstring,
            language=language,
            calls=calls,
            inherits=inherits,
            imports=file_imports if not scope else [],  # only top-level gets imports
            decorators=decorators,
        )
        chunks.append(chunk)

        # Recursively chunk methods inside classes (S8)
        if chunk_type == "class" and depth < max_depth:
            child_scope = chunk.qualified_name
            for child in node.children:
                if child.type in ("block", "class_body", "declaration_list", "module"):
                    for inner in child.children:
                        if inner.type in FUNCTION_NODE_TYPES:
                            process_node(inner, scope=child_scope, depth=depth + 1)
                        elif inner.type in CLASS_NODE_TYPES and depth + 1 < max_depth:
                            process_node(inner, scope=child_scope, depth=depth + 1)

    # Walk direct children of the module root
    for child in root_node.children:
        if child.type in CHUNK_NODE_TYPES:
            flush_module_nodes()
            process_node(child)
        else:
            module_level_nodes.append(child)

    flush_module_nodes()
    return chunks
