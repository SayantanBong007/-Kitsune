"""
Tests for Autonomous External Library Discovery & Dynamic Catalog Generation.
"""
import json
import tempfile
from pathlib import Path

from kitsune.graph.library_catalog import (
    DynamicLibraryCatalog,
    GLOBAL_CATALOG,
    classify_library,
)
from kitsune.graph.resolver import CallResolver
from kitsune.models.chunk import Chunk
from kitsune.store.graph_store import GraphStore


def test_proprietary_library_autonomous_classification():
    catalog = DynamicLibraryCatalog()

    # 1. Simulate an unknown company payment SDK: @acme/billing-gateway
    catalog.record_call(
        package="@acme/billing-gateway",
        method="charge",
        caller_node="services/checkout.ts::processOrder",
        caller_file="services/checkout.ts",
        receiver_var="paymentClient",
    )
    catalog.record_call(
        package="@acme/billing-gateway",
        method="refund",
        caller_node="services/checkout.ts::cancelOrder",
        caller_file="services/checkout.ts",
        receiver_var="paymentClient",
    )

    info = catalog.classify("@acme/billing-gateway")
    assert info.category == "Payment Gateway"
    assert info.risk_level == "critical"
    assert "charge" in info.discovered_methods
    assert "refund" in info.discovered_methods
    assert "services/checkout.ts" in info.callers
    assert info.confidence >= 0.7


def test_proprietary_auth_vault_classification():
    catalog = DynamicLibraryCatalog()

    # 2. Simulate an unknown internal security vault: corp_vault_client
    catalog.record_call(
        package="corp_vault_client",
        method="verifyToken",
        caller_node="middleware/auth.py::require_auth",
        caller_file="middleware/auth.py",
        receiver_var="vault",
    )

    info = catalog.classify("corp_vault_client")
    assert info.category == "Authentication & Security"
    assert info.risk_level == "high"
    assert "verifyToken" in info.discovered_methods


def test_proprietary_event_bus_classification():
    catalog = DynamicLibraryCatalog()

    # 3. Simulate an unknown internal event broker: internal_bus
    catalog.record_call(
        package="internal_bus",
        method="publish",
        caller_node="workers/events.py::emit_event",
        caller_file="workers/events.py",
        receiver_var="eventQueue",
    )

    info = catalog.classify("internal_bus")
    assert info.category == "Message Broker"
    assert info.risk_level == "medium"
    assert "publish" in info.discovered_methods


def test_manifest_mining(tmp_path):
    catalog = DynamicLibraryCatalog()

    # Create dummy package.json
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(
        json.dumps({
            "name": "enterprise-app",
            "dependencies": {
                "@corp/secrets-manager": "1.0.0",
            },
            "description": "Secure vault authentication and secrets tokens",
        }),
        encoding="utf-8"
    )

    catalog.scan_manifests(tmp_path)
    assert "@corp/secrets-manager" in catalog._manifest_packages

    # Classify based on manifest and package name tokens
    info = catalog.classify("@corp/secrets-manager")
    assert info.category == "Authentication & Security"


def test_end_to_end_resolver_and_graph_store_catalog(tmp_path):
    # Reset global catalog state
    GLOBAL_CATALOG.clear()

    # 1. Module chunk with an unknown proprietary import: @company/pay
    module_chunk = {
        "node_id": "billing/pay.py::<module>",
        "name": "<module>",
        "qualified_name": "<module>",
        "file_path": "billing/pay.py",
        "resolve": {
            "chunk_type": "module",
            "scope": "",
            "inherits": [],
            "decorators": [],
            "call_refs": [],
            "import_map": {"pay_sdk": "@company/pay"},
        }
    }

    # 2. Caller calling pay_sdk.charge
    caller_chunk = {
        "node_id": "billing/pay.py::execute_payment",
        "name": "execute_payment",
        "qualified_name": "execute_payment",
        "file_path": "billing/pay.py",
        "resolve": {
            "chunk_type": "function",
            "scope": "",
            "inherits": [],
            "decorators": [],
            "call_refs": ["pay_sdk|charge"],
            "var_types": {},
        }
    }

    resolver = CallResolver([module_chunk, caller_chunk])
    resolution = resolver.resolve_all()

    # Verify external call was registered with package provenance
    ext_symbols = [name for src, name in resolution.externals]
    assert any("@company/pay::charge" in s for s in ext_symbols)

    # 3. Store into GraphStore
    graph = GraphStore(persist_path=str(tmp_path / "graph.pkl"))
    chunk = Chunk(
        text="def execute_payment(): pass",
        chunk_type="function",
        name="execute_payment",
        file_path="billing/pay.py",
        start_line=1,
        end_line=5,
    )
    graph.upsert_chunk_node(chunk)
    graph.replace_call_edges(resolution.edges, resolution.externals)

    # 4. Query external services
    services = graph.get_external_services("execute_payment")
    assert len(services) > 0
    pay_service = next(s for s in services if "@company/pay" in s["package"])
    assert pay_service["category"] == "Payment Gateway"
    assert pay_service["risk"] == "critical"
    assert "charge" in pay_service["side_effect"]
    assert pay_service["confidence"] >= 0.7


def test_catalog_export(tmp_path):
    catalog = DynamicLibraryCatalog()
    catalog.record_call(
        package="@company/cache-lib",
        method="get",
        caller_node="app.py::get_user",
        caller_file="app.py",
    )
    catalog.record_call(
        package="@company/cache-lib",
        method="set",
        caller_node="app.py::save_user",
        caller_file="app.py",
    )

    export_path = tmp_path / "catalog.json"
    exported_file = catalog.export_catalog_json(export_path)

    assert Path(exported_file).exists()
    data = json.loads(Path(exported_file).read_text(encoding="utf-8"))
    assert "@company/cache-lib" in data
    assert data["@company/cache-lib"]["category"] == "Cache Store"
    assert "get" in data["@company/cache-lib"]["discovered_methods"]
    assert "set" in data["@company/cache-lib"]["discovered_methods"]
