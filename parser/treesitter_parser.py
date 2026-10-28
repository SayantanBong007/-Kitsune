"""
Tree-sitter multi-language parser.

Supports 8 languages via dedicated tree-sitter packages. The same API
works for all languages — only the language package and extension mapping
change when adding a new language.

Features:
- Language detection from file extension (centralized mapping)
- Node type constants for cross-language chunking
"""
from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
import tree_sitter_cpp as tscpp
import tree_sitter_c as tsc
import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_rust as tsrust
from tree_sitter import Language, Node, Parser


# ── Language registry ────────────────────────────────────────────────────────

LANGUAGES: dict[str, Language] = {
    ".py":   Language(tspython.language()),
    ".js":   Language(tsjavascript.language()),
    ".jsx":  Language(tsjavascript.language()),
    ".ts":   Language(tstypescript.language_typescript()),
    ".tsx":  Language(tstypescript.language_tsx()),
    ".c":    Language(tsc.language()),
    ".h":    Language(tsc.language()),
    ".cpp":  Language(tscpp.language()),
    ".hpp":  Language(tscpp.language()),
    ".cc":   Language(tscpp.language()),
    ".go":   Language(tsgo.language()),
    ".java": Language(tsjava.language()),
    ".rs":   Language(tsrust.language()),
}

# Reverse map: extension → human-readable language name
LANGUAGE_NAMES: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".go": "go", ".java": "java", ".rs": "rust",
}


def _optional_language(module: str, attr: str, extensions: tuple[str, ...], name: str):
    """Register a grammar only if its package is installed (pip install tree-sitter-kotlin ...)."""
    try:
        mod = __import__(module)
        lang = Language(getattr(mod, attr)())
    except Exception:
        return
    for ext in extensions:
        LANGUAGES[ext] = lang
        LANGUAGE_NAMES[ext] = name


_optional_language("tree_sitter_kotlin", "language", (".kt", ".kts"), "kotlin")
_optional_language("tree_sitter_c_sharp", "language", (".cs",), "csharp")
_optional_language("tree_sitter_php", "language_php", (".php",), "php")

# All supported extensions (used by the indexer)
SUPPORTED_EXTENSIONS = tuple(LANGUAGES.keys())


# ── Node type constants for cross-language chunking ──────────────────────────
# These are the tree-sitter node types we treat as chunk boundaries.
# Grouped by category so the chunker can classify chunks uniformly.

FUNCTION_NODE_TYPES = {
    "function_definition",        # Python
    "function_declaration",       # JS/TS/Java/C++
    "arrow_function",             # JS/TS
    "function_expression",        # JS/TS: const foo = function () {...}
    "generator_function_declaration",  # JS/TS
    "method_definition",          # JS/TS
    "method_declaration",         # Java / Go
    "constructor_declaration",    # Java
    "function_item",              # Rust
    "function_declaration",       # Go
}

CLASS_NODE_TYPES = {
    "class_definition",           # Python
    "class_declaration",          # JS/TS/Java/C++/Kotlin/C#/PHP
    "class_specifier",            # C++
    "impl_item",                  # Rust
    "enum_declaration",           # Java/C#/TS: enums carry methods and constants
    "record_declaration",         # Java 16+ / C#
    "object_declaration",         # Kotlin: object Foo { ... }
    "trait_declaration",          # PHP
    "struct_declaration",         # C#
}

STRUCT_NODE_TYPES = {
    "struct_type",                # Go
    "interface_type",             # Go
    "struct_specifier",           # C++
    "struct_item",                # Rust
    "trait_item",                 # Rust
    "interface_declaration",      # TS/Java
}

# Wrapper nodes whose inner definition is the real chunk boundary.
# The wrapper's text (decorators, `export`) is kept as part of the chunk.
WRAPPER_NODE_TYPES = {
    "decorated_definition",       # Python: @decorator + def/class
    "export_statement",           # JS/TS: export function/class/const
    "lexical_declaration",        # JS/TS: const foo = () => {...}
    "variable_declaration",       # JS:    var foo = function () {...}
    "type_declaration",           # Go:    type Foo struct {...}
}

# All chunk-boundary node types
CHUNK_NODE_TYPES = FUNCTION_NODE_TYPES | CLASS_NODE_TYPES | STRUCT_NODE_TYPES

# Node types that represent function/method calls
CALL_NODE_TYPES = {"call", "call_expression", "method_invocation",
                   "invocation_expression",                          # C#
                   "member_call_expression", "nullsafe_member_call_expression",  # PHP $a->b()
                   "function_call_expression", "scoped_call_expression"}         # PHP f(), A::b()

# Node types that represent inheritance/base classes
INHERITANCE_NODE_TYPES = {
    "argument_list",     # Python: class Foo(Base) — base is in arguments
    "superclass_list",   # Java/C++
    "base_class_clause", # Go
}


def get_language_name(ext: str) -> str:
    """Return the human-readable language name for a file extension."""
    return LANGUAGE_NAMES.get(ext, "unknown")


def make_parser(ext: str) -> Parser:
    """Return a configured tree-sitter Parser for the given extension."""
    lang = LANGUAGES.get(ext)
    if not lang:
        raise ValueError(f"Unsupported language extension: {ext}")
    return Parser(lang)


def parse_file(path: str | Path) -> tuple[Node, bytes]:
    """Parse a source file and return the root AST node + raw bytes.

    Parameters
    ----------
    path:
        Path to a source file.

    Returns
    -------
    Tuple of (root_node, source_bytes).
    """
    path_obj = Path(path)
    source = path_obj.read_bytes()
    parser = make_parser(path_obj.suffix)
    tree = parser.parse(source)
    return tree.root_node, source


def get_node_text(node: Node, source: bytes) -> str:
    """Extract the source text for a given node using its byte offsets."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def get_node_start_line(node: Node) -> int:
    """Return the 1-indexed start line of a node."""
    return node.start_point[0] + 1


def get_node_end_line(node: Node) -> int:
    """Return the 1-indexed end line of a node."""
    return node.end_point[0] + 1
