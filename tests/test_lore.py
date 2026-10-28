"""Kitsune grows with age: lessons taught by a person, habits learned across systems."""
from __future__ import annotations

import textwrap
from pathlib import Path

from kitsune.services import discover_services, extract_contracts, link_contracts
from kitsune.services.lore import Lore


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def _node_service(root: Path, name: str, server: str = "", client: str = ""):
    _write(root / name / "package.json", f'{{"name": "{name}"}}')
    if server:
        _write(root / name / "server.js", server)
    if client:
        _write(root / name / "client.js", client)


def _run(root: Path, lore: Lore, system: str):
    """One indexing run's linking, as the engine does it."""
    services = discover_services(str(root))
    contracts = extract_contracts(services, hints=lore.hints(system))
    for kind, value, service in contracts.observations:
        lore.observe(kind, value, f"{system}/{service}")
    links = lore.apply_verdicts(system, link_contracts(contracts, list(services)))
    return {(l.client_service, l.server_service, l.path, l.confidence) for l in links}, contracts


ITEMS = "const app = require('express')();\napp.get('/items', (req, res) => res.json([]));\n"
USERS = "const app = require('express')();\napp.get('/users', (req, res) => res.json([]));\n"


def test_env_suffix_habit_carries_to_the_next_system(tmp_path):
    lore = Lore(tmp_path / "lore.json")
    # System A: this team writes service addresses as <SERVICE>_GATEWAY, with defaults
    a = tmp_path / "a"
    _node_service(a, "cart", server=ITEMS)
    _node_service(a, "user", server=USERS)
    _node_service(a, "web", client="const h = process.env.CART_GATEWAY || 'cart';\n"
                                   "function f() { return axios.get('http://' + h + '/items'); }\n")
    _node_service(a, "orders", client="const u = process.env.USER_GATEWAY || 'user';\n"
                                      "function g() { return axios.get('http://' + u + '/users'); }\n")
    _run(a, lore, "a")
    assert "GATEWAY" in lore.learned("env_suffix")

    # System B: same convention, no defaults — only the learned habit can place it
    b = tmp_path / "b"
    _node_service(b, "cart", server=ITEMS)
    _node_service(b, "shop", client="function f() { return axios.get('http://' + process.env.CART_GATEWAY"
                                    " + '/items'); }\n")
    young, _ = _run(b, Lore(tmp_path / "empty.json"), "b")
    grown, _ = _run(b, lore, "b")
    assert ("shop", "cart", "/items", "confirmed") not in young
    assert ("shop", "cart", "/items", "confirmed") in grown


def test_one_service_is_not_enough_to_form_a_habit(tmp_path):
    lore = Lore(tmp_path / "lore.json")
    a = tmp_path / "a"
    _node_service(a, "cart", server=ITEMS)
    _node_service(a, "web", client="const h = process.env.CART_GATEWAY || 'cart';\n"
                                   "function f() { return axios.get('http://' + h + '/items'); }\n")
    _run(a, lore, "a")
    assert "GATEWAY" not in lore.learned("env_suffix")
    assert "GATEWAY" in lore.age()["habits_forming"]["env_suffix"]


def test_in_house_http_helper_is_learned(tmp_path):
    lore = Lore(tmp_path / "lore.json")
    a = tmp_path / "a"
    _node_service(a, "cart", server=ITEMS)
    _node_service(a, "user", server=USERS)
    _node_service(a, "web", client="function f() { return callService('http://cart/items'); }\n")
    _node_service(a, "orders", client="function g() { return callService('http://user/users'); }\n")
    _run(a, lore, "a")
    assert "callService" in lore.learned("http_client")

    # System B: the helper adds the base URL itself; the path still identifies the service
    b = tmp_path / "b"
    _node_service(b, "cart", server=ITEMS)
    _node_service(b, "shop", client="function f() { return callService('/items'); }\n")
    young, contracts = _run(b, Lore(tmp_path / "empty.json"), "b")
    grown, _ = _run(b, lore, "b")
    assert not any(c == "shop" for c, *_ in young)
    assert any(b["what"].startswith("callService(") for b in contracts.blind_spots)
    assert ("shop", "cart", "/items", "possible") in grown


