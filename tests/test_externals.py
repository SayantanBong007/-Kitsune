"""
Tests for External Library Semantic Resolution & Categorization in Kitsune.
"""
from kitsune.graph.library_catalog import classify_library
from kitsune.graph.resolver import CallResolver
from kitsune.store.graph_store import GraphStore


def test_library_catalog_classification():
    stripe_info = classify_library("stripe")
    assert stripe_info.package == "stripe"
    assert stripe_info.category == "Payment Gateway"
    assert "Network" in stripe_info.side_effect
    assert stripe_info.risk_level == "high"

    redis_info = classify_library("redis")
    assert redis_info.category == "Cache Store"
    assert "In-Memory" in redis_info.side_effect

    sql_info = classify_library("sqlalchemy")
    assert sql_info.category == "Database ORM"


def test_external_library_call_resolution(tmp_path):
    module_chunk = {
        "node_id": "billing/checkout.py::<module>",
        "name": "<module>",
        "qualified_name": "<module>",
        "file_path": "billing/checkout.py",
        "resolve": {
            "chunk_type": "module",
            "scope": "",
            "inherits": [],
            "decorators": [],
            "call_refs": [],
            "import_map": {"stripe": "stripe", "redis": "redis"},
        }
    }

    caller_chunk = {
        "node_id": "billing/checkout.py::process_order",
        "name": "process_order",
        "qualified_name": "process_order",
        "file_path": "billing/checkout.py",
        "resolve": {
            "chunk_type": "function",
            "scope": "",
            "inherits": [],
            "decorators": [],
            "call_refs": ["stripe.Charge|create", "redis|set"],
            "var_types": {},
        }
    }

    definitions = [module_chunk, caller_chunk]
    resolver = CallResolver(definitions)
    resolution = resolver.resolve_all()

    # Verify that external symbols preserve library provenance
    ext_symbols = [name for src, name in resolution.externals]
    assert any("stripe" in s and "create" in s for s in ext_symbols)
    assert any("redis" in s and "set" in s for s in ext_symbols)

    # Test GraphStore integration
    from kitsune.models.chunk import Chunk
    graph = GraphStore(persist_path=str(tmp_path / "graph.pkl"))
    chunk = Chunk(
        text="def process_order(): pass",
        chunk_type="function",
        name="process_order",
        file_path="billing/checkout.py",
        start_line=1,
        end_line=10,
    )
    graph.upsert_chunk_node(chunk)
    graph.replace_call_edges(resolution.edges, resolution.externals)
    services = graph.get_external_services("process_order")

    categories = {s["category"] for s in services}
    assert "Payment Gateway" in categories
    assert "Cache Store" in categories
