"""Optional languages (Kotlin, C#, PHP) and Java enums.

The optional grammars load only when their packages are installed
(tree-sitter-kotlin, tree-sitter-c-sharp, tree-sitter-php); their tests skip otherwise.
"""
from __future__ import annotations

import textwrap

import pytest

from kitsune.parser.chunker import chunk_file
from kitsune.parser.treesitter_parser import LANGUAGES


def _chunks(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return {c.qualified_name: c for c in chunk_file(path)}


def _need(ext):
    if ext not in LANGUAGES:
        pytest.skip(f"grammar for {ext} not installed")


def test_java_enum_and_its_methods_are_chunked(tmp_path):
    c = _chunks(tmp_path, "Status.java", """
        public enum PaymentStatus {
            NEW, PAID;
            public boolean isFinal() { return this == PAID; }
        }
    """)
    assert c["PaymentStatus"].chunk_type == "class"
    assert "PaymentStatus.isFinal" in c


def test_csharp(tmp_path):
    _need(".cs")
    c = _chunks(tmp_path, "OrderController.cs", """
        namespace Shop.Orders
        {
            [ApiController]
            [Route("api/orders")]
            public class OrderController : ControllerBase
            {
                private readonly IOrderService _service;
                [HttpGet("{id}")]
                public Order Get(string id) => _service.Find(id);
            }
            public enum Status { New, Paid }
        }
    """)
    ctrl = c["OrderController"].resolve_info()
    assert ctrl["decorators"] == ["ApiController", "Route"]
    assert ctrl["inherits"] == ["ControllerBase"]
    get = c["OrderController.Get"].resolve_info()
    assert get["decorators"] == ["HttpGet"] and get["call_refs"] == ["_service|Find"]
    assert "Status" in c   # inside the namespace block, not one big module chunk


def test_kotlin(tmp_path):
    _need(".kt")
    c = _chunks(tmp_path, "OrderController.kt", """
        class OrderController(private val service: OrderService) : BaseController() {
            @GetMapping("/{id}")
            fun get(id: String): Order = service.find(id)
        }
        object Ids { fun next(): String = UUID.randomUUID().toString() }
    """)
    assert c["OrderController"].resolve_info()["inherits"] == ["BaseController"]
    get = c["OrderController.get"].resolve_info()
    assert get["decorators"] == ["GetMapping"] and get["call_refs"] == ["service|find"]
    assert "Ids.next" in c


def test_php(tmp_path):
    _need(".php")
    c = _chunks(tmp_path, "RatingsController.php", """
        <?php
        class RatingsController extends BaseController
        {
            public function show($sku)
            {
                return json_encode($this->repo->find($sku));
            }
        }
        function helper($x) { return Util::clamp($x); }
    """)
    assert c["RatingsController"].resolve_info()["inherits"] == ["BaseController"]
    assert c["RatingsController.show"].resolve_info()["call_refs"] == ["this.repo|find", "|json_encode"]
    assert c["helper"].resolve_info()["call_refs"] == ["Util|clamp"]
