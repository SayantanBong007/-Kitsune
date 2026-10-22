"""
Integration tests against real Qdrant and Neo4j servers.

These WIPE the target Qdrant collection and Neo4j database, so they only run
when explicitly enabled:

    docker run -d -p 6333:6333 qdrant/qdrant
    docker run -d -p 7687:7687 -e NEO4J_AUTH=neo4j/kitsune_password neo4j:5-community
    KITSUNE_INTEGRATION=1 python -m pytest tests/test_integration.py -q

Override endpoints with KITSUNE_TEST_QDRANT_URL / KITSUNE_TEST_NEO4J_URI /
KITSUNE_TEST_NEO4J_PASSWORD.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KITSUNE_INTEGRATION") != "1",
    reason="set KITSUNE_INTEGRATION=1 to run against live Qdrant/Neo4j (wipes them)",
)

QDRANT_URL = os.getenv("KITSUNE_TEST_QDRANT_URL", "http://localhost:6333")
NEO4J_URI = os.getenv("KITSUNE_TEST_NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("KITSUNE_TEST_NEO4J_PASSWORD", "kitsune_password")


def _write(path: Path, code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _write(root / "main.py", """
        from billing import charge_card

        def checkout(cart):
            \"\"\"Checkout flow: validates the cart then charges the card.\"\"\"
            return charge_card(cart.total)
        """)
    _write(root / "billing.py", """
        class Gateway(BaseGateway):
            def send(self, amount):
                return _gateway_request(amount)

        def charge_card(amount):
            \"\"\"Charge a credit card through the payment gateway.\"\"\"
            return _gateway_request(amount)

        def _gateway_request(amount):
            print(amount)
            return {"ok": True, "amount": amount}
        """)
    return root


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("NEO4J_URI", NEO4J_URI)
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", NEO4J_PASSWORD)

    from kitsune.engine import RepositoryKnowledgeEngine
    eng = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data"))
    assert not eng.vector_store.using_fallback, "Qdrant server not reachable"
    assert not eng.graph_store.use_fallback, "Neo4j server not reachable"
    eng.clear_all()
    yield eng
    eng.clear_all()
    eng.graph_store.close()


def test_namespaces_isolate_projects_on_shared_servers(engine, tmp_path):
    from kitsune.engine import RepositoryKnowledgeEngine

    repo_a = _write(tmp_path / "a" / "svc.py", """
        def handler():
            return helper()

        def helper():
            return 1
        """).parent
    repo_b = _write(tmp_path / "b" / "svc.py", """
        def handler():
            return other()

        def other():
            return 2
        """).parent
    proj_a = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "da"), namespace="it-proj-a")
    proj_b = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "db"), namespace="it-proj-b")
    try:
        for eng in (proj_a, proj_b):
            assert not eng.vector_store.using_fallback and not eng.graph_store.use_fallback
            eng.clear_all()
        proj_a.index_repository(str(repo_a))
        proj_b.index_repository(str(repo_b))
        engine.index_repository(str(repo_a))  # default (no namespace) index of repo A too

        # Same function name in both projects — each sees only its own graph/vectors
        assert proj_a.get_downstream_dependencies("handler", depth=1) == ["helper"]
        assert proj_b.get_downstream_dependencies("handler", depth=1) == ["other"]
        assert {r["name"] for r in proj_b.vector_store.get_all()} == {"handler", "other"}
        assert proj_a.graph_store.stats()["nodes"] == proj_b.graph_store.stats()["nodes"] == 2

        # Clearing one project leaves the others intact
        proj_b.clear_all()
        assert proj_b.vector_store.count() == 0 and proj_b.graph_store.stats()["nodes"] == 0
        assert proj_a.get_downstream_dependencies("handler", depth=1) == ["helper"]
        assert engine.get_downstream_dependencies("handler", depth=1) == ["helper"]
    finally:
        for eng in (proj_a, proj_b):
            eng.clear_all()
            eng.vector_store._client.delete_collection(eng.vector_store._collection_name)
            eng.graph_store.close()


def _neo4j(engine, query, **params):
    with engine.graph_store._driver.session() as session:
        return [r.data() for r in session.run(query, **params)]


def test_index_and_query_on_servers(engine, repo):
    metrics = engine.index_repository(str(repo))
    assert metrics.errors == []
    assert engine.vector_store.count() == metrics.total_chunks > 0

    # Neo4j graph: real nodes, cross-file edge resolved, builtins excluded
    assert engine.get_downstream_dependencies("checkout", depth=1) == ["charge_card"]
    assert engine.get_upstream_callers("charge_card", depth=1) == ["checkout"]
    assert sorted(engine.get_upstream_callers("_gateway_request", depth=1)) == ["charge_card", "send"]
    assert set(engine.get_downstream_dependencies("checkout", depth=2)) == {
        "charge_card", "_gateway_request"}
    assert "print" in engine.graph_store.get_dependencies(
        "_gateway_request", include_external=True)
    assert engine.get_inheritance_tree("Gateway") == ["BaseGateway"]
    impact = engine.get_impact_analysis("charge_card")
    assert impact["upstream"] == ["checkout"] and impact["downstream"] == ["_gateway_request"]

    # No anonymous junk nodes (the old query created two per edge)
    assert _neo4j(engine, "MATCH (n) WHERE n.name IS NULL RETURN count(n) AS c")[0]["c"] == 0
    # No leftover stubs for functions that exist in the repo
    stubs = _neo4j(engine, "MATCH (n:Chunk) WHERE n.id IS NULL RETURN n.name AS name")
    assert {"charge_card", "_gateway_request", "checkout"}.isdisjoint({s["name"] for s in stubs})

    # Qdrant: lookups by node_id and name
    some = engine.vector_store.get_by_name("charge_card")[0]
    assert engine.vector_store.get_by_id(some["node_id"])["name"] == "charge_card"
    assert engine.get_source_code("checkout").startswith("def checkout")

    # Full query path
    result = engine.search("what calls charge_card?")
    assert result["query_type"] == "dependency"
    assert "checkout" in result["graph_paths"]["charge_card"]["called_by"]
    assert result["graph_paths"]["locations"]["checkout"].endswith("main.py:4")


def test_incremental_reindex_on_servers(engine, repo):
    engine.index_repository(str(repo))

    _write(repo / "billing.py", """
        def charge_card(amount):
            return _send(amount)

        def _send(amount):
            return amount
        """)
    (repo / "main.py").unlink()
    metrics = engine.index_repository(str(repo))
    assert metrics.errors == []

    names = {r["name"] for r in engine.vector_store.get_all()}
    assert {"charge_card", "_send"} <= names
    assert names.isdisjoint({"checkout", "_gateway_request", "Gateway", "send"})

    assert engine.get_upstream_callers("charge_card") == []
    assert engine.get_downstream_dependencies("charge_card", depth=1) == ["_send"]
    files = {r["f"] for r in _neo4j(engine, "MATCH (c:Chunk {ns: ''}) WHERE c.id IS NOT NULL "
                                            "RETURN DISTINCT c.file_path AS f")}
    assert all(f.endswith("billing.py") for f in files)


def test_caller_indexed_before_callee_relinks(engine, tmp_path):
    """Edges to a not-yet-indexed function must move onto it once it is indexed."""
    root = tmp_path / "order"
    _write(root / "a.py", "def caller():\n    return later_defined()\n")
    engine.index_repository(str(root))
    assert engine.get_downstream_dependencies("caller") == []  # only a stub so far
    assert engine.graph_store.get_dependencies("caller", include_external=True) == [
        "later_defined"]

    _write(root / "b.py", "def later_defined():\n    return 1\n")
    engine.index_repository(str(root))
    assert engine.get_downstream_dependencies("caller") == ["later_defined"]
    assert engine.get_upstream_callers("later_defined") == ["caller"]
    assert _neo4j(engine, "MATCH (n:Chunk {name: 'later_defined'}) WHERE n.id IS NULL "
                          "RETURN count(n) AS c")[0]["c"] == 0
