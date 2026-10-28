"""
Call resolver — decides which definition each call site targets.

Runs after indexing over the whole project (so file order never matters),
using the facts extracted in parser/semantics.py:

    self.validate()     → method of the enclosing class (or an inherited one)
    db.insert(...)      → `insert` in the module `db` was imported from
    order.save()        → `order` is an Order (annotation, `order = Order(...)`,
                          or the variable is named after a class) → Order.save
    this.api.get()      → field `api` of the class is an ApiClient → ApiClient.get
    s.ledger.Apply(tx)  → Go receiver `s` is a Server, its `ledger` field a Ledger
    checkout()          → a function (never a method) — same file, imported,
                          same package/dir, or the only one with that name
    append(x)           → no function named append → external (Go builtin),
                          even though a method Ledger.append exists

A call that still matches several definitions is returned as *ambiguous*:
graph queries skip those edges unless asked to include them.
"""
from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from kitsune.graph.library_catalog import GLOBAL_CATALOG, DynamicLibraryCatalog

_SELF_NAMES = {"self", "this", "cls", "Self"}
_SUPER_NAMES = {"super", "super()"}
# Languages where a bare call inside a class can target the class's own method
_IMPLICIT_THIS = {"java", "cpp"}


@dataclass
class Resolution:
    """Outcome of linking all call sites."""
    edges: list[tuple[str, str, bool]] = field(default_factory=list)  # (src, dst, ambiguous)
    externals: list[tuple[str, str]] = field(default_factory=list)    # (src, callee name)


def _norm_path(path: str) -> str:
    """Forward slashes, no extension."""
    path = path.replace("\\", "/")
    root, _ext = posixpath.splitext(path)
    return root


