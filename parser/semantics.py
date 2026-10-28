"""
Lightweight semantic facts for call resolution — no type checker needed.

Extracted per chunk during K1 and used by graph/resolver.py to decide which
definition a call like `order.save()` actually targets:

- call refs:    every call site as "receiver|name" ("self|validate", "db|insert", "|checkout")
- var types:    local/parameter variable → type name (annotations, `x = Order(...)`,
                `new ApiClient()`, Go `&Server{}` / `NewServer()`, Go method receivers)
- field types:  class/struct field → type name (`this.api: ApiClient`, Go struct fields,
                `self.repo = Repo()`)
- import map:   local name → module it comes from (`from billing import charge` → billing)

Covers Python, JavaScript/TypeScript, Go, and Java; other languages simply yield
fewer facts (calls then resolve by name as before).
"""
from __future__ import annotations

import re

from tree_sitter import Node

from kitsune.parser.treesitter_parser import CALL_NODE_TYPES, get_node_text

_SEPARATOR = re.compile(r"\?\.|\.|::|->")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_TYPE_WRAPPERS = {
    "Optional", "List", "list", "Dict", "dict", "Set", "set", "Tuple", "tuple",
    "Sequence", "Iterable", "Union", "Promise", "Array", "ReadonlyArray", "Partial",
    "Readonly", "Record", "Map", "Awaitable", "Type", "type", "const", "readonly",
}
_FUNCTION_SCOPES = {
    "function_definition", "function_declaration", "method_definition", "method_declaration",
    "arrow_function", "function_expression", "function_item", "constructor_declaration",
    "generator_function_declaration",
}


def type_name(text: str) -> str:
    """Normalize a type annotation to a bare class name.

    "*Ledger" → Ledger, "Note[]" → Note, "Optional[Order]" → Order,
    "Promise<Note>" → Note, "http.ResponseWriter" → ResponseWriter.
    """
    for ident in _IDENT.findall(text):
        if ident not in _TYPE_WRAPPERS:
            last = ident
            # qualified types: take the segment after the last dot
            tail = text.split(ident, 1)[1]
            dotted = re.match(r"\.([A-Za-z_][\w.]*)", tail)
            if dotted:
                last = dotted.group(1).split(".")[-1]
            return last
    return ""


def _text(node: Node | None, source: bytes) -> str:
    return get_node_text(node, source) if node is not None else ""


def _first_identifier(node: Node, source: bytes) -> str:
    for child in node.named_children:
        if child.type in ("identifier", "field_identifier", "property_identifier"):
            return _text(child, source)
    return ""


# ── Call sites ────────────────────────────────────────────────────────────────

def split_callee(callee_text: str) -> tuple[str, str]:
    """"self.db.query" → ("self.db", "query"); "checkout" → ("", "checkout")."""
    text = re.sub(r"^(?:await|new|yield)\s+", "", callee_text.strip())
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"<[^<>()]*>$", "", text)  # trailing generic args: request<T>
    matches = list(_SEPARATOR.finditer(text))
    if not matches:
        receiver, name = "", text
    else:
        last = matches[-1]
        receiver, name = text[:last.start()], text[last.end():]
    ident = _IDENT.match(name)
    return receiver, ident.group(0) if ident else ""


def call_ref(node: Node, source: bytes) -> str | None:
    """Call site as "receiver|name", or None if the callee isn't a plain name."""
    if node.type in ("method_invocation", "member_call_expression",
                     "nullsafe_member_call_expression"):  # Java a.b(), PHP $a->b()
        receiver = _text(node.child_by_field_name("object"), source)
        name = _text(node.child_by_field_name("name"), source)
    elif node.type == "scoped_call_expression":           # PHP Foo::bar()
        receiver = _text(node.child_by_field_name("scope"), source)
        name = _text(node.child_by_field_name("name"), source)
    else:
        callee = node.child_by_field_name("function")
        if callee is None and node.named_children:
            # Kotlin: call_expression(navigation_expression | identifier, value_arguments)
            first = node.named_children[0]
            callee = first if first.type in ("navigation_expression", "identifier",
                                             "simple_identifier") else None
        if callee is None:
            return None
        receiver, name = split_callee(_text(callee, source))
    # PHP: $this->repo → this.repo, so receivers read the same in every language
    receiver = receiver.replace("$", "").replace("->", ".").replace("?.", ".")
    if len(name) < 2 or (name.startswith("__") and name.endswith("__")):
        return None
    receiver = re.sub(r"\s+", "", receiver)
    return f"{receiver}|{name}"


