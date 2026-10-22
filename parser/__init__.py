"""Kitsune parser — multi-language tree-sitter parsing and AST-aware chunking."""
from kitsune.parser.treesitter_parser import (
    SUPPORTED_EXTENSIONS,
    parse_file,
    get_node_text,
    get_node_start_line,
    get_node_end_line,
)
from kitsune.parser.chunker import chunk_file
from kitsune.parser.extractor import (
    extract_calls,
    extract_inheritance,
    extract_imports,
    extract_decorators,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "parse_file",
    "get_node_text",
    "get_node_start_line",
    "get_node_end_line",
    "chunk_file",
    "extract_calls",
    "extract_inheritance",
    "extract_imports",
    "extract_decorators",
]
