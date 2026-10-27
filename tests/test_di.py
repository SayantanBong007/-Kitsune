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