def extract_call_refs(node: Node, source: bytes, skip=None) -> list[str]:
    """All call sites inside `node` (sorted, unique).

    `skip(n)` may return True to not descend into a subtree (e.g. methods
    that are chunked separately).
    """
    refs: set[str] = set()

    def walk(n: Node):
        if skip is not None and skip(n):
            return
        if n.type in CALL_NODE_TYPES:
            ref = call_ref(n, source)
            if ref:
                refs.add(ref)
        for child in n.children:
            walk(child)

    walk(node)
    return sorted(refs)


# ── Variable types ────────────────────────────────────────────────────────────

def _constructed_type(value: Node | None, source: bytes) -> str:
    """Type created by an expression: Order(...), new ApiClient(), &Server{}, NewLedger()."""
    if value is None:
        return ""
    if value.type == "new_expression":  # JS/TS
        return type_name(_text(value.child_by_field_name("constructor"), source))
    if value.type == "object_creation_expression":  # Java
        return type_name(_text(value.child_by_field_name("type"), source))
    if value.type == "unary_expression":  # Go &Server{}
        return _constructed_type(value.child_by_field_name("operand"), source)
    if value.type == "composite_literal":  # Go Server{}
        return type_name(_text(value.child_by_field_name("type"), source))
    if value.type in ("call", "call_expression"):
        fn = value.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            name = _text(fn, source)
            if name[:1].isupper() and not name.isupper():
                if name.startswith("New") and len(name) > 3 and name[3].isupper():
                    return name[3:]  # Go convention: NewServer() → *Server
                return name  # Python/JS: Order(...)
    if value.type in ("await_expression", "parenthesized_expression") and value.named_children:
        return _constructed_type(value.named_children[0], source)
    return ""


def extract_var_types(node: Node, source: bytes) -> dict[str, str]:
    """Variable → type for parameters and local assignments inside `node`."""
    types: dict[str, str] = {}

    def put(name: str, typ: str):
        if name and typ and name not in types:
            types[name] = typ

    def walk(n: Node, top: bool):
        t = n.type
        # Python parameters
        if t == "typed_parameter":
            put(_first_identifier(n, source), type_name(_text(n.child_by_field_name("type"), source)))
        elif t == "typed_default_parameter":
            put(_text(n.child_by_field_name("name"), source),
                type_name(_text(n.child_by_field_name("type"), source)))
        # TS parameters (incl. `private api: ApiClient`)
        elif t in ("required_parameter", "optional_parameter"):
            pattern = n.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                put(_text(pattern, source), type_name(_text(n.child_by_field_name("type"), source)))
        # Go / Java parameters
        elif t in ("parameter_declaration", "formal_parameter"):
            typ = type_name(_text(n.child_by_field_name("type"), source))
            for child in n.named_children:
                if child.type == "identifier":
                    put(_text(child, source), typ)
        # Python: x = Order(...)  /  x: Order = ...
        elif t == "assignment":
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                annotated = type_name(_text(n.child_by_field_name("type"), source))
                put(_text(left, source),
                    annotated or _constructed_type(n.child_by_field_name("right"), source))
        # JS/TS: const x = new ApiClient()  /  const x: T = ...
        elif t == "variable_declarator":
            name = n.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                annotated = n.child_by_field_name("type")
                typ = (type_name(_text(annotated, source)) if annotated is not None and
                       annotated.type == "type_annotation" else "")
                put(_text(name, source),
                    typ or _constructed_type(n.child_by_field_name("value"), source))
        # Java: User u = new User();
        elif t == "local_variable_declaration":
            typ = type_name(_text(n.child_by_field_name("type"), source))
            for decl in n.named_children:
                if decl.type == "variable_declarator":
                    put(_text(decl.child_by_field_name("name"), source), typ)
        # Go: srv := &Server{} / NewServer(cfg)
        elif t == "short_var_declaration":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is not None and right is not None:
                for name_node, value in zip(left.named_children, right.named_children):
                    if name_node.type == "identifier":
                        put(_text(name_node, source), _constructed_type(value, source))

        for child in n.children:
            # Nested functions have their own scope (and their own chunk)
            if not top and child.type in _FUNCTION_SCOPES:
                continue
            walk(child, False)

    walk(node, True)
    return types


