"""
Graph store — Neo4j with NetworkX fallback and disk persistence.

Improvements over Kitsune 1.0:
- Disk persistence for NetworkX fallback (S4): serializes the graph to JSON
  so it survives process restarts in CI environments.
- Richer edge types (S9): CALLS, EXTENDS, IMPORTS, DECORATED_BY.
- Multi-hop traversal support (S2): get_call_graph with configurable depth.
- Node metadata includes all chunk fields.
"""
from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path

import networkx as nx

from kitsune_2_0.models.chunk import Chunk

logger = logging.getLogger(__name__)

# Edge types
EDGE_CALLS = "CALLS"
EDGE_EXTENDS = "EXTENDS"          # class inheritance
EDGE_IMPORTS = "IMPORTS"          # module-level import
EDGE_DECORATED_BY = "DECORATED_BY"


class GraphStore:
    """Stores the dependency graph with Neo4j primary + NetworkX fallback.

    If Neo4j is unavailable (e.g. in GitHub Actions), falls back to an
    in-memory NetworkX graph that is PERSISTED to disk as pickle (S4).

    Scalability fixes for large codebases:
    - Name→node_id index for O(1) lookups (was O(V) linear scan)
    - Pickle binary serialization (10x faster than JSON for large graphs)
    """

    def __init__(self, persist_path: str | None = None):
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "kitsune_password")

        self._driver = None
        self.use_fallback = False
        self.nx_graph: nx.DiGraph | None = None
        self._persist_path = persist_path or os.path.join(
            os.getcwd(), "data", "kitsune_2_0", "graph.pkl"
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
        query = """
        CREATE CONSTRAINT chunk_id IF NOT EXISTS
        FOR (c:Chunk) REQUIRE c.id IS UNIQUE
        """
        assert self._driver is not None
        with self._driver.session() as session:
            try:
                session.run(query)
            except Exception as e:
                logger.debug(f"Could not create constraint: {e}")

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
            self.nx_graph.add_node(chunk.node_id, **attrs)
            self._index_node_name(chunk.node_id, attrs)  # maintain O(1) index
            return

        query = """
        MERGE (c:Chunk {id: $id})
        SET c.name = $name, c.qualified_name = $qualified_name,
            c.type = $type, c.file_path = $file_path,
            c.start_line = $start_line, c.end_line = $end_line,
            c.scope = $scope, c.docstring = $docstring,
            c.language = $language, c.hash = $hash
        """
        assert self._driver is not None
        meta = chunk.to_metadata()
        with self._driver.session() as session:
            session.run(query, id=chunk.node_id, **{
                k: meta[k] for k in
                ("name", "qualified_name", "type", "file_path",
                 "start_line", "end_line", "scope", "docstring", "language", "hash")
            })

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
            target_ids = self._find_nodes_by_name(target_name)
            if target_ids:
                target_id = target_ids[0]
            else:
                target_id = f"stub::{target_name}"
                self.nx_graph.add_node(target_id, name=target_name, type="stub")
                self._index_node_name(target_id, {"name": target_name})

            self.nx_graph.add_edge(source_id, target_id, type=edge_type)
            return

        query_map = {
            EDGE_CALLS: "MERGE (caller)-[:CALLS]->(callee)",
            EDGE_EXTENDS: "MERGE (child)-[:EXTENDS]->(parent)",
            EDGE_IMPORTS: "MERGE (importer)-[:IMPORTS]->(module)",
            EDGE_DECORATED_BY: "MERGE (target)-[:DECORATED_BY]->(decorator)",
        }
        query = f"""
        MATCH (source:Chunk {{id: $source_id}})
        MERGE (target:Chunk {{name: $target_name}})
        {query_map.get(edge_type, query_map[EDGE_CALLS])}
        """
        assert self._driver is not None
        with self._driver.session() as session:
            session.run(query, source_id=source_id, target_name=target_name)

    # ── Query operations ─────────────────────────────────────────────────

    def get_dependencies(self, chunk_name: str, depth: int = 1) -> list[str]:
        """Find what a function calls (downstream dependencies).

        Parameters
        ----------
        chunk_name:
            The name or qualified name of the chunk.
        depth:
            How many hops to traverse (S2). 1 = immediate calls only.
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
                        if edge_data and edge_data.get("type") == EDGE_CALLS:
                            name = self.nx_graph.nodes[successor].get("name", "")
                            if name:
                                deps.add(name)
                            next_frontier.append(successor)
                frontier = next_frontier

            return list(deps)

        if not self._driver:
            return []

        # Neo4j: variable-length path traversal
        query = f"""
        MATCH (caller:Chunk)-[:CALLS*1..{depth}]->(callee)
        WHERE caller.name = $name OR caller.qualified_name = $name
        RETURN DISTINCT callee.name AS callee_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name)
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
                        if edge_data and edge_data.get("type") == EDGE_CALLS:
                            name = self.nx_graph.nodes[predecessor].get("name", "")
                            if name:
                                callers.add(name)
                            next_frontier.append(predecessor)
                frontier = next_frontier

            return list(callers)

        if not self._driver:
            return []

        query = f"""
        MATCH (caller)-[:CALLS*1..{depth}]->(callee:Chunk)
        WHERE callee.name = $name OR callee.qualified_name = $name
        RETURN DISTINCT caller.name AS caller_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name)
            return [record["caller_name"] for record in result]

    def get_call_graph(self, entity_name: str, depth: int = 2) -> dict:
        """Return a structured topology of the call graph (S15).

        Unlike Kitsune 1.0 which ignored the depth parameter, this actually
        traverses the graph up to the specified depth.
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
        WHERE c.name = $name OR c.qualified_name = $name
        RETURN c LIMIT 1
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=chunk_name).single()
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
        MATCH (child:Chunk {name: $name})-[:EXTENDS]->(parent)
        RETURN parent.name AS parent_name
        """
        assert self._driver is not None
        with self._driver.session() as session:
            result = session.run(query, name=class_name)
            return [record["parent_name"] for record in result]

    def clear_all(self):
        """Delete all nodes and edges."""
        if self.use_fallback:
            assert self.nx_graph is not None
            self.nx_graph.clear()
            self._save_to_disk()
            return

        if not self._driver:
            return
        query = "MATCH (n) DETACH DELETE n"
        assert self._driver is not None
        with self._driver.session() as session:
            session.run(query)

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

        return {"backend": "neo4j", "nodes": -1, "edges": -1, "edge_types": {}}
