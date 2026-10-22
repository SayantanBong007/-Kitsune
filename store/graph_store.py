"""
Graph store — Neo4j with NetworkX fallback and disk persistence.

Features:
- Disk persistence for NetworkX fallback: serializes the graph to pickle
  so it survives process restarts in CI environments.
- Richer edge types: CALLS, EXTENDS, IMPORTS, DECORATED_BY.
- Multi-hop traversal support: get_call_graph with configurable depth.
- Node metadata includes all chunk fields.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from pathlib import Path

import networkx as nx

from kitsune.models.chunk import Chunk

logger = logging.getLogger(__name__)

# Edge types
EDGE_CALLS = "CALLS"
EDGE_EXTENDS = "EXTENDS"          # class inheritance
EDGE_IMPORTS = "IMPORTS"          # module-level import
EDGE_DECORATED_BY = "DECORATED_BY"
_EDGE_TYPES = (EDGE_CALLS, EDGE_EXTENDS, EDGE_IMPORTS, EDGE_DECORATED_BY)

# Cypher filter: skip call edges the resolver marked ambiguous
_NO_AMBIGUOUS = "AND ALL(r IN relationships(p) WHERE NOT coalesce(r.ambiguous, false))"


def _related_record(attrs: dict, depth: int) -> dict:
    """Shape a graph node into a get_related() result."""
    name = attrs.get("name") or ""
    return {
        "name": name,
        "qualified_name": attrs.get("qualified_name") or name,
        "file_path": attrs.get("file_path"),
        "start_line": attrs.get("start_line"),
        "depth": depth,
    }


class GraphStore:
    """Stores the dependency graph with Neo4j primary + NetworkX fallback.

    If Neo4j is unavailable (e.g. in GitHub Actions), falls back to an
    in-memory NetworkX graph that is PERSISTED to disk as pickle (S4).

    Scalability fixes for large codebases:
    - Name→node_id index for O(1) lookups (was O(V) linear scan)
    - Pickle binary serialization (10x faster than JSON for large graphs)
    """

    def __init__(self, persist_path: str | None = None, namespace: str = ""):
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "kitsune_password")

        # Neo4j is shared by every project pointed at it: each node carries
        # its project's namespace (`ns`) and every query filters on it.
        # (The NetworkX fallback is separated by persist_path instead.)
        self._ns = namespace
        self._driver = None
        self.use_fallback = False
        self.nx_graph: nx.DiGraph | None = None
        self._persist_path = persist_path or os.path.join(
            os.getcwd(), "data", "kitsune", "graph.pkl"
        )
        # Name → set of node_ids index for O(1) lookup (avoids scanning all nodes)
        self._name_index: dict[str, set[str]] = {}

        try:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            self._driver.verify_connectivity()
            logger.info(f"Neo4j connected successfully at {uri}!")
            self._setup_constraints()
        except Exception as e:
            logger.warning(f"Neo4j connection failed: {e}")
            logger.warning("Falling back to in-memory NetworkX Graph (with disk persistence)!")
            self._driver = None
            self.use_fallback = True
            self.nx_graph = nx.DiGraph()
            self._load_from_disk()

    def close(self):
        if self._driver:
            self._driver.close()

    def _setup_constraints(self):
        if self.use_fallback:
            return
        queries = [
            "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
            # Name/file lookups are filtered by namespace
            "CREATE INDEX chunk_name_ns IF NOT EXISTS FOR (c:Chunk) ON (c.name, c.ns)",
            "CREATE INDEX chunk_file_ns IF NOT EXISTS FOR (c:Chunk) ON (c.file_path, c.ns)",
        ]
        assert self._driver is not None
        with self._driver.session() as session:
            for query in queries:
                try:
                    session.run(query)
                except Exception as e:
                    logger.debug(f"Could not create constraint/index: {e}")

    def _nid(self, node_id: str) -> str:
        """Neo4j node id: the chunk node_id, prefixed by the namespace if any
        (ids are unique database-wide, and two projects may share file paths)."""
        return f"{self._ns}|{node_id}" if self._ns else node_id

    # ── Disk persistence for NetworkX fallback (S4) ──────────────────────

    def _load_from_disk(self):
        """Load the NetworkX graph from disk if it exists."""
        path = Path(self._persist_path)
        if not path.exists():
            logger.info("No persisted graph found — starting fresh.")
            return

        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self.nx_graph = data.get("graph", nx.DiGraph())
            self._name_index = data.get("name_index", {})
            logger.info(f"Loaded graph from disk: {self.nx_graph.number_of_nodes()} nodes, "
                        f"{self.nx_graph.number_of_edges()} edges.")
        except Exception as e:
            logger.warning(f"Could not load graph from disk: {e}")
            self.nx_graph = nx.DiGraph()
            self._name_index = {}

    def _save_to_disk(self):
        """Persist the NetworkX graph to disk as pickle (binary, fast)."""
        if not self.use_fallback or self.nx_graph is None:
            return

        path = Path(self._persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "wb") as f:
            pickle.dump({"graph": self.nx_graph, "name_index": self._name_index}, f)
        logger.debug(f"Persisted graph to disk: {self.nx_graph.number_of_nodes()} nodes, "
                     f"{self.nx_graph.number_of_edges()} edges.")

    def _index_node_name(self, node_id: str, attrs: dict):
        """Add a node to the name index for O(1) lookup."""
        name = attrs.get("name") or attrs.get("qualified_name", "")
        if name:
            if name not in self._name_index:
                self._name_index[name] = set()
            self._name_index[name].add(node_id)
        qname = attrs.get("qualified_name", "")
        if qname and qname != name:
            if qname not in self._name_index:
                self._name_index[qname] = set()
            self._name_index[qname].add(node_id)

    def _find_nodes_by_name(self, chunk_name: str) -> list[str]:
        """O(1) lookup of node IDs by name (was O(V) linear scan)."""
        return list(self._name_index.get(chunk_name, set()))

    # ── Node operations ──────────────────────────────────────────────────

    def upsert_chunk_node(self, chunk: Chunk):
        """Create or update a chunk node in the graph."""
        if self.use_fallback:
            assert self.nx_graph is not None
            attrs = chunk.to_metadata()
            attrs["resolve"] = chunk.resolve_info()  # facts for the call resolver
            self.nx_graph.add_node(chunk.node_id, **attrs)
            self._index_node_name(chunk.node_id, attrs)  # maintain O(1) index
            self._relink_stub(chunk.name, chunk.node_id)
            return

        query = """
        MERGE (c:Chunk {id: $id})
        SET c.ns = $ns, c.node_id = $node_id,
            c.name = $name, c.qualified_name = $qualified_name,
            c.type = $type, c.file_path = $file_path,
            c.start_line = $start_line, c.end_line = $end_line,
            c.scope = $scope, c.docstring = $docstring,
            c.language = $language, c.hash = $hash, c.resolve = $resolve
        """
        assert self._driver is not None
        meta = chunk.to_metadata()
        params = {k: meta[k] for k in
                  ("name", "qualified_name", "file_path",
                   "start_line", "end_line", "scope", "docstring", "language", "hash")}
        params["type"] = meta["chunk_type"]
        params["resolve"] = json.dumps(chunk.resolve_info())
        with self._driver.session() as session:
            session.run(query, id=self._nid(chunk.node_id), ns=self._ns,
                        node_id=chunk.node_id, **params)
            # Edges added before this node existed point at an id-less stub
            # with the same name — move them onto the real node.
            for rel in _EDGE_TYPES:
                session.run(f"""
                MATCH (src)-[r:{rel}]->(s:Chunk {{name: $name, ns: $ns}}) WHERE s.id IS NULL
                MATCH (c:Chunk {{id: $id}})
                MERGE (src)-[:{rel}]->(c)
                DELETE r
                """, name=chunk.name, ns=self._ns, id=self._nid(chunk.node_id))
            session.run("MATCH (s:Chunk {name: $name, ns: $ns}) WHERE s.id IS NULL "
                        "DETACH DELETE s", name=chunk.name, ns=self._ns)

    def _relink_stub(self, name: str, node_id: str):
        """Point edges that were attached to `stub::<name>` at the real node.

        When file A calls a function defined in file B and A is indexed first,
        the edge targets a stub. Without relinking, the graph depends on file
        order and callers of B's function are never found.
        """
        assert self.nx_graph is not None
        stub_id = f"stub::{name}"
        if not name or stub_id not in self.nx_graph:
            return
        for pred in list(self.nx_graph.predecessors(stub_id)):
            self.nx_graph.add_edge(pred, node_id, **self.nx_graph.get_edge_data(pred, stub_id))
        self.nx_graph.remove_node(stub_id)
        self._name_index.get(name, set()).discard(stub_id)

    def remove_file(self, file_path: str, keep_ids: set[str] | None = None):
        """Drop a file's outgoing edges, and the nodes no longer in it.

        Used by incremental indexing before a changed file is re-added (its
        current chunks are passed as `keep_ids` so callers' edges into them
        survive) and for files deleted from the repo (keep_ids empty).
        """
        keep_ids = keep_ids or set()
        if self.use_fallback:
            assert self.nx_graph is not None
            file_nodes = [n for n, d in self.nx_graph.nodes(data=True)
                          if d.get("file_path") == file_path]
            for node_id in file_nodes:
                self.nx_graph.remove_edges_from(list(self.nx_graph.out_edges(node_id)))
                if node_id not in keep_ids:
                    attrs = self.nx_graph.nodes[node_id]
                    for key in ("name", "qualified_name"):
                        self._name_index.get(attrs.get(key, ""), set()).discard(node_id)
                    # Keep callers' edges on a stub so they relink if the
                    # function reappears (e.g. it moved to another file).
                    name = attrs.get("name", "")
                    preds = list(self.nx_graph.predecessors(node_id))
                    if name and preds:
                        stub_id = f"stub::{name}"
                        self.nx_graph.add_node(stub_id, name=name, type="stub")
                        self._index_node_name(stub_id, {"name": name})
                        for pred in preds:
                            self.nx_graph.add_edge(
                                pred, stub_id, **self.nx_graph.get_edge_data(pred, node_id))
                    self.nx_graph.remove_node(node_id)
            return

        if not self._driver:
            return
        with self._driver.session() as session:
            session.run("MATCH (c:Chunk {file_path: $fp, ns: $ns})-[r]->() DELETE r",
                        fp=file_path, ns=self._ns)
            session.run("MATCH (c:Chunk {file_path: $fp, ns: $ns}) WHERE NOT c.id IN $keep "
                        "DETACH DELETE c", fp=file_path, ns=self._ns,
                        keep=[self._nid(k) for k in keep_ids])

    def add_edge(self, source_id: str, target_name: str, edge_type: str = EDGE_CALLS):
        """Add a typed edge between two nodes.

        Edge types:
        - CALLS: function A calls function B
        - EXTENDS: class A inherits from class B
        - IMPORTS: module A imports module B
        - DECORATED_BY: function A is decorated by B
        """
        if self.use_fallback:
            assert self.nx_graph is not None
            # For CALLS, the target might not have a chunk node yet — create a stub
            if source_id not in self.nx_graph:
                self.nx_graph.add_node(source_id, name=source_id.split("::")[-1])
                self._index_node_name(source_id, {"name": source_id.split("::")[-1]})

            # O(1) lookup via name index (was O(V) linear scan)
            target_ids = [t for t in self._find_nodes_by_name(target_name)
                          if not t.startswith("stub::")]
            # Names are not unique (e.g. two classes with a `save` method):
            # prefer a definition in the caller's own file, otherwise link
            # every definition rather than an arbitrary one.
            source_file = source_id.split("::")[0]
            same_file = [t for t in target_ids if t.split("::")[0] == source_file]
            target_ids = sorted(same_file or target_ids)
            if not target_ids:
                stub_id = f"stub::{target_name}"
                self.nx_graph.add_node(stub_id, name=target_name, type="stub")
                self._index_node_name(stub_id, {"name": target_name})
                target_ids = [stub_id]

            for target_id in target_ids:
                self.nx_graph.add_edge(source_id, target_id, type=edge_type)
            return

        rel = edge_type if edge_type in _EDGE_TYPES else EDGE_CALLS
        query = f"""
        MATCH (source:Chunk {{id: $source_id}})
        MERGE (target:Chunk {{name: $target_name, ns: $ns}})
        MERGE (source)-[:{rel}]->(target)
        """
        assert self._driver is not None
        with self._driver.session() as session:
            session.run(query, source_id=self._nid(source_id), target_name=target_name,
                        ns=self._ns)

    # ── Call linking (graph/resolver.py) ──────────────────────────────────

    def all_definitions(self) -> list[dict]:
        """Every indexed chunk node with the facts the call resolver needs."""
        if self.use_fallback:
            assert self.nx_graph is not None
            return [
                {"node_id": n, "name": d.get("name", ""),
                 "qualified_name": d.get("qualified_name") or d.get("name", ""),
                 "file_path": d.get("file_path", ""), "chunk_type": d.get("chunk_type", ""),
                 "language": d.get("language", ""), "resolve": d.get("resolve") or {}}
                for n, d in self.nx_graph.nodes(data=True)
                if d.get("file_path") and d.get("type") != "stub"
            ]
        if not self._driver:
            return []
        with self._driver.session() as session:
            rows = session.run("""
                MATCH (c:Chunk {ns: $ns}) WHERE c.id IS NOT NULL
                RETURN c.node_id AS node_id, c.name AS name, c.qualified_name AS qualified_name,
                       c.file_path AS file_path, c.type AS chunk_type, c.language AS language,
                       c.resolve AS resolve
            """, ns=self._ns)
            return [{**r.data(), "resolve": json.loads(r["resolve"]) if r["resolve"] else {}}
                    for r in rows if r["node_id"]]

    def replace_call_edges(self, edges: list[tuple[str, str, bool]],
                           externals: list[tuple[str, str]]) -> int:
        """Replace every CALLS edge with a freshly resolved set.

        `edges` are (source node_id, target node_id, ambiguous); `externals`
        are (source node_id, callee name) for calls into code that isn't
        indexed (stdlib, libraries), stored as stub nodes. Returns the number
        of resolved call edges.
        """
        if self.use_fallback:
            assert self.nx_graph is not None
            calls = [(u, v) for u, v, d in self.nx_graph.edges(data=True)
                     if d.get("type") == EDGE_CALLS]
            self.nx_graph.remove_edges_from(calls)
            for src, dst, ambiguous in edges:
                if src in self.nx_graph and dst in self.nx_graph and \
                        not self.nx_graph.has_edge(src, dst):
                    self.nx_graph.add_edge(src, dst, type=EDGE_CALLS, ambiguous=ambiguous)
            for src, name in externals:
                stub_id = f"stub::{name}"
                if stub_id not in self.nx_graph:
                    self.nx_graph.add_node(stub_id, name=name, type="stub")
                    self._index_node_name(stub_id, {"name": name})
                if src in self.nx_graph and not self.nx_graph.has_edge(src, stub_id):
                    self.nx_graph.add_edge(src, stub_id, type=EDGE_CALLS, ambiguous=False)
            orphans = [n for n, d in self.nx_graph.nodes(data=True)
                       if d.get("type") == "stub" and self.nx_graph.degree(n) == 0]
            for n in orphans:
                self._name_index.get(self.nx_graph.nodes[n].get("name", ""), set()).discard(n)
            self.nx_graph.remove_nodes_from(orphans)
            return len(edges)

        if not self._driver:
            return 0
        with self._driver.session() as session:
            session.run("MATCH (:Chunk {ns: $ns})-[r:CALLS]->() DELETE r", ns=self._ns)
            for i in range(0, len(edges), 1000):
                rows = [{"s": self._nid(s), "t": self._nid(t), "a": a}
                        for s, t, a in edges[i:i + 1000]]
                session.run("""
                    UNWIND $rows AS row
                    MATCH (s:Chunk {id: row.s}) MATCH (t:Chunk {id: row.t})
                    MERGE (s)-[r:CALLS]->(t) SET r.ambiguous = row.a
                """, rows=rows)
            for i in range(0, len(externals), 1000):
                rows = [{"s": self._nid(s), "n": n} for s, n in externals[i:i + 1000]]
                session.run("""
                    UNWIND $rows AS row
                    MATCH (s:Chunk {id: row.s})
                    MERGE (t:Chunk {name: row.n, ns: $ns, external: true})
                    MERGE (s)-[r:CALLS]->(t) SET r.ambiguous = false
                """, rows=rows, ns=self._ns)
            session.run("MATCH (c:Chunk {ns: $ns}) WHERE c.id IS NULL AND NOT (c)--() DELETE c",
                        ns=self._ns)
        return len(edges)

    # ── Query operations ─────────────────────────────────────────────────

    def get_related(self, symbol: str, direction: str = "callees", depth: int = 1,
                    include_external: bool = False,
                    include_ambiguous: bool = False) -> list[dict]:
        """Callers or callees of a symbol, with where each one lives.

        Unlike get_dependencies/get_upstream_callers (bare names), this tells
        same-named functions apart: each result has name, qualified_name,
        file_path, start_line, and depth (hops from `symbol`).

        Parameters
        ----------
        symbol:
            Name or qualified name ("save" or "Order.save").
        direction:
            "callees" (what it calls) or "callers" (what calls it).
        depth:
            How many CALLS hops to follow.
        include_external:
            Include callees not defined in the indexed code (stubs).
        include_ambiguous:
            Also follow calls the resolver could not pin to one definition
            (e.g. `obj.save()` with several `save` methods) — "possible" links.
        """
        downstream = direction == "callees"
        if self.use_fallback:
            assert self.nx_graph is not None
            results: dict[str, dict] = {}
            visited: set[str] = set()
            frontier = self._find_nodes_by_name(symbol)
            for hop in range(1, depth + 1):
                next_frontier = []
                for n in frontier:
                    if n in visited:
                        continue
                    visited.add(n)
                    neighbors = (self.nx_graph.successors(n) if downstream
                                 else self.nx_graph.predecessors(n))
                    for m in neighbors:
                        edge = (self.nx_graph.get_edge_data(n, m) if downstream
                                else self.nx_graph.get_edge_data(m, n))
                        if not edge or edge.get("type") != EDGE_CALLS:
                            continue
                        if edge.get("ambiguous") and not include_ambiguous:
                            continue
                        attrs = self.nx_graph.nodes[m]
                        if attrs.get("type") == "stub" and not include_external:
                            continue
                        if m not in results:
                            results[m] = _related_record(attrs, hop)
                        next_frontier.append(m)
                frontier = next_frontier
            return sorted(results.values(), key=lambda r: (r["depth"], r["qualified_name"]))

        if not self._driver:
            return []
        if downstream:
            pattern = f"(start:Chunk)-[:CALLS*1..{depth}]->(other)"
        else:
            pattern = f"(other:Chunk)-[:CALLS*1..{depth}]->(start:Chunk)"
        external_filter = "" if include_external else "AND other.id IS NOT NULL"
        ambiguous_filter = "" if include_ambiguous else _NO_AMBIGUOUS
        query = f"""
        MATCH p = {pattern}
        WHERE start.ns = $ns AND (start.name = $name OR start.qualified_name = $name)
              {external_filter} {ambiguous_filter}
        RETURN other.name AS name, other.qualified_name AS qualified_name,
               other.file_path AS file_path, other.start_line AS start_line,
               min(length(p)) AS depth
        ORDER BY depth, qualified_name
        """
        with self._driver.session() as session:
            return [_related_record(r.data(), r["depth"])
                    for r in session.run(query, name=symbol, ns=self._ns)]

    def get_dependencies(self, chunk_name: str, depth: int = 1,
                         include_external: bool = False) -> list[str]:
        """Find what a function calls (downstream dependencies).

        Parameters
        ----------
        chunk_name:
            The name or qualified name of the chunk.
        depth:
            How many hops to traverse (S2). 1 = immediate calls only.
        include_external:
            Also return calls to things not defined in the indexed code
            (builtins, libraries — `len`, `append`, `requests.get`). Off by
            default so they don't flood context and impact counts.
        """
        if self.use_fallback:
            assert self.nx_graph is not None
            # O(1) name index lookup (was O(V) linear scan over ALL nodes)
            nodes = self._find_nodes_by_name(chunk_name)
            deps: set[str] = set()
            visited: set[str] = set()
            frontier = list(nodes)

            for _ in range(depth):
                next_frontier = []
                for n in frontier:
                    if n in visited:
                        continue
                    visited.add(n)
                    for successor in self.nx_graph.successors(n):
                        edge_data = self.nx_graph.get_edge_data(n, successor)
                        if edge_data and edge_data.get("type") == EDGE_CALLS                                 and not edge_data.get("ambiguous"):
                            attrs = self.nx_graph.nodes[successor]
                            if attrs.get("type") == "stub" and not include_external:
                                continue
                            name = attrs.get("name", "")
                            if name:
                                deps.add(name)
                            next_frontier.append(successor)
                frontier = next_frontier

            return list(deps)

        if not self._driver:
            return []

        # Neo4j: variable-length path traversal
        # Stub (external) nodes are the ones with no id
        external_filter = "" if include_external else "AND callee.id IS NOT NULL"
        query = f"""
        MATCH p = (caller:Chunk)-[:CALLS*1..{depth}]->(callee)
        WHERE caller.ns = $ns AND (caller.name = $name OR caller.qualified_name = $name)
              {external_filter} {_NO_AMBIGUOUS}
        RETURN DISTINCT callee.name AS callee_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name, ns=self._ns)
            return [record["callee_name"] for record in result]

    def get_upstream_callers(self, chunk_name: str, depth: int = 1) -> list[str]:
        """Find what calls a function (upstream callers).

        Parameters
        ----------
        chunk_name:
            The name or qualified name of the chunk.
        depth:
            How many hops to traverse (S2). 1 = immediate callers only.
        """
        if self.use_fallback:
            assert self.nx_graph is not None
            # O(1) name index lookup (was O(V) linear scan over ALL nodes)
            nodes = self._find_nodes_by_name(chunk_name)
            callers: set[str] = set()
            visited: set[str] = set()
            frontier = list(nodes)

            for _ in range(depth):
                next_frontier = []
                for n in frontier:
                    if n in visited:
                        continue
                    visited.add(n)
                    for predecessor in self.nx_graph.predecessors(n):
                        edge_data = self.nx_graph.get_edge_data(predecessor, n)
                        if edge_data and edge_data.get("type") == EDGE_CALLS                                 and not edge_data.get("ambiguous"):
                            name = self.nx_graph.nodes[predecessor].get("name", "")
                            if name:
                                callers.add(name)
                            next_frontier.append(predecessor)
                frontier = next_frontier

            return list(callers)

        if not self._driver:
            return []

        query = f"""
        MATCH p = (caller)-[:CALLS*1..{depth}]->(callee:Chunk)
        WHERE callee.ns = $ns AND (callee.name = $name OR callee.qualified_name = $name)
              {_NO_AMBIGUOUS}
        RETURN DISTINCT caller.name AS caller_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name, ns=self._ns)
            return [record["caller_name"] for record in result]

    def get_call_graph(self, entity_name: str, depth: int = 2) -> dict:
        """Return a structured topology of the call graph (S15).

        This traverses the graph up to the specified depth.
        """
        return {
            "entity": entity_name,
            "metadata": self.get_node_metadata(entity_name),
            "calls": self.get_dependencies(entity_name, depth=depth),
            "called_by": self.get_upstream_callers(entity_name, depth=depth),
            "depth": depth,
        }

    def get_node_metadata(self, chunk_name: str) -> dict | None:
        """Fetch metadata for a chunk by name."""
        if self.use_fallback:
            assert self.nx_graph is not None
            # O(1) name index lookup (was O(V) linear scan over ALL nodes)
            node_ids = self._find_nodes_by_name(chunk_name)
            for node_id in node_ids:
                data = self.nx_graph.nodes.get(node_id)
                if data:
                    return dict(data)
            return None

        if not self._driver:
            return None
        query = """
        MATCH (c:Chunk)
        WHERE c.ns = $ns AND c.id IS NOT NULL
              AND (c.name = $name OR c.qualified_name = $name)
        RETURN c LIMIT 1
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name, ns=self._ns).single()
            if result:
                node = result["c"]
                return dict(node)
            return None

    def get_inheritance_tree(self, class_name: str) -> list[str]:
        """Find all parent classes (EXTENDS edges)."""
        if self.use_fallback:
            assert self.nx_graph is not None
            # O(1) name index lookup
            nodes = self._find_nodes_by_name(class_name)
            parents: set[str] = set()
            for n in nodes:
                for successor in self.nx_graph.successors(n):
                    edge_data = self.nx_graph.get_edge_data(n, successor)
                    if edge_data and edge_data.get("type") == EDGE_EXTENDS:
                        name = self.nx_graph.nodes[successor].get("name", "")
                        if name:
                            parents.add(name)
            return list(parents)

        if not self._driver:
            return []
        query = """
        MATCH (child:Chunk {name: $name, ns: $ns})-[:EXTENDS]->(parent)
        RETURN parent.name AS parent_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=class_name, ns=self._ns)
            return [record["parent_name"] for record in result]

    def clear_all(self):
        """Delete all nodes and edges of this graph (this namespace only, on Neo4j)."""
        if self.use_fallback:
            assert self.nx_graph is not None
            self.nx_graph.clear()
            self._name_index = {}
            self._save_to_disk()
            return

        if not self._driver:
            return
        # Only this project's nodes — other namespaces in the same database are kept
        query = "MATCH (n:Chunk {ns: $ns}) DETACH DELETE n"
        assert self._driver is not None
        with self._driver.session() as session:
            session.run(query, ns=self._ns)

    def save(self):
        """Persist the graph to disk (NetworkX fallback only)."""
        if self.use_fallback:
            self._save_to_disk()

    def stats(self) -> dict:
        """Return graph statistics."""
        if self.use_fallback:
            assert self.nx_graph is not None
            edge_types: dict[str, int] = {}
            for _, _, data in self.nx_graph.edges(data=True):
                etype = data.get("type", "UNKNOWN")
                edge_types[etype] = edge_types.get(etype, 0) + 1
            return {
                "backend": "networkx",
                "nodes": self.nx_graph.number_of_nodes(),
                "edges": self.nx_graph.number_of_edges(),
                "edge_types": edge_types,
            }

        if not self._driver:
            return {"backend": "neo4j", "nodes": -1, "edges": -1, "edge_types": {}}
        with self._driver.session() as session:
            nodes = session.run("MATCH (c:Chunk {ns: $ns}) RETURN count(c) AS n",
                                ns=self._ns).single()["n"]
            edge_types = {r["t"]: r["n"] for r in session.run(
                "MATCH (c:Chunk {ns: $ns})-[r]->() RETURN type(r) AS t, count(r) AS n",
                ns=self._ns)}
        return {"backend": "neo4j", "nodes": nodes, "edges": sum(edge_types.values()),
                "edge_types": edge_types}