def go_receiver(node: Node, source: bytes) -> tuple[str, str] | None:
    """Go method receiver: func (s *Server) Start() → ("s", "Server")."""
    if node.type != "method_declaration":
        return None
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    for param in receiver.named_children:
        if param.type == "parameter_declaration":
            typ = type_name(_text(param.child_by_field_name("type"), source))
            var = _text(param.child_by_field_name("name"), source)
            if typ:
                return var, typ
    return None


# ── Class / struct field types ────────────────────────────────────────────────

def extract_field_types(definition: Node, source: bytes) -> dict[str, str]:
    """Field → type for a class/struct definition."""
    fields: dict[str, str] = {}

    def put(name: str, typ: str):
        if name and typ and name not in fields:
            fields[name] = typ

    def walk(n: Node, ctor_params: dict[str, str]):
        t = n.type
        if t == "public_field_definition":  # TS: notes: Note[] = []
            annotated = n.child_by_field_name("type")
            put(_text(n.child_by_field_name("name"), source),
                type_name(_text(annotated, source)) if annotated is not None
                else _constructed_type(n.child_by_field_name("value"), source))
        elif t == "required_parameter" and any(
                c.type == "accessibility_modifier" for c in n.children):
            # TS parameter property: constructor(private api: ApiClient)
            put(_text(n.child_by_field_name("pattern"), source),
                type_name(_text(n.child_by_field_name("type"), source)))
        elif t == "field_declaration":
            typ = type_name(_text(n.child_by_field_name("type"), source))
            names = [c for c in n.named_children
                     if c.type in ("field_identifier", "variable_declarator")]
            for c in names:  # Go struct field / Java field
                put(_text(c.child_by_field_name("name"), source) if c.type == "variable_declarator"
                    else _text(c, source), typ)
        elif t in ("assignment", "assignment_expression"):
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is not None and left.type in ("attribute", "member_expression"):
                obj = left.child_by_field_name("object")
                attr = (left.child_by_field_name("attribute")
                        or left.child_by_field_name("property"))
                if obj is not None and _text(obj, source) in ("self", "this"):
                    typ = type_name(_text(n.child_by_field_name("type"), source))
                    if not typ and right is not None and right.type == "identifier":
                        typ = ctor_params.get(_text(right, source), "")
                    put(_text(attr, source), typ or _constructed_type(right, source))
        if t in ("function_definition", "method_definition", "constructor_declaration", "method_declaration"):
            # self.x = x inside __init__/constructor: use the parameter's annotation
            ctor_params = extract_var_types(n, source)
        for child in n.children:
            walk(child, ctor_params)

    walk(definition, {})
    return fields


# ── Imports ───────────────────────────────────────────────────────────────────

_BEAN_ANNOTATIONS = {"Service", "Component", "Repository", "Controller", "RestController"}


