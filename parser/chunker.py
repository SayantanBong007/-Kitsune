"""
AST-aware chunker with nested method support.

Features:
- Extracts calls/inheritance/imports/decorators DURING chunking
- Recursively chunks methods inside classes
- Produces method-level chunks with proper scope tracking
- Module-level code is preserved as a separate chunk
"""
from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from kitsune.models.chunk import Chunk
from kitsune.parser.treesitter_parser import (
    CALL_NODE_TYPES,
    CHUNK_NODE_TYPES,
    FUNCTION_NODE_TYPES,
    CLASS_NODE_TYPES,
    STRUCT_NODE_TYPES,
    WRAPPER_NODE_TYPES,
    get_node_end_line,
    get_node_start_line,
    get_node_text,
    parse_file,
    get_language_name,
)
from kitsune.parser.semantics import (
    extract_call_refs,
    extract_field_types,
    extract_import_map,
    extract_var_types,
    go_receiver,
)
from kitsune.parser.extractor import (
    extract_calls,
    extract_decorators,
    extract_docstring,
    extract_imports,
    extract_inheritance,
    extract_name,
)


# Maximum depth for recursive class chunking.
# depth=0 → whole class is one chunk
# depth=1 → class + its methods as separate chunks (default)
MAX_RECURSION_DEPTH = 1

# Runs of top-level code (constants, statements) are split into module chunks
# of roughly this many characters, so one file can't produce a giant chunk.
MAX_MODULE_CHUNK_CHARS = 2000

# Functions longer than this have their nested functions chunked separately
MAX_FUNCTION_CHARS = 3000


def _node_type_to_chunk_type(node_type: str) -> str:
    """Map a tree-sitter node type to a Kitsune chunk type."""
    if node_type in FUNCTION_NODE_TYPES:
        return "function"
    if node_type in CLASS_NODE_TYPES:
        return "class"
    if node_type in STRUCT_NODE_TYPES:
        return "class"  # treat structs/interfaces as classes
    return "module"


_CALLEE_TAIL = re.compile(r"\?\.|\.")  # window.LF → LF

CLASS_BODY_TYPES = ("block", "class_body", "declaration_list", "module",
                    "field_declaration_list", "interface_body")


def _extend_comment_run(run: list[Node], comment: Node) -> list[Node]:
    """Add a comment to the current run; a blank line starts a new run."""
    if run and comment.start_point[0] > run[-1].end_point[0] + 1:
        return [comment]
    return run + [comment]


def _adjacent_comments(run: list[Node], node: Node) -> list[Node]:
    """The comment run if it ends on the line right above `node`, else []."""
    if run and run[-1].end_point[0] >= node.start_point[0] - 1:
        return run
    return []


def _comment_text(comments: list[Node], source: bytes) -> str:
    """Doc comment text without //, #, /*, * markers."""
    lines = []
    for comment in comments:
        for line in get_node_text(comment, source).splitlines():
            line = re.sub(r"^\s*(?:///?|#+|/\*\*?|\*/|\*)\s?", "", line)
            line = re.sub(r"\s*\*/\s*$", "", line).strip()
            if line:
                lines.append(line)
    return " ".join(lines)


def _is_split_method(node: Node, source: bytes) -> bool:
    """Is `node` a method directly in a class body (chunked on its own)?"""
    parent = node.parent
    if parent is None or parent.type not in CLASS_BODY_TYPES:
        return False
    unwrapped = _unwrap(node, source)
    return unwrapped is not None and unwrapped[0].type in FUNCTION_NODE_TYPES


def _calls_outside_methods(node: Node, source: bytes) -> set[str]:
    """Calls made by a class itself (decorators, bases, class-level code),
    excluding calls inside its methods."""
    if node.type in CLASS_BODY_TYPES:
        calls: set[str] = set()
        for inner in node.children:
            unwrapped = _unwrap(inner, source)
            if unwrapped is not None and unwrapped[0].type in FUNCTION_NODE_TYPES:
                continue
            calls |= extract_calls(inner, source)
        return calls
    if node.type in CALL_NODE_TYPES:
        return extract_calls(node, source)
    calls = set()
    for child in node.children:
        calls |= _calls_outside_methods(child, source)
    return calls


