"""Regression tests for the indexing and retrieval pipeline."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from kitsune.embedder.embedder import embed_batch
from kitsune.parser.chunker import chunk_file


def _write(path: Path, code: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


def _by_name(chunks):
    return {c.qualified_name: c for c in chunks}


# ── Embedding ─────────────────────────────────────────────────────────────────

def test_embed_batch_returns_one_vector_per_text():
    vectors = embed_batch(["def a(): pass", "def b(): pass", "class C: pass"], batch_size=2)
    assert len(vectors) == 3
    assert all(isinstance(v, list) and len(v) == 384 for v in vectors)


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_python_decorated_definitions_are_chunked(tmp_path):
    chunks = chunk_file(_write(tmp_path / "app.py", """
        import os

        @app.route("/pay")
        def pay(amount):
            \"\"\"Charge a card.\"\"\"
            return _charge(amount)

        def _charge(amount):
            return os.getenv("KEY")

        class Account:
            @property
            def balance(self):
                return 1

            @balance.setter
            def balance(self, value):
                self._save(value)

            @staticmethod
            def make():
                return Account()
        """))
    names = _by_name(chunks)

    assert "pay" in names and names["pay"].chunk_type == "function"
    assert names["pay"].decorators == ["app.route"]
    assert names["pay"].docstring == "Charge a card."
    assert "_charge" in names["pay"].calls          # private helpers are kept
    assert "Account.make" in names
    # getter + setter share a name but must get distinct node IDs
    ids = [c.node_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert sum(c.qualified_name == "Account.balance" for c in chunks) == 2


def test_class_chunk_excludes_method_calls(tmp_path):
    chunks = chunk_file(_write(tmp_path / "c.py", """
        @register(name=make_name())
        class Service(Base):
            client = build_client()

            def run(self):
                return self.fetch()
        """))
    names = _by_name(chunks)
    assert names["Service"].calls == {"register", "make_name", "build_client"}
    assert names["Service.run"].calls == {"fetch"}
    # Class chunk is an outline: signature kept, body (indexed separately) elided
    assert "def run(self):" in names["Service"].text
    assert names["Service"].text.rstrip().endswith("...")
    assert "self.fetch()" not in names["Service"].text
    assert "self.fetch()" in names["Service.run"].text


def test_engines_with_different_data_dirs_are_isolated(tmp_path, monkeypatch):
    from kitsune.engine import RepositoryKnowledgeEngine

    monkeypatch.delenv("CHROMA_PATH")  # use each engine's data_dir
    repo_a = _write(tmp_path / "a" / "a.py", "def only_in_a(): pass\n").parent
    repo_b = _write(tmp_path / "b" / "b.py", "def only_in_b(): pass\n").parent
    engine_a = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data_a"))
    engine_b = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data_b"))
    engine_a.index_repository(str(repo_a))
    engine_b.index_repository(str(repo_b))

    assert {r["name"] for r in engine_b.vector_store.get_all()} == {"only_in_b"}

    # A second index of the same repo (fresh data_dir) re-indexes it
    engine_c = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data_c"))
    assert engine_c.index_repository(str(repo_a)).total_files == 1
    # ...and nothing is written into the indexed repo itself
    assert sorted(p.name for p in repo_a.iterdir()) == ["a.py"]


def test_namespaces_share_data_dir_but_not_data(tmp_path, monkeypatch):
    from kitsune.engine import RepositoryKnowledgeEngine

    monkeypatch.delenv("CHROMA_PATH")
    repo_a = _write(tmp_path / "a" / "a.py", "def only_in_a(): pass\n").parent
    repo_b = _write(tmp_path / "b" / "b.py", "def only_in_b(): pass\n").parent
    data = str(tmp_path / "data")
    engine_a = RepositoryKnowledgeEngine(data_dir=data, namespace="proj a")
    engine_b = RepositoryKnowledgeEngine(data_dir=data, namespace="proj-b")
    assert engine_a.namespace == "proj_a"
    engine_a.index_repository(str(repo_a))
    engine_b.index_repository(str(repo_b))

    assert {r["name"] for r in engine_a.vector_store.get_all()} == {"only_in_a"}
    assert {r["name"] for r in engine_b.vector_store.get_all()} == {"only_in_b"}
    engine_b.clear_all()
    assert engine_a.vector_store.count() == 1

    monkeypatch.setenv("KITSUNE_NAMESPACE", "proj a")
    assert RepositoryKnowledgeEngine(data_dir=data).vector_store.count() == 1


def test_multiple_module_blocks_get_unique_ids(tmp_path):
    chunks = chunk_file(_write(tmp_path / "m.py", """
        import os
        def a(): pass
        X = 1
        def b(): pass
        Y = 2
        """))
    ids = [c.node_id for c in chunks]
    assert len(ids) == len(set(ids))


def test_c_function_names(tmp_path):
    chunks = chunk_file(_write(tmp_path / "x.c", """
        static int helper(int x) { return x * 2; }
        int *make(void) { helper(1); return 0; }
        """))
    names = _by_name(chunks)
    assert {"helper", "make"} <= set(names)
    assert "helper" in names["make"].calls


def test_js_exports_and_arrow_functions(tmp_path):
    chunks = chunk_file(_write(tmp_path / "a.js", """
        export function login(user) { return api.post(user); }
        const logout = () => session.clear();
        export const refresh = async () => { await login(1); };
        export default class Store { save() { this.db.write(); } }
        const VERSION = "1";
        """))
    names = _by_name(chunks)
    assert {"login", "logout", "refresh", "Store", "Store.save"} <= set(names)
    assert names["login"].calls == {"post"}
    assert "login" in names["refresh"].calls
    assert "write" in names["Store.save"].calls


def test_js_iife_contents_are_chunked(tmp_path):
    chunks = chunk_file(_write(tmp_path / "content.js", """
        // content script
        (() => {
          let tick = null;
          function startTimer() { tick = setInterval(render, 1000); }
          function stopTimer() { clearInterval(tick); tick = null; render(); }
          const render = () => draw(tick);
        })();
        (function () { function legacy() { startTimer(); } }());
        """))
    names = _by_name(chunks)
    assert {"startTimer", "stopTimer", "render", "legacy"} <= set(names)
    assert "render" in names["stopTimer"].calls


def test_js_module_pattern_is_chunked(tmp_path):
    chunks = chunk_file(_write(tmp_path / "shared.js", """
        const LF = (() => {
          const DAY = 86400000;
          function daysUntil(d) { return Math.ceil((d - Date.now()) / DAY); }
          const plan = (d) => daysUntil(d) * 2;
          return { daysUntil, plan };
        })();
        window.Util = (function () { function slug(s) { return s.trim(); } return { slug }; })();
        """))
    names = _by_name(chunks)
    assert {"LF.daysUntil", "LF.plan", "Util.slug"} <= set(names)
    assert "daysUntil" in names["LF.plan"].calls


def test_long_module_code_is_split(tmp_path):
    lines = "\n".join(f"CONSTANT_{i} = {i}" for i in range(400))  # ~6 KB of top-level code
    chunks = chunk_file(_write(tmp_path / "consts.py", lines + "\n"))
    assert len(chunks) > 1
    assert all(len(c.text) <= 2000 for c in chunks)


def test_java_method_calls(tmp_path):
    chunks = chunk_file(_write(tmp_path / "A.java", """
        class A extends Base {
            A() { init(); }
            void run() { this.helper(); Util.log("x"); }
            void helper() {}
        }
        """))
    names = _by_name(chunks)
    assert names["A"].inherits == ["Base"]
    assert {"helper", "log"} <= names["A.run"].calls
    assert "init" in names["A.A"].calls


def test_go_methods_and_structs(tmp_path):
    chunks = chunk_file(_write(tmp_path / "s.go", """
        package main

        type Server struct { port int }

        func (s *Server) Start() { s.listen(); fmt.Println("x") }
        """))
    names = _by_name(chunks)
    assert "Server" in names and names["Server"].chunk_type == "class"
    assert {"listen", "Println"} <= names["Server.Start"].calls


# ── End-to-end engine (ChromaDB + NetworkX) ──────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    # main.py is indexed in arbitrary order relative to billing.py — call
    # edges must resolve either way.
    _write(root / "main.py", """
        from billing import charge_card

        def checkout(cart):
            \"\"\"Checkout flow: validates the cart then charges the card.\"\"\"
            return charge_card(cart.total)
        """)
    _write(root / "billing.py", """
        def charge_card(amount):
            \"\"\"Charge a credit card through the payment gateway.\"\"\"
            return _gateway_request(amount)

        def _gateway_request(amount):
            print(amount)
            return {"ok": True, "amount": amount}
        """)
    return root


@pytest.fixture
def engine(tmp_path):
    from kitsune.engine import RepositoryKnowledgeEngine
    return RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data"))


def test_index_and_graph(engine, repo):
    metrics = engine.index_repository(str(repo))
    assert metrics.errors == []
    assert metrics.total_files == 2
    assert engine.vector_store.count() == metrics.total_chunks > 0

    # Cross-file edge resolves to the real node, not a stub, regardless of order
    assert "charge_card" in engine.get_downstream_dependencies("checkout")
    assert "checkout" in engine.get_upstream_callers("charge_card")
    assert set(engine.get_downstream_dependencies("checkout", depth=2)) >= {
        "charge_card", "_gateway_request"}
    # Builtins/external calls (`print`) are not counted as code dependencies
    assert engine.get_impact_analysis("charge_card") == {
        "function": "charge_card",
        "upstream": ["checkout"],
        "downstream": ["_gateway_request"],
        "total_impact": 2,
    }
    assert "print" not in engine.get_downstream_dependencies("checkout", depth=3)
    assert "print" in engine.graph_store.get_dependencies(
        "_gateway_request", include_external=True)


def test_dependency_query_returns_context(engine, repo):
    engine.index_repository(str(repo))
    result = engine.search("what calls charge_card?")
    assert result["query_type"] == "dependency"
    assert result["primary_chunks"], "graph_only queries need semantic entry points"
    # Upstream caller is traversed and its code included (as primary or dependency)
    assert "checkout" in result["graph_paths"]["charge_card"]["called_by"]
    context_names = {c["name"] for c in result["primary_chunks"] + result["dependency_chunks"]}
    assert "checkout" in context_names


def test_keyword_chunks_carry_text(engine, repo):
    engine.index_repository(str(repo))
    hits = engine.keyword_store.search("gateway request amount", k=5)
    assert hits and all(h["text"] for h in hits)


def test_incremental_reindex_cleans_up(engine, repo):
    engine.index_repository(str(repo))

    # Rename a function and delete a file
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
    assert "_gateway_request" not in names and "checkout" not in names
    assert "_send" in names
    kw_names = {d["metadata"]["name"] for d in engine.keyword_store._corpus.values()}
    assert "_gateway_request" not in kw_names and "checkout" not in kw_names
    assert engine.get_upstream_callers("charge_card") == []
    assert engine.get_downstream_dependencies("charge_card") == ["_send"]

    hashes = json.loads(engine.indexer._hash_file(repo).read_text())
    assert set(hashes) == {"billing.py"}


def test_failed_files_are_retried(engine, repo, monkeypatch):
    from kitsune import indexer as indexer_mod

    real_chunk_file = indexer_mod.chunk_file

    def flaky(path):
        if Path(path).name == "main.py":
            raise RuntimeError("boom")
        return real_chunk_file(path)

    monkeypatch.setattr(indexer_mod, "chunk_file", flaky)
    metrics = engine.index_repository(str(repo))
    assert metrics.errors
    hashes = json.loads(engine.indexer._hash_file(repo).read_text())
    assert "main.py" not in hashes  # not marked as indexed → retried next run

    monkeypatch.setattr(indexer_mod, "chunk_file", real_chunk_file)
    metrics = engine.index_repository(str(repo))
    assert metrics.total_files == 1 and metrics.errors == []


def test_cache_cleared_after_reindex_and_not_mutated(engine, repo):
    engine.index_repository(str(repo))
    engine.search("how does charge_card work?")
    hit1 = engine.search("how does charge_card work?")
    hit2 = engine.search("how does charge_card work?")
    assert hit2["query_type"] == "concept (cached)"
    assert hit1["metrics"].query_type == "concept (cached)"

    engine.index_repository(str(repo))
    assert engine.cache.stats()["size"] == 0


# ── Query cache ───────────────────────────────────────────────────────────────

def test_semantic_cache_requires_same_identifiers(tmp_path):
    from kitsune.query.cache import QueryCache

    cache = QueryCache(persist_path=str(tmp_path / "cache.pkl"))
    cache._cache.clear()
    vec = [1.0] + [0.0] * 383
    cache.put("what calls get_user?", {"answer": "user"}, query_vector=vec)

    assert cache.get("who calls get_user", query_vector=vec) == {"answer": "user"}
    assert cache.get("what calls get_order?", query_vector=vec) is None


# ── Qdrant (embedded mode) ───────────────────────────────────────────────────

def test_qdrant_store_roundtrip(tmp_path, monkeypatch):
    from kitsune.models.chunk import Chunk
    from kitsune.store.qdrant_store import QdrantVectorStore

    monkeypatch.setenv("QDRANT_LOCAL_PATH", str(tmp_path / "qdrant"))
    store = QdrantVectorStore()
    assert not store.using_fallback

    chunk = Chunk(text="def pay(): pass", chunk_type="function", name="pay",
                  file_path="a.py", start_line=1, end_line=1)
    vectors = embed_batch([chunk.text])
    store.upsert_batch([chunk], vectors)

    assert store.count() == 1
    assert store.search(vectors[0], k=1)[0]["name"] == "pay"
    assert store.get_by_id(chunk.node_id)["name"] == "pay"
    assert list(store.get_hashes()) == [chunk.node_id]
    store.delete_by_file("a.py")
    assert store.count() == 0


# ── LLM settings ──────────────────────────────────────────────────────────────

def test_reasoning_effort_only_for_reasoning_models(monkeypatch):
    from kitsune.llm import llm_extra_kwargs

    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    assert llm_extra_kwargs("openai/gpt-oss-20b") == {"reasoning_effort": "low"}
    assert llm_extra_kwargs("gpt-4o-mini") == {}
    assert llm_extra_kwargs("llama3.1:8b") == {}
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    assert llm_extra_kwargs("openai/gpt-oss-20b") == {}
