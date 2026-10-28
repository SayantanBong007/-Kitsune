"""The `kitsune` command, end to end on local stores."""
from __future__ import annotations

import json
import textwrap

from kitsune.cli import main


def _write(path, code):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code).lstrip(), encoding="utf-8")


def _run(capsys, *argv) -> str:
    assert main(list(argv)) == 0
    return capsys.readouterr().out


def test_index_then_query_from_the_command_line(tmp_path, capsys):
    repo = tmp_path / "repo"
    _write(repo / "billing.py", """
        def charge_card(amount):
            return _gateway(amount)

        def _gateway(amount):
            return amount
    """)
    _write(repo / "main.py", """
        from billing import charge_card

        def checkout(cart):
            return charge_card(cart.total)
    """)
    common = ["--namespace", "shop", "--data-dir", str(tmp_path / "data")]
    assert "chunks" in _run(capsys, "index", str(repo), *common)
    assert "checkout" in _run(capsys, "callers", "charge_card", *common)
    rows = json.loads(_run(capsys, "callees", "checkout", "--depth", "2", "--json", *common))
    assert {r["name"] for r in rows} == {"charge_card", "_gateway"}
    assert "tail" in _run(capsys, "age", *common)


def test_system_links_and_teaching(tmp_path, capsys):
    root = tmp_path / "sys"
    _write(root / "cart" / "package.json", '{"name": "cart"}')
    _write(root / "cart" / "server.js", """
        const app = require('express')();
        app.get('/items', (req, res) => res.json([]));
    """)
    _write(root / "web" / "package.json", '{"name": "web"}')
    _write(root / "web" / "client.js", """
        function list() { return axios.get('http://basket-api/items'); }
    """)
    common = ["--namespace", "shop", "--data-dir", str(tmp_path / "data")]
    _run(capsys, "index-system", str(root), *common)
    before = json.loads(_run(capsys, "links", "--service", "cart", "--direction", "in", "--json", *common))
    assert not [l for l in before if l["confidence"] == "confirmed"]
    _run(capsys, "teach", "host", "basket-api", "cart", *common)
    after = json.loads(_run(capsys, "links", "--service", "cart", "--direction", "in", "--json", *common))
    assert [(l["client_service"], l["confidence"]) for l in after] == [("web", "confirmed")]
    assert "web → cart" in _run(capsys, "map", *common)


def test_teach_link_needs_all_parts(capsys):
    assert main(["teach", "link", "a", "b", "http", "GET"]) == 2
