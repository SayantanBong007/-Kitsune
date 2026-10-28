"""
Tests for Dependency Injection (DI) and Interface Bean Resolution in Kitsune.
"""
from kitsune.graph.resolver import CallResolver


def test_dependency_injection_resolution():
    # 1. Interface definition
    interface_chunk = {
        "node_id": "api/PaymentGateway.java::PaymentGateway",
        "name": "PaymentGateway",
        "qualified_name": "PaymentGateway",
        "file_path": "api/PaymentGateway.java",
        "resolve": {
            "chunk_type": "class",
            "scope": "",
            "inherits": [],
            "decorators": [],
            "call_refs": [],
        }
    }

    # 2. Concrete implementation with @Service
    impl_class_chunk = {
        "node_id": "services/StripePaymentGateway.java::StripePaymentGateway",
        "name": "StripePaymentGateway",
        "qualified_name": "StripePaymentGateway",
        "file_path": "services/StripePaymentGateway.java",
        "resolve": {
            "chunk_type": "class",
            "scope": "",
            "inherits": ["PaymentGateway"],
            "decorators": ["Service"],
            "call_refs": [],
        }
    }
    impl_method_chunk = {
        "node_id": "services/StripePaymentGateway.java::StripePaymentGateway.charge",
        "name": "charge",
        "qualified_name": "StripePaymentGateway.charge",
        "file_path": "services/StripePaymentGateway.java",
        "resolve": {
            "chunk_type": "method",
            "scope": "StripePaymentGateway",
            "inherits": [],
            "decorators": [],
            "call_refs": [],
        }
    }

    # 3. Caller controller with @Autowired field
    controller_class_chunk = {
        "node_id": "controllers/OrderController.java::OrderController",
        "name": "OrderController",
        "qualified_name": "OrderController",
        "file_path": "controllers/OrderController.java",
        "resolve": {
            "chunk_type": "class",
            "scope": "",
            "inherits": [],
            "decorators": ["RestController"],
            "field_types": {"gateway": "PaymentGateway"},
            "call_refs": [],
        }
    }
    controller_method_chunk = {
        "node_id": "controllers/OrderController.java::OrderController.checkout",
        "name": "checkout",
        "qualified_name": "OrderController.checkout",
        "file_path": "controllers/OrderController.java",
        "resolve": {
            "chunk_type": "method",
            "scope": "OrderController",
            "inherits": [],
            "decorators": ["PostMapping"],
            "call_refs": ["this.gateway|charge"],
            "var_types": {},
        }
    }

    definitions = [
        interface_chunk,
        impl_class_chunk,
        impl_method_chunk,
        controller_class_chunk,
        controller_method_chunk,
    ]

    resolver = CallResolver(definitions)
    resolution = resolver.resolve_all()

    # The caller OrderController.checkout MUST link directly to StripePaymentGateway.charge!
    expected_edge = (
        "controllers/OrderController.java::OrderController.checkout",
        "services/StripePaymentGateway.java::StripePaymentGateway.charge",
        False,  # Unambiguous (only 1 bean implements PaymentGateway)
    )

    assert expected_edge in resolution.edges, f"Expected {expected_edge} in {resolution.edges}"


def _definitions(paths):
    from kitsune.parser.chunker import chunk_file
    return [{"node_id": c.node_id, "name": c.name, "qualified_name": c.qualified_name,
             "file_path": c.file_path, "chunk_type": c.chunk_type, "language": c.language,
             "resolve": c.resolve_info()}
            for p in paths for c in chunk_file(p)]


def test_dependency_injection_from_real_java_source(tmp_path):
    """Annotations parsed from Java source (they sit in the class's `modifiers`; a bare
    @Service is a marker_annotation) must reach the resolver: the interface call goes
    to the @Service implementation."""
    (tmp_path / "OrderService.java").write_text(
        "public interface OrderService { Response create(Order o); }\n", encoding="utf-8")
    (tmp_path / "OrderServiceImpl.java").write_text(
        "@Service\npublic class OrderServiceImpl implements OrderService {\n"
        "    public Response create(Order o) { return null; }\n}\n", encoding="utf-8")
    (tmp_path / "OrderController.java").write_text(
        "@RestController\npublic class OrderController {\n"
        "    @Autowired private OrderService orderService;\n"
        "    @PostMapping(\"/order\")\n"
        "    public Response createNewOrder(Order o) { return orderService.create(o); }\n}\n",
        encoding="utf-8")
    defs = _definitions(sorted(tmp_path.glob("*.java")))
    decorators = {d["qualified_name"]: d["resolve"]["decorators"] for d in defs}
    assert decorators["OrderServiceImpl"] == ["Service"]
    assert decorators["OrderController.createNewOrder"] == ["PostMapping"]
    names = {d["node_id"]: d["qualified_name"] for d in defs}
    src = next(d["node_id"] for d in defs if d["qualified_name"] == "OrderController.createNewOrder")
    targets = [names[t] for s, t, _ in CallResolver(defs).resolve_all().edges if s == src]
    assert targets == ["OrderServiceImpl.create"]