class CallResolver:
    def __init__(self, definitions: list[dict], catalog: DynamicLibraryCatalog | None = None):
        # Where calls into imported external packages are recorded (library roles)
        self.catalog = catalog if catalog is not None else GLOBAL_CATALOG
        self.defs = [d for d in definitions if d.get("name") and d.get("name") != "<module>"]
        self.module_chunks = [d for d in definitions if d.get("name") == "<module>"]
        self.by_name: dict[str, list[dict]] = defaultdict(list)
        self.classes: dict[str, dict] = {}
        self.class_by_name: dict[str, list[dict]] = defaultdict(list)
        self.methods: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.files: set[str] = set()

        for d in definitions:
            d.setdefault("resolve", {})
            self.files.add(d["file_path"])
        for d in self.defs:
            self.by_name[d["name"]].append(d)
            if d["resolve"].get("chunk_type", d.get("chunk_type")) == "class":
                self.classes[d["qualified_name"]] = d
                self.class_by_name[d["name"]].append(d)
        for d in self.defs:
            scope = d["resolve"].get("scope", "")
            if scope in self.classes:
                self.methods[(scope, d["name"])].append(d)

        self.implementations: dict[str, list[dict]] = defaultdict(list)
        self.beans_by_type: dict[str, list[dict]] = defaultdict(list)
        _BEAN_DECORATORS = {"service", "component", "repository", "controller",
                            "injectable", "singleton", "bean", "restcontroller"}

        for d in self.defs:
            if d["resolve"].get("chunk_type", d.get("chunk_type")) == "class":
                decs = {dec.lower().split("(")[0].strip()
                        for dec in (d["resolve"].get("decorators") or [])}
                is_bean = bool(decs & _BEAN_DECORATORS)
                cls_name = d["name"]
                cls_qn = d["qualified_name"]
                if is_bean:
                    self.beans_by_type[cls_name].append(d)
                    self.beans_by_type[cls_qn].append(d)
                for base in (d["resolve"].get("inherits") or []):
                    base_name = base.split(".")[-1]
                    self.implementations[base_name].append(d)
                    self.implementations[base].append(d)
                    if is_bean:
                        self.beans_by_type[base_name].append(d)
                        self.beans_by_type[base].append(d)

        # Per-file facts from module-level chunks: imports and module variables
        self.file_imports: dict[str, dict[str, str]] = defaultdict(dict)
        self.file_vars: dict[str, dict[str, str]] = defaultdict(dict)
        for d in definitions:
            info = d["resolve"]
            self.file_imports[d["file_path"]].update(info.get("import_map") or {})
            if d.get("name") == "<module>":
                self.file_vars[d["file_path"]].update(info.get("var_types") or {})

    def _implementations_of(self, class_qn: str, beans_only: bool = False) -> list[str]:
        """Return qualified names of classes that implement or extend class_qn (DI resolution)."""
        base_name = class_qn.split(".")[-1]
        impls = self.beans_by_type.get(base_name) or \
            ([] if beans_only else self.implementations.get(base_name)) or []
        seen = set()
        res = []
        for c in impls:
            qn = c["qualified_name"]
            if qn != class_qn and qn not in seen:
                seen.add(qn)
                res.append(qn)
        return res

    # ── Lookups ───────────────────────────────────────────────────────────

    def _is_method(self, d: dict) -> bool:
        return d["resolve"].get("scope", "") in self.classes

    def _enclosing_class(self, src: dict) -> str | None:
        if src["qualified_name"] in self.classes:
            return src["qualified_name"]
        scope = src["resolve"].get("scope", "")
        while scope:
            if scope in self.classes:
                return scope
            scope = scope.rpartition(".")[0]
        return None

    def _classes_named(self, type_name: str, near_file: str) -> list[str]:
        """Class qualified names for a type name, preferring the nearest."""
        if type_name in self.classes:
            return [type_name]
        found = self.class_by_name.get(type_name, [])
        if len(found) > 1:
            local = [c for c in found if c["file_path"] == near_file]
            imported = self._in_module_of(type_name, near_file, found)
            found = local or imported or found
        return [c["qualified_name"] for c in found]

    def _parents(self, class_qn: str) -> list[str]:
        cls = self.classes[class_qn]
        parents = []
        for base in cls["resolve"].get("inherits") or []:
            parents.extend(self._classes_named(base.split(".")[-1], cls["file_path"]))
        return parents

    def _methods_of(self, class_qn: str, name: str, seen: set[str] | None = None) -> list[dict]:
        """Method `name` on a class, searching base classes if not defined there."""
        seen = seen or set()
        if class_qn in seen or class_qn not in self.classes:
            return []
        seen.add(class_qn)
        own = self.methods.get((class_qn, name))
        if own:
            return own
        for parent in self._parents(class_qn):
            found = self._methods_of(parent, name, seen)
            if found:
                return found
        return []

    def _field_type(self, class_qn: str, field_name: str, seen: set[str] | None = None) -> str:
        seen = seen or set()
        if class_qn in seen or class_qn not in self.classes:
            return ""
        seen.add(class_qn)
        typ = (self.classes[class_qn]["resolve"].get("field_types") or {}).get(field_name, "")
        if typ:
            return typ
        for parent in self._parents(class_qn):
            typ = self._field_type(parent, field_name, seen)
            if typ:
                return typ
        return ""

    def _module_files(self, module: str, from_file: str) -> list[str]:
        """Indexed files that an import path refers to."""
        if not module:
            return []
        src_dir = posixpath.dirname(_norm_path(from_file))
        if module.startswith("./") or module.startswith("../"):  # JS/TS relative
            base = posixpath.normpath(posixpath.join(src_dir, module))
            targets = {base, base + "/index"}
            return [f for f in self.files if _norm_path(f) in targets]
        if module.startswith("."):  # Python relative: from .x import y
            dots = len(module) - len(module.lstrip("."))
            base_dir = src_dir
            for _ in range(dots - 1):
                base_dir = posixpath.dirname(base_dir)
            rest = module.lstrip(".").replace(".", "/")
            base = posixpath.join(base_dir, rest) if rest else base_dir
            return [f for f in self.files
                    if _norm_path(f) in (base, base + "/__init__")]
        # Absolute: dotted (Python/Java) or slash (Go/JS packages) — suffix match
        path = module.replace(".", "/") if "/" not in module else module
        matches = []
        for f in self.files:
            norm = _norm_path(f)
            if (norm == path or norm.endswith("/" + path)
                    or norm.endswith("/" + path + "/__init__")
                    or posixpath.dirname(norm).endswith("/" + path)):  # Go package dir
                matches.append(f)
        return matches

    def _in_module_of(self, name: str, from_file: str, candidates: list[dict]) -> list[dict]:
        module = self.file_imports.get(from_file, {}).get(name)
        if module is None:
            return []
        files = set(self._module_files(module, from_file))
        return [c for c in candidates if c["file_path"] in files]

    def _type_of_receiver(self, receiver: str, src: dict, var_types: dict[str, str]) -> str | None:
        """Class qualified name a receiver expression refers to, if knowable."""
        if "(" in receiver or "[" in receiver:
            return None
        parts = receiver.replace("->", ".").replace("::", ".").replace("?.", ".").split(".")
        base, rest = parts[0], parts[1:]
        cls = self._enclosing_class(src)
        if base in _SELF_NAMES:
            current = cls
        elif base in var_types:
            named = self._classes_named(var_types[base], src["file_path"])
            current = named[0] if len(named) == 1 else None
        elif cls and src.get("language") in _IMPLICIT_THIS and self._field_type(cls, base):
            # Java `service.run()` inside a class means this.service: use the field's type
            # (@Autowired private TravelService service;)
            named = self._classes_named(self._field_type(cls, base), src["file_path"])
            current = named[0] if len(named) == 1 else None
        elif not rest and base in self.class_by_name:
            named = self._classes_named(base, src["file_path"])  # static call: Util.log()
            current = named[0] if len(named) == 1 else None
        else:
            # Variable named after a class: order.save() → Order
            named = [c for c in self.classes.values() if c["name"].lower() == base.lower()]
            current = named[0]["qualified_name"] if len(named) == 1 else None
        for attr in rest:
            if current is None:
                return None
            typ = self._field_type(current, attr)
            named = self._classes_named(typ, src["file_path"]) if typ else []
            current = named[0] if len(named) == 1 else None
        return current

    # ── Resolution ────────────────────────────────────────────────────────

    def _pick_function(self, src: dict, funcs: list[dict]) -> tuple[list[dict], bool]:
        """Choose among same-named functions: local, imported, same dir, unique."""
        file = src["file_path"]
        same_file = [f for f in funcs if f["file_path"] == file]
        if same_file:
            return same_file, False
        imported = self._in_module_of(funcs[0]["name"], file, funcs)
        if imported:
            return imported, False
        if len(funcs) == 1:
            return funcs, False
        same_dir = [f for f in funcs
                    if posixpath.dirname(_norm_path(f["file_path"])) == posixpath.dirname(_norm_path(file))]
        if len(same_dir) == 1:
            return same_dir, False
        return funcs, True

    def resolve_ref(self, src: dict, ref: str) -> tuple[list[dict], bool] | None:
        """Targets for one call site: (definitions, ambiguous), or None if external."""
        receiver, _, name = ref.partition("|")
        candidates = self.by_name.get(name)
        if not candidates:
            return None
        var_types = {**self.file_vars.get(src["file_path"], {}),
                     **(src["resolve"].get("var_types") or {})}
        cls = self._enclosing_class(src)

        if not receiver:
            funcs = [d for d in candidates if not self._is_method(d)]
            if funcs:
                return self._pick_function(src, funcs)
            if cls and src.get("language") in _IMPLICIT_THIS:  # helper() inside a Java class
                found = self._methods_of(cls, name)
                if found:
                    return found, False
            return None  # e.g. Go builtin append() vs method Ledger.append

        if receiver in _SUPER_NAMES and cls:
            for parent in self._parents(cls):
                found = self._methods_of(parent, name)
                if found:
                    return found, False
            return None

        # Module alias: db.insert(...), http.ListenAndServe(...)
        module = self.file_imports.get(src["file_path"], {}).get(receiver)
        if module is not None and receiver not in var_types:
            files = set(self._module_files(module, src["file_path"]))
            found = [d for d in candidates if d["file_path"] in files and not self._is_method(d)]
            return (found, False) if found else None

        class_qn = self._type_of_receiver(receiver, src, var_types)
        if class_qn is not None:
            found = self._methods_of(class_qn, name)
            # Dependency injection / interfaces: a field typed as an interface
            # (PaymentGateway) really calls the injected implementation
            # (@Service StripePaymentGateway). Prefer implementations when a
            # bean implements the type, or when the type itself has no body for
            # the method. A concrete class's own method still wins otherwise —
            # self.helper() in a base class must not jump to subclasses.
            prefer_impls = bool(self._implementations_of(class_qn, beans_only=True)) or not found
            if prefer_impls:
                impl_methods = []
                for impl_qn in self._implementations_of(class_qn):
                    impl_methods.extend(m for m in self._methods_of(impl_qn, name)
                                        if m not in impl_methods)
                if impl_methods:
                    return (impl_methods, len(impl_methods) > 1)
            return (found, False) if found else None  # known type, method not in repo
        base = receiver.split(".")[0]
        if receiver == base and base in var_types and \
                not self._classes_named(var_types[base], src["file_path"]):
            return None  # typed as something outside the repo: app: Flask, w: ResponseWriter

        # Unknown receiver type: unique method name → link; several → ambiguous
        methods = [d for d in candidates if self._is_method(d)] or candidates
        return methods, len(methods) > 1

    def resolve_all(self) -> Resolution:
        out = Resolution()
        seen_edges: dict[tuple[str, str], bool] = {}
        for src in self.defs + self.module_chunks:
            externals: set[str] = set()
            for ref in src["resolve"].get("call_refs") or []:
                result = self.resolve_ref(src, ref)
                if result is None:
                    receiver, _, name = ref.partition("|")
                    base = receiver.split(".")[0]
                    pkg = self.file_imports.get(src["file_path"], {}).get(base) if receiver else None
                    if pkg is None:
                        # builtin (len), or a method on an object of unknown type
                        externals.add(name)
                        continue
                    # Call into an imported package: keep its provenance
                    # (stripe::Charge.create) and record it for the library catalog.
                    sub = receiver.split(".", 1)[1] if "." in receiver else ""
                    # Mockito.when(restTemplate.exchange(...)).thenReturn: keep the call
                    # chain's names, not its arguments (they can be kilobytes long)
                    sub = re.split(r"[(\s]", sub, maxsplit=1)[0].rstrip(".")
                    externals.add(f"{pkg}::{sub}.{name}" if sub else f"{pkg}::{name}")
                    self.catalog.record_call(
                        package=pkg,
                        method=name,
                        caller_node=src["node_id"],
                        caller_file=src["file_path"],
                        receiver_var=receiver,
                    )
                    continue
                targets, ambiguous = result
                for target in targets:
                    key = (src["node_id"], target["node_id"])
                    # A confident resolution wins over an ambiguous one
                    seen_edges[key] = seen_edges.get(key, True) and ambiguous
            out.externals.extend((src["node_id"], name) for name in sorted(externals))
        out.edges = [(s, t, amb) for (s, t), amb in seen_edges.items()]
        return out
