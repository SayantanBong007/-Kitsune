"""
AST extractor — extracts relationships from tree-sitter AST nodes.

Features:
- Extracts calls, inheritance, imports, AND decorators (not just calls)
- Works on in-memory AST nodes (no re-parsing needed)
- Handles method calls (self.method, obj.method) by extracting the base name
"""
from __future__ import annotations

import re

from tree_sitter import Node

from kitsune.parser.treesitter_parser import CALL_NODE_TYPES, get_node_text

_CALLEE_SEPARATORS = re.compile(r"\?\.|\.|::|->")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")

_INHERITANCE_CONTAINER_TYPES = (
    "argument_list", "superclass", "super_interfaces", "type_list", "superclass_list",
    "class_heritage", "extends_clause", "implements_clause", "base_class_clause",
)
_BASE_NAME_TYPES = (
    "identifier", "type_identifier", "scoped_identifier", "scoped_type_identifier",
    "qualified_identifier", "attribute", "member_expression", "generic_type",
)

# Leaf node types that hold a C/C++ function or method name inside a declarator
_DECLARATOR_NAME_TYPES = ("identifier", "field_identifier", "qualified_identifier",
                          "destructor_name", "operator_name", "type_identifier")


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

    def add(callee_text: str):
        # Base name from member access in any language:
        # self.db.query / obj->run / pkg::mod::func / x?.y / Vec::<T>::new
        base_name = _CALLEE_SEPARATORS.split(callee_text.strip())[-1]
        match = _IDENTIFIER.match(base_name)
        if not match:
            return
        base_name = match.group(0)
        # Skip dunders (super().__init__) and 1-char names; keep _private helpers
        if len(base_name) > 1 and not (base_name.startswith("__") and base_name.endswith("__")):
            calls.add(base_name)

    def walk(n: Node):
        if n.type in CALL_NODE_TYPES:
            # "function" field: Python/JS/TS/Go/Rust/C/C++; "name" field: Java
            callee = n.child_by_field_name("function") or n.child_by_field_name("name")
            if callee is not None:
                add(get_node_text(callee, source))
            else:
                for child in n.children:
                    if child.type in ("identifier", "attribute", "member_expression"):
                        add(get_node_text(child, source))
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
        # Java: superclass / super_interfaces → type_list
        # JS: class_heritage; TS: class_heritage → extends_clause / implements_clause
        # C++: base_class_clause
        if n.type in _INHERITANCE_CONTAINER_TYPES:
            for child in n.children:
                if child.type in _BASE_NAME_TYPES:
                    # models.Model / React.Component / ns::Base / List<T> → bare
                    # class name, so EXTENDS edges match the class's own chunk
                    name = get_node_text(child, source).split("<")[0]
                    name = _CALLEE_SEPARATORS.split(name)[-1].strip()
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

    # Where annotations live depends on the grammar: directly on the node (TS), inside its
    # `modifiers` (Java: `@Service public class X`), or on the parent `decorated_definition`
    # (Python). A bare `@Service` is a marker_annotation in Java.
    candidates = list(node.children)
    for child in node.children:
        if child.type == "modifiers":
            candidates.extend(child.children)
    if node.parent is not None and node.parent.type == "decorated_definition":
        candidates.extend(node.parent.children)
    for child in candidates:
        if child.type in ("decorator", "annotation", "marker_annotation", "decorator_call"):
            text = get_node_text(child, source)
            # Extract just the decorator name: "@app.route('/')" → "app.route"
            name = text.lstrip("@").split("(")[0].strip()
            if name:
                decorators.append(name)

    return decorators


def extract_name(node: Node, source: bytes) -> str:
    """Extract the identifier name from a function or class node."""
    # Most grammars expose a "name" field (this also covers Go's field_identifier
    # method names, which the child scan below misses)
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return get_node_text(name_node, source)

    # C/C++: int foo(int x) → declarator(function_declarator) → declarator(identifier)
    declarator = node.child_by_field_name("declarator")
    while declarator is not None:
        if declarator.type in _DECLARATOR_NAME_TYPES:
            # Foo::bar → bar, so it matches bare call names
            return get_node_text(declarator, source).split("::")[-1]
        declarator = declarator.child_by_field_name("declarator")

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
