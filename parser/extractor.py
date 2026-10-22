"""
AST extractor — extracts relationships from tree-sitter AST nodes.

Features:
- Extracts calls, inheritance, imports, AND decorators (not just calls)
- Works on in-memory AST nodes (no re-parsing needed)
- Handles method calls (self.method, obj.method) by extracting the base name
"""
from __future__ import annotations

from tree_sitter import Node

from kitsune.parser.treesitter_parser import get_node_text


def extract_calls(node: Node, source: bytes) -> set[str]:
    """Find all function/method calls inside an AST node.

    Walks the tree recursively. Whenever we hit a 'call' node, we look for
    the callee (identifier or attribute) and extract the base name.

    For attribute calls like 'self.db.query', we extract 'query' as the
    base name to match against other chunks.

    Parameters
    ----------
    node:
        The tree-sitter Node to search inside.
    source:
        The raw bytes of the file.

    Returns
    -------
    A set of function/method names called within this node.
    """
    calls: set[str] = set()

    def walk(n: Node):
        if n.type in ("call", "call_expression"):
            for child in n.children:
                if child.type in ("identifier", "attribute", "member_expression"):
                    callee_text = get_node_text(child, source)
                    # Extract base name from attribute access: self.db.query → query
                    base_name = callee_text.split(".")[-1] if "." in callee_text else callee_text
                    if base_name and not base_name.startswith("_") and len(base_name) > 1:
                        calls.add(base_name)
                    break

        for child in n.children:
            walk(child)

    walk(node)
    return calls


def extract_inheritance(node: Node, source: bytes) -> list[str]:
    """Extract parent class names from a class/struct node.

    Handles:
    - Python: class Foo(Base1, Base2) — bases are in argument_list
    - Java/C++: class Foo extends Base — in superclass_list
    - Go: type Foo struct { ... } — embedded fields

    Parameters
    ----------
    node:
        A class/struct definition node.
    source:
        The raw bytes of the file.

    Returns
    -------
    List of parent class/interface names.
    """
    parents: list[str] = []

    def walk(n: Node):
        # Python: argument_list inside class_definition contains base classes
        # Java: superclass_list / implements_list
        if n.type in ("argument_list", "superclass_list", "super_interfaces_list"):
            for child in n.children:
                if child.type in ("identifier", "type_identifier", "scoped_identifier"):
                    name = get_node_text(child, source)
                    if name and name not in parents:
                        parents.append(name)

        for child in n.children:
            # Only walk into children that could contain inheritance info
            if child.type not in ("block", "class_body", "declaration_list"):
                walk(child)

    walk(node)
    return parents


def extract_imports(node: Node, source: bytes) -> list[str]:
    """Extract import statements from an AST node.

    Handles:
    - Python: import foo / from foo import bar
    - JS/TS: import ... from 'foo'
    - Java: import foo.bar;
    - Go: import "foo"
    - Rust: use foo::bar;

    Parameters
    ----------
    node:
        The tree-sitter Node to search (usually the module root).
    source:
        The raw bytes of the file.

    Returns
    -------
    List of imported module/package names.
    """
    imports: list[str] = []

    def walk(n: Node):
        if n.type in ("import_statement", "import_declaration", "import_from_clause",
                       "use_declaration", "import_spec"):
            text = get_node_text(n, source)
            # Extract module name from various import syntaxes
            # Python: "from foo.bar import baz" → "foo.bar"
            # Python: "import foo" → "foo"
            # JS:     "import x from 'foo'" → "foo"
            # Java:   "import foo.bar.Baz;" → "foo.bar"
            # Go:     "import \"foo\"" → "foo"
            # Rust:   "use foo::bar;" → "foo"
            parts = text.replace("'", '"').replace("`", '"').split('"')
            if len(parts) >= 2:
                # Quoted import path (JS, Go)
                imports.append(parts[1])
            else:
                # Unqualified import (Python, Java, Rust)
                cleaned = text.replace("import ", "").replace("from ", "").replace("use ", "")
                cleaned = cleaned.split(" as ")[0].split(" import ")[0].split(";")[0].strip()
                if cleaned:
                    imports.append(cleaned)

        for child in n.children:
            walk(child)

    walk(node)
    return imports


def extract_decorators(node: Node, source: bytes) -> list[str]:
    """Extract decorator names from a function/class node.

    Handles:
    - Python: @decorator / @decorator(args)
    - Java: @Annotation
    - JS/TS: @Decorator

    Parameters
    ----------
    node:
        A function/class definition node.
    source:
        The raw bytes of the file.

    Returns
    -------
    List of decorator/annotation names.
    """
    decorators: list[str] = []

    for child in node.children:
        if child.type in ("decorator", "annotation", "decorator_call"):
            text = get_node_text(child, source)
            # Extract just the decorator name: "@app.route('/')" → "app.route"
            name = text.lstrip("@").split("(")[0].strip()
            if name:
                decorators.append(name)

    return decorators


def extract_name(node: Node, source: bytes) -> str:
    """Extract the identifier name from a function or class node."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "name"):
            return get_node_text(child, source)
    return ""


def extract_docstring(node: Node, source: bytes) -> str:
    """Extract the docstring from a function or class node, if present.

    A docstring in the AST appears as the first statement in the body,
    and that statement is an expression_statement containing a string.
    """
    for child in node.children:
        if child.type in ("block", "function_body", "class_body", "declaration_list"):
            for stmt in child.children:
                if stmt.type == "expression_statement":
                    for inner in stmt.children:
                        if inner.type == "string":
                            raw = get_node_text(inner, source)
                            return raw.strip("\"'").strip()
    return ""