def test_field_with_a_generic_name_uses_its_declared_type(tmp_path):
    """`service.getTrip()` in Java is this.service: the field's type decides, even when
    the name says nothing about the class (unlike orderService ≈ OrderService)."""
    (tmp_path / "TravelService.java").write_text(
        "public interface TravelService { Trip getTrip(String id); }\n", encoding="utf-8")
    (tmp_path / "TravelServiceImpl.java").write_text(
        "@Service\npublic class TravelServiceImpl implements TravelService {\n"
        "    public Trip getTrip(String id) { return null; }\n}\n", encoding="utf-8")
    (tmp_path / "TravelController.java").write_text(
        "@RestController\npublic class TravelController {\n"
        "    @Autowired private TravelService service;\n"
        "    public Trip getTrip(String id) { return service.getTrip(id); }\n}\n", encoding="utf-8")
    defs = _definitions(sorted(tmp_path.glob("*.java")))
    names = {d["node_id"]: d["qualified_name"] for d in defs}
    src = next(d["node_id"] for d in defs if d["qualified_name"] == "TravelController.getTrip")
    edges = [(names[t], amb) for s, t, amb in CallResolver(defs).resolve_all().edges if s == src]
    assert edges == [("TravelServiceImpl.getTrip", False)]


def test_python_decorators_are_read(tmp_path):
    (tmp_path / "views.py").write_text(
        "@app.route('/pay')\n@login_required\ndef pay():\n    return 1\n", encoding="utf-8")
    pay = next(d for d in _definitions([tmp_path / "views.py"]) if d["name"] == "pay")
    assert pay["resolve"]["decorators"] == ["app.route", "login_required"]


def _gateways(tmp_path, extra_impl_annotation: str, field_annotation: str):
    (tmp_path / "PaymentGateway.java").write_text(
        "public interface PaymentGateway { void charge(); }\n", encoding="utf-8")
    (tmp_path / "StripeGateway.java").write_text(
        f"@Service(\"stripe\")\n{extra_impl_annotation}\npublic class StripeGateway implements PaymentGateway {{\n"
        "    public void charge() {}\n}\n", encoding="utf-8")
    (tmp_path / "PaypalGateway.java").write_text(
        "@Service\npublic class PaypalGateway implements PaymentGateway {\n"
        "    public void charge() {}\n}\n", encoding="utf-8")
    (tmp_path / "Checkout.java").write_text(
        "@Service\npublic class Checkout {\n"
        f"    @Autowired {field_annotation} private PaymentGateway gateway;\n"
        "    public void pay() { gateway.charge(); }\n}\n", encoding="utf-8")
    defs = _definitions(sorted(tmp_path.glob("*.java")))
    names = {d["node_id"]: d["qualified_name"] for d in defs}
    src = next(d["node_id"] for d in defs if d["qualified_name"] == "Checkout.pay")
    return sorted((names[t], amb) for s, t, amb in CallResolver(defs).resolve_all().edges if s == src)


def test_two_beans_without_a_hint_stay_ambiguous(tmp_path):
    assert _gateways(tmp_path, "", "") == [("PaypalGateway.charge", True), ("StripeGateway.charge", True)]


def test_qualifier_picks_the_named_bean(tmp_path):
    assert _gateways(tmp_path, "", '@Qualifier("paypalGateway")') == [("PaypalGateway.charge", False)]
    assert _gateways(tmp_path, "", '@Qualifier("stripe")') == [("StripeGateway.charge", False)]


def test_primary_bean_wins(tmp_path):
    assert _gateways(tmp_path, "@Primary", "") == [("StripeGateway.charge", False)]
