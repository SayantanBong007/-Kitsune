"""Regression tests for bugs found in review of the DI / catalog / smart-graph changes."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kitsune.graph.library_catalog import DynamicLibraryCatalog
from kitsune.graph.resolver import CallResolver


def _write(path: Path, code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "shop"
    _write(root / "models.py", """
        import db

        class BaseModel:
            def save(self):
                self.validate()

            def validate(self):
                return True

        class Order(BaseModel):
            def validate(self):
                return bool(self.items)

        class StripeGateway:
            def charge(self, amount):
                return db.insert("charges", {"amount": amount})
        """)
    _write(root / "db.py", """
        def insert(table, row):
            return row
        """)
    _write(root / "service.py", """
        from models import Order, StripeGateway

        gateway = StripeGateway()

        def pay(order):
            gateway.charge(order.total)
            order.save()
        """)
    return root


@pytest.fixture
def engine(tmp_path):
    from kitsune.engine import RepositoryKnowledgeEngine
    return RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data"))


def test_unknown_symbols_return_nothing(engine, shop):
    """Graph answers must never fall back to a *different* symbol."""
    engine.index_repository(str(shop))
    for symbol in ("refund_customer", "Foo.save", "pays", "charge_cards"):
        assert engine.get_upstream_callers(symbol) == []
        assert engine.get_downstream_dependencies(symbol) == []
        assert engine.graph_store.get_related(symbol, "callers") == []
    # Case and "()" are still accepted for real symbols
    assert engine.get_upstream_callers("StripeGateway.Charge()", depth=1) == ["pay"]


def test_inheritance_edges_in_local_mode(engine, shop):
    engine.index_repository(str(shop))
    assert engine.get_inheritance_tree("Order") == ["BaseModel"]


def test_base_class_keeps_its_own_method(engine, shop):
    """self.validate() inside BaseModel is BaseModel.validate, not the subclass override."""
    engine.index_repository(str(shop))
    callees = {r["qualified_name"] for r in engine.graph_store.get_related("BaseModel.save", "callees")}
    assert callees == {"BaseModel.validate"}


def test_rename_then_lookup_old_name_does_not_crash(engine, shop):
    engine.index_repository(str(shop))
    models = shop / "models.py"
    models.write_text(models.read_text().replace("StripeGateway", "PaymentGateway"))
    service = shop / "service.py"
    service.write_text(service.read_text().replace("StripeGateway", "PaymentGateway"))
    engine.index_repository(str(shop))
    for symbol in ("StripeGateway", "stripegateway", "StripeGateway.charge"):
        assert engine.graph_store.get_related(symbol, "callers") == []
        assert engine.get_downstream_dependencies(symbol) == []
    assert engine.graph_store.search_nodes("stripe gateway") == [] or all(
        "Stripe" not in n for n in engine.graph_store.search_nodes("stripe gateway"))


def test_search_results_have_no_duplicate_chunks(engine, shop):
    engine.index_repository(str(shop))
    for question in ("How does paying for an order work?", "How is a charge stored?"):
        result = engine.search(question)
        chunks = result["primary_chunks"] + result["dependency_chunks"] + result["keyword_chunks"]
        keys = [(c.get("file_path"), c.get("qualified_name"), c.get("start_line")) for c in chunks]
        assert len(keys) == len(set(keys)), question


def test_externals_recorded_once_and_only_imported_packages_cataloged():
    module = {"node_id": "a.py::<module>", "name": "<module>", "qualified_name": "<module>",
              "file_path": "a.py", "resolve": {"chunk_type": "module", "call_refs": [],
                                               "import_map": {"requests": "requests"}}}
    fn = {"node_id": "a.py::f", "name": "f", "qualified_name": "f", "file_path": "a.py",
          "resolve": {"chunk_type": "function", "scope": "",
                      "call_refs": ["requests|post", "items|update", "|len"]}}
    catalog = DynamicLibraryCatalog()
    names = sorted(n for _, n in CallResolver([module, fn], catalog=catalog).resolve_all().externals)
    assert names == ["len", "requests::post", "update"]  # no duplicate bare "post"
    assert set(catalog.get_catalog()) == {"requests"}      # `items` is not a library


def test_catalog_does_not_invent_risky_roles():
    catalog = DynamicLibraryCatalog()
    assert catalog.classify("json", method="dumps").category == "Standard Library"
    catalog.record_call("json", "dumps", "x", "payments/payment_processor.py", "json")
    assert catalog.classify("json", method="dumps").category == "Standard Library"
    assert catalog.classify("requests", method="post").category == "HTTP Client"
    catalog.record_call("acme_utils", "update", "x", "a.py", "acme_utils")
    assert catalog.classify("acme_utils", method="update").category == "Third-Party Library"


def test_external_names_drop_call_arguments():
    """Mockito.when(restTemplate.exchange(<kilobytes>)).thenReturn(...) must not put the
    whole expression in a node name: Neo4j refuses to index values over ~8 KB."""
    long_args = ", ".join(f"Mockito.any(Type{i}.class)" for i in range(500))
    src = {"node_id": "T.java::T.test", "name": "test", "qualified_name": "T.test",
           "file_path": "T.java",
           "resolve": {"chunk_type": "method", "scope": "", "inherits": [], "decorators": [],
                       "call_refs": [f"Mockito.when(restTemplate.exchange({long_args}))|thenReturn"],
                       "import_map": {"Mockito": "org.mockito.Mockito"}}}
    externals = CallResolver([src]).resolve_all().externals
    assert externals == [("T.java::T.test", "org.mockito.Mockito::when.thenReturn")]


def test_projects_have_separate_catalogs(tmp_path):
    from kitsune.engine import RepositoryKnowledgeEngine
    a = _write(tmp_path / "a" / "x.py", "import stripe\n\ndef pay():\n    stripe.Charge.create()\n").parent
    b = _write(tmp_path / "b" / "y.py", "import redis\n\ndef cache():\n    redis.set('k', 1)\n").parent
    ea = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "da"))
    eb = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "db"))
    ea.index_repository(str(a))
    eb.index_repository(str(b))
    assert set(ea.get_library_catalog()) == {"stripe"}
    assert set(eb.get_library_catalog()) == {"redis"}


def test_require_servers_refuses_to_fall_back(tmp_path, monkeypatch):
    """With KITSUNE_REQUIRE_SERVERS=1 an unreachable server is an error, not a silent
    switch to private local stores (conftest points Neo4j at a closed port)."""
    from kitsune.engine import RepositoryKnowledgeEngine
    monkeypatch.setenv("KITSUNE_REQUIRE_SERVERS", "1")
    with pytest.raises(RuntimeError, match="Neo4j"):
        RepositoryKnowledgeEngine(data_dir=str(tmp_path / "d"))