def _nested_functions(definition: Node, source: bytes) -> list[tuple[Node, list[Node]]]:
    """Function definitions directly inside a function body, with their doc comments."""
    body = definition.child_by_field_name("body")
    if body is None:
        return []
    found: list[tuple[Node, list[Node]]] = []
    pending: list[Node] = []
    for stmt in body.named_children:
        if stmt.type == "comment":
            pending = _extend_comment_run(pending, stmt)
            continue
        unwrapped = _unwrap(stmt, source)
        if unwrapped is not None and unwrapped[0].type in FUNCTION_NODE_TYPES:
            found.append((stmt, _adjacent_comments(pending, stmt)))
        pending = []
    return found


def _class_outline(node: Node, definition: Node, source: bytes) -> str:
    """Class source with each method body replaced by `...` (signatures kept).

    The methods are indexed as separate chunks, so the class chunk only needs
    its own shape: bases, docstring, fields, and method signatures.
    """
    bodies: list[Node] = []
    for child in definition.children:
        if child.type not in CLASS_BODY_TYPES:
            continue
        for inner in child.children:
            unwrapped = _unwrap(inner, source)
            if unwrapped is not None and unwrapped[0].type in FUNCTION_NODE_TYPES:
                body = unwrapped[0].child_by_field_name("body")
                if body is not None:
                    bodies.append(body)
    return _outline(node, bodies, source)


def _outline(node: Node, bodies: list[Node], source: bytes) -> str:
    """Source of `node` with the given body blocks replaced by `...`."""
    parts: list[bytes] = []
    pos = node.start_byte
    for body in sorted(bodies, key=lambda b: b.start_byte):
        parts.append(source[pos:body.start_byte])
        parts.append(b"{ ... }" if source[body.start_byte:body.start_byte + 1] == b"{" else b"...")
        pos = body.end_byte
    parts.append(source[pos:node.end_byte])
    return b"".join(parts).decode("utf-8", errors="replace")


def _iife_body(node: Node, source: bytes) -> tuple[Node, str] | None:
    """Return (body block, scope name) for a JS/TS IIFE statement, else None.

    Matches `(() => { ... })();`, `(function () { ... }());`, and the module
    pattern `const LF = (() => { ... })();` / `window.LF = (function () {...})();`
    — for the latter two the scope name is the assigned variable ("LF").
    """
    expr = None
    scope = ""
    if node.type == "expression_statement" and node.named_children:
        expr = node.named_children[0]
        if expr.type == "assignment_expression":
            left = expr.child_by_field_name("left")
            scope = _CALLEE_TAIL.split(get_node_text(left, source))[-1] if left is not None else ""
            expr = expr.child_by_field_name("right")
    elif node.type in ("lexical_declaration", "variable_declaration"):
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        if len(declarators) == 1:
            expr = declarators[0].child_by_field_name("value")
            name = declarators[0].child_by_field_name("name")
            scope = get_node_text(name, source) if name is not None else ""
    if expr is None:
        return None
    body = _iife_call_body(expr)
    return (body, scope) if body is not None else None


def _iife_call_body(expr: Node) -> Node | None:
    """Body block of `(function () {...})()`-style call expression, else None."""
    while expr.type == "parenthesized_expression" and expr.named_children:
        expr = expr.named_children[0]
    if expr.type != "call_expression":
        return None
    fn = expr.child_by_field_name("function")
    while fn is not None and fn.type == "parenthesized_expression" and fn.named_children:
        fn = fn.named_children[0]
    if fn is None or fn.type not in ("arrow_function", "function_expression", "function"):
        return None
    body = fn.child_by_field_name("body")
    return body if body is not None and body.type == "statement_block" else None