def _annotations(node: Node | None, source: bytes) -> list[tuple[str, str]]:
    """(name, first string argument) for each Java annotation on a declaration."""
    out = []
    for mods in (c for c in (node.children if node is not None else []) if c.type == "modifiers"):
        for a in mods.children:
            if a.type not in ("annotation", "marker_annotation"):
                continue
            name = _text(a.child_by_field_name("name"), source).split(".")[-1]
            args = a.child_by_field_name("arguments")
            value = re.search(r'"([^"]*)"', _text(args, source)) if args is not None else None
            out.append((name, value.group(1) if value else ""))
    return out


def extract_di_facts(definition: Node, source: bytes) -> dict:
    """Spring bean facts of a Java class, for choosing between several beans of one type:
    its bean name (@Service("stripe") or the class name in lowerCamelCase), whether it
    is @Primary, and @Qualifier("...") on its fields and constructor parameters."""
    if definition.type not in ("class_declaration", "record_declaration"):
        return {}
    anns = _annotations(definition, source)
    facts: dict = {}
    bean = next((v for n, v in anns if n in _BEAN_ANNOTATIONS), None)
    if bean is not None:
        cls = _text(definition.child_by_field_name("name"), source)
        facts["bean"] = bean or (cls[:1].lower() + cls[1:])
    if any(n == "Primary" for n, _ in anns):
        facts["primary"] = True
    qualifiers = {}
    for n in _walk_nodes(definition):
        if n.type == "field_declaration":
            q = next((v for name, v in _annotations(n, source) if name == "Qualifier" and v), None)
            if q:
                for d in n.children:
                    if d.type == "variable_declarator":
                        qualifiers[_text(d.child_by_field_name("name"), source)] = q
        elif n.type == "formal_parameter":
            q = next((v for name, v in _annotations(n, source) if name == "Qualifier" and v), None)
            if q:   # constructor injection: the parameter usually becomes a same-named field
                qualifiers[_text(n.child_by_field_name("name"), source)] = q
    if qualifiers:
        facts["qualifiers"] = qualifiers
    return facts


def _walk_nodes(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def extract_import_map(root: Node, source: bytes) -> dict[str, str]:
    """Local name → module path for a file's imports."""
    imports: dict[str, str] = {}

    def walk(n: Node):
        t = n.type
        if t == "import_statement" and n.child_by_field_name("source") is not None:
            module = _text(n.child_by_field_name("source"), source).strip("'\"`")
            for clause in n.named_children:
                if clause.type != "import_clause":
                    continue
                for part in clause.named_children:
                    if part.type == "identifier":  # default import
                        imports[_text(part, source)] = module
                    elif part.type == "namespace_import":
                        imports[_first_identifier(part, source)] = module
                    elif part.type == "named_imports":
                        for spec in part.named_children:
                            if spec.type == "import_specifier":
                                local = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                                imports[_text(local, source)] = module
        elif t == "import_statement":  # Python: import a.b [as c]
            for part in n.named_children:
                if part.type == "dotted_name":
                    module = _text(part, source)
                    imports[module] = module
                    imports.setdefault(module.split(".")[0], module.split(".")[0])
                elif part.type == "aliased_import":
                    imports[_text(part.child_by_field_name("alias"), source)] = \
                        _text(part.child_by_field_name("name"), source)
        elif t == "import_from_statement":  # Python: from a.b import c [as d]
            module = _text(n.child_by_field_name("module_name"), source)
            for i, part in enumerate(n.children):
                if n.field_name_for_child(i) != "name":
                    continue
                if part.type == "aliased_import":
                    imports[_text(part.child_by_field_name("alias"), source)] = module
                else:
                    imports[_text(part, source).split(".")[-1]] = module
        elif t == "import_spec":  # Go
            path = _text(n.child_by_field_name("path"), source).strip('"`')
            alias = _text(n.child_by_field_name("name"), source)
            imports[alias or path.split("/")[-1]] = path
        elif t == "import_declaration" and n.named_children and \
                n.named_children[0].type == "scoped_identifier":  # Java
            path = _text(n.named_children[0], source)
            imports[path.split(".")[-1]] = path
        for child in n.children:
            walk(child)

    walk(root)
    return imports
