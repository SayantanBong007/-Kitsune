"""Tests for Smart Knowledge Graph: subtoken decomposition, stemming, and autonomous entrypoint discovery."""

import pytest
from kitsune.models.chunk import Chunk
from kitsune.store.graph_store import GraphStore, _split_identifier, _stem


def test_stemmer_and_tokenizer():
    # Test sub-token decomposition
    assert "send" in _split_identifier("send_receipt")
    assert "receipt" in _split_identifier("send_receipt")
    assert "stripe" in _split_identifier("StripePaymentGateway")
    assert "payment" in _split_identifier("StripePaymentGateway")
    assert "gateway" in _split_identifier("StripePaymentGateway")

    # Test stemming
    assert _stem("receipts") == "receipt"
    assert _stem("emailed") == "email"
    assert _stem("passwords") == "password"
    assert _stem("processing") == "process"


def test_smart_graph_entrypoint_discovery(tmp_path):
    graph = GraphStore(persist_path=str(tmp_path / "graph.pkl"))
    graph.clear_all()

    # Create dummy chunks
    c1 = Chunk(
        text="def checkout(): process_payment()",
        chunk_type="function",
        name="checkout",
        file_path="shop/checkout.py",
        start_line=1,
        end_line=5,
        docstring="Handles customer checkout and initiates payment flow.",
    )
    c2 = Chunk(
        text="def process_payment(): send_receipt()",
        chunk_type="function",
        name="process_payment",
        file_path="shop/payment.py",
        start_line=1,
        end_line=5,
        docstring="Processes credit card charge and issues customer receipts.",
    )
    c3 = Chunk(
        text="def send_receipt(): send_email()",
        chunk_type="function",
        name="send_receipt",
        file_path="shop/email.py",
        start_line=1,
        end_line=5,
        docstring="Sends an email receipt to the customer.",
    )
    c4 = Chunk(
        text="def send_email(): smtp.send()",
        chunk_type="function",
        name="send_email",
        file_path="shop/email.py",
        start_line=6,
        end_line=10,
        docstring="Low-level SMTP email sender.",
    )

    graph.upsert_chunk_node(c1)
    graph.upsert_chunk_node(c2)
    graph.upsert_chunk_node(c3)
    graph.upsert_chunk_node(c4)

    # Add calls edges using node_ids
    graph.replace_call_edges([
        (c1.node_id, c2.node_id, False),
        (c2.node_id, c3.node_id, False),
        (c3.node_id, c4.node_id, False),
    ], externals=[])

    # 1. Test search_nodes with natural language query with plural "receipts" & verb "emailed"
    hits = graph.search_nodes("How are receipts emailed to customers?", limit=5)
    assert len(hits) > 0
    assert c3.node_id in hits or c4.node_id in hits

    # 2. Test search_nodes for "checkout work"
    checkout_hits = graph.search_nodes("How does checkout work?", limit=5)
    assert c1.node_id in checkout_hits

    # 3. Test get_dependencies resilience when method parens or unnormalized names are passed
    deps = graph.get_dependencies("checkout()", depth=2)
    assert "process_payment" in deps or "send_receipt" in deps

    # 4. Test get_upstream_callers resilience
    callers = graph.get_upstream_callers("send_receipt()", depth=2)
    assert "process_payment" in callers or "checkout" in callers