def _unwrap(node: Node, source: bytes) -> tuple[Node, str] | None:
    """Find the chunk-boundary definition inside a wrapper node.

    Returns (definition_node, name_override) — name_override is "" unless the
    name lives on the wrapper (`const foo = () => {}`, Go `type Foo struct`).
    Returns None if the node is not a chunk boundary at all.
    """
    if node.type in CHUNK_NODE_TYPES:
        return node, ""
    if node.type not in WRAPPER_NODE_TYPES:
        return None

    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        return _unwrap(inner, source) if inner is not None else None

    if node.type == "export_statement":
        inner = node.child_by_field_name("declaration")
        return _unwrap(inner, source) if inner is not None else None

    if node.type in ("lexical_declaration", "variable_declaration"):
        declarators = [c for c in node.named_children if c.type == "variable_declarator"]
        if len(declarators) != 1:
            return None
        value = declarators[0].child_by_field_name("value")
        name = declarators[0].child_by_field_name("name")
        if value is None or name is None or value.type not in CHUNK_NODE_TYPES:
            return None
        return value, get_node_text(name, source)

    if node.type == "type_declaration":
        for spec in node.named_children:
            if spec.type != "type_spec":
                continue
            type_node = spec.child_by_field_name("type")
            name = spec.child_by_field_name("name")
            if type_node is not None and name is not None and type_node.type in CHUNK_NODE_TYPES:
                return type_node, get_node_text(name, source)
        return None

    return None


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
    file_import_map = extract_import_map(root_node, source)

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
        call_refs: set[str] = set()
        var_types: dict[str, str] = {}
        for n in meaningful:
            if n is not first:
                calls |= extract_calls(n, source)
            call_refs.update(extract_call_refs(n, source))
            # module-level `gateway = StripeGateway(...)` types calls in this file
            for var, typ in extract_var_types(n, source).items():
                var_types.setdefault(var, typ)

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
            call_refs=sorted(call_refs),
            var_types=var_types,
            import_map=file_import_map,
        ))
        module_level_nodes.clear()

    def process_node(node: Node, scope: str = "", depth: int = 0,
                     comments: list[Node] | None = None):
        """Process a chunk-boundary node, optionally recursing into classes.

        `node` may be a wrapper (decorated/exported definition): its text,
        line range, and decorators are used, while the type, name, docstring,
        and inheritance come from the definition inside it.
        `comments` are doc comments directly above it (// ..., /** ... */):
        they become part of the chunk and its docstring.
        """
        definition, name_override = _unwrap(node, source) or (node, "")
        chunk_type = _node_type_to_chunk_type(definition.type)
        name = name_override or extract_name(definition, source)
        docstring = extract_docstring(definition, source)
        text = get_node_text(node, source)
        comments = comments or []
        if comments and not docstring:
            docstring = _comment_text(comments, source)

        var_types = extract_var_types(definition, source) if chunk_type == "function" else {}
        receiver = go_receiver(definition, source)
        if receiver is not None:
            # Go methods live outside their struct: func (s *Server) Start()
            # → scope "Server", and `s` is a Server inside the method
            receiver_var, receiver_type = receiver
            scope = scope or receiver_type
            if receiver_var:
                var_types[receiver_var] = receiver_type

        # Extract all relationships DURING chunking (no re-parsing later!)
        splits_methods = chunk_type == "class" and depth < max_depth
        if splits_methods:
            # Methods become their own chunks — don't credit their calls to the
            # class too, or every caller/impact query double-counts them.
            calls = _calls_outside_methods(node, source)
            call_refs = extract_call_refs(node, source, skip=lambda n: _is_split_method(n, source))
            # ...and don't repeat their bodies: the class chunk becomes an
            # outline, so one big class can't fill the whole context budget.
            text = _class_outline(node, definition, source)
        else:
            calls = extract_calls(node, source)
            call_refs = extract_call_refs(node, source)

        # Large functions with nested helpers (closures, inner defs): the
        # helpers become their own chunks and the parent an outline, so one
        # big function doesn't swallow the context budget.
        nested = _nested_functions(definition, source) \
            if chunk_type == "function" and depth <= max_depth and len(text) > MAX_FUNCTION_CHARS else []
        if nested:
            nested_nodes = {id(n) for n, _ in nested}
            skip = lambda n: id(n) in nested_nodes  # noqa: E731
            call_refs = extract_call_refs(node, source, skip=skip)
            calls = {ref.partition("|")[2] for ref in call_refs}
            bodies = [b for b in (_unwrap(n, source)[0].child_by_field_name("body") for n, _ in nested)
                      if b is not None]
            text = _outline(node, bodies, source)

        inherits = extract_inheritance(definition, source) if chunk_type == "class" else []
        decorators = extract_decorators(node, source)
        field_types = extract_field_types(definition, source) if chunk_type == "class" else {}

        start_line = get_node_start_line(node)
        if comments:
            text = source[comments[0].start_byte:node.start_byte].decode(
                "utf-8", errors="replace") + text
            start_line = get_node_start_line(comments[0])

        chunk = Chunk(
            text=text,
            chunk_type=chunk_type,
            name=name,
            file_path=str(path),
            start_line=start_line,
            end_line=get_node_end_line(node),
            scope=scope,
            docstring=docstring,
            language=language,
            calls=calls,
            inherits=inherits,
            imports=file_imports if not scope else [],  # only top-level gets imports
            decorators=decorators,
            call_refs=call_refs,
            var_types=var_types,
            field_types=field_types,
        )
        chunks.append(chunk)

        for inner, doc in nested:
            process_node(inner, scope=chunk.qualified_name, depth=max_depth + 1, comments=doc)

        # Recursively chunk methods inside classes (S8)
        if splits_methods:
            child_scope = chunk.qualified_name
            for child in definition.children:
                if child.type in CLASS_BODY_TYPES:
                    pending: list[Node] = []
                    for inner in child.children:
                        if inner.type == "comment":
                            pending = _extend_comment_run(pending, inner)
                            continue
                        doc = _adjacent_comments(pending, inner)
                        pending = []
                        unwrapped = _unwrap(inner, source)
                        if unwrapped is None:
                            continue
                        inner_type = unwrapped[0].type
                        if inner_type in FUNCTION_NODE_TYPES:
                            process_node(inner, scope=child_scope, depth=depth + 1, comments=doc)
                        elif inner_type in CLASS_NODE_TYPES and depth + 1 < max_depth:
                            process_node(inner, scope=child_scope, depth=depth + 1, comments=doc)

    def walk_top_level(children: list[Node], scope: str = ""):
        module_chars = 0
        pending: list[Node] = []  # comment run that may document the next definition
        for child in children:
            if child.type == "comment":
                pending = _extend_comment_run(pending, child)
                continue
            iife = _iife_body(child, source)
            is_definition = iife is None and _unwrap(child, source) is not None
            doc = _adjacent_comments(pending, child) if is_definition else []
            leftover = [c for c in pending if c not in doc]
            pending = []
            if leftover:
                module_level_nodes.extend(leftover)
                module_chars += sum(c.end_byte - c.start_byte + 1 for c in leftover)
            if iife is not None:
                # (() => { ... })() wraps a whole file — chunk its contents
                # as if they were top-level instead of one giant module chunk
                flush_module_nodes()
                body, iife_scope = iife
                inner_scope = ".".join(s for s in (scope, iife_scope) if s)
                walk_top_level(body.named_children, inner_scope)
                flush_module_nodes()
                module_chars = 0
            elif is_definition:
                flush_module_nodes()
                process_node(child, scope=scope, comments=doc)
                module_chars = 0
            else:
                # Cap module chunks so long runs of top-level code are split
                size = child.end_byte - child.start_byte + 1  # +1: joined with "\n"
                if module_level_nodes and module_chars + size > MAX_MODULE_CHUNK_CHARS:
                    flush_module_nodes()
                    module_chars = 0
                module_level_nodes.append(child)
                module_chars += size
        # Trailing comments with nothing after them stay module-level
        module_level_nodes.extend(pending)

    # Walk direct children of the module root
    walk_top_level(root_node.children)
    flush_module_nodes()

    # node_id must be unique per file: several module blocks, overloaded
    # methods, or @property + @x.setter would otherwise share one ID.
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.node_id in seen:
            chunk.id_suffix = f"@L{chunk.start_line}"
        seen.add(chunk.node_id)
    return chunks