def test_taught_lessons_reject_and_confirm_links(tmp_path):
    from kitsune.services.lore import link_key
    lore = Lore(tmp_path / "lore.json")
    root = tmp_path / "sys"
    _node_service(root, "cart", server=ITEMS)
    _node_service(root, "user", server=USERS)
    _node_service(root, "web", client="function f() { return axios.get('http://cart/items'); }\n"
                                      "function g() { return axios.get('/users'); }\n")
    before, _ = _run(root, lore, "s")
    assert ("web", "user", "/users", "possible") in before

    lore.teach_link("s", link_key("web", "cart", "http", "GET", "/items"), correct=False)
    lore.teach_link("s", link_key("web", "user", "http", "GET", "/users"), correct=True)
    lore.save()
    after, _ = _run(root, Lore(tmp_path / "lore.json"), "s")    # lessons survive a restart
    assert not any(s == "cart" for _, s, *_ in after)
    assert ("web", "user", "/users", "confirmed") in after
    # lessons belong to their system
    assert ("web", "cart", "/items", "confirmed") in _run(root, lore, "other-system")[0]


def test_taught_host_and_env(tmp_path):
    lore = Lore(tmp_path / "lore.json")
    root = tmp_path / "sys"
    _node_service(root, "cart", server=ITEMS)
    _node_service(root, "web", client="function f() { return axios.get('http://basket-api/items'); }\n"
                                      "function g() { return axios.get('http://' + process.env.PAY_GW"
                                      " + '/items'); }\n")
    _, contracts = _run(root, lore, "s")
    assert not any(b["kind"] == "env" for b in contracts.blind_spots)   # PAY_GW: not an address name
    lore.teach_host("s", "basket-api", "cart")
    lore.teach_env("s", "PAY_GW", "cart")
    links, _ = _run(root, lore, "s")
    assert ("web", "cart", "/items", "confirmed") in links
    assert lore.age()["taught_lessons"] == 2


def test_tails_grow_with_lessons(tmp_path):
    lore = Lore(tmp_path / "lore.json")
    assert lore.age()["tails"] == 1
    for i in range(7):
        lore.teach_host("s", f"alias{i}", "cart")
    assert lore.age()["tails"] == 4
    assert Lore(tmp_path / "missing" / "lore.json").age()["tails"] == 1


def test_engine_remembers_lessons_across_reindex(tmp_path):
    from kitsune.engine import RepositoryKnowledgeEngine
    root = tmp_path / "sys"
    _node_service(root, "cart", server=ITEMS)
    _node_service(root, "web", client="function f() { return axios.get('http://basket-api/items'); }\n")
    engine = RepositoryKnowledgeEngine(data_dir=str(tmp_path / "data"), namespace="shop")
    engine.index_system(str(root))
    first = engine.kitsune_age()
    assert not [l for l in engine.get_service_links("cart", "in") if l["confidence"] == "confirmed"]

    engine.teach_host("basket-api", "cart")
    assert [l for l in engine.get_service_links("cart", "in") if l["confidence"] == "confirmed"]
    engine.index_system(str(root))             # lesson is applied again on re-index
    assert [l for l in engine.get_service_links("cart", "in") if l["confidence"] == "confirmed"]
    assert engine.kitsune_age()["lessons"] > first["lessons"]


def test_merge_keeps_what_both_sides_learned():
    a, b = Lore.__new__(Lore), Lore.__new__(Lore)
    empty = lambda: {"version": 1, "systems": {}, "taught": {}, "observed": {}, "blind_spots": {}}  # noqa: E731
    a.data, b.data = empty(), empty()
    a.teach_host("s", "basket-api", "cart")
    b.teach_env("s", "PAY_GW", "payment")
    a.observe("env_suffix", "GATEWAY", "x/web")
    b.observe("env_suffix", "GATEWAY", "y/orders")
    merged = Lore.merge(a.data, b.data)
    assert merged["taught"]["s"]["hosts"] == {"basket-api": "cart"}
    assert merged["taught"]["s"]["env"] == {"PAY_GW": "payment"}
    assert merged["observed"]["env_suffix"]["GATEWAY"] == ["x/web", "y/orders"]
