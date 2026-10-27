"""Multi-service support: discovery, contract extraction and cross-service linking.

Each fixture is a tiny system that reproduces a pattern seen in public
microservice projects (Sock Shop, Online Boutique, Spring PetClinic, Robot Shop).
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from kitsune.indexer import _is_generated
from kitsune.services import discover_services, extract_contracts, link_contracts
from kitsune.services.contracts import normalize_path, parse_url, paths_match


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def _links(root: Path) -> set[tuple]:
    services = discover_services(str(root))
    links = link_contracts(extract_contracts(services), list(services))
    return {(l.client_service, l.server_service, l.kind, l.method, l.path, l.confidence) for l in links}


# ── discovery ─────────────────────────────────────────────────────────────────

def test_discovery_uses_manifests_and_skips_infra(tmp_path):
    _write(tmp_path / "orders" / "pom.xml", "<project><artifactId>orders</artifactId></project>")
    _write(tmp_path / "cartservice" / "src" / "package.json", '{"name": "cart"}')
    _write(tmp_path / "deploy" / "helm" / "package.json", '{"name": "charts"}')
    services = discover_services(str(tmp_path))
    assert "orders" in services
    assert "cartservice" in services          # generic "src" folder takes the parent's name
    assert not any("helm" in n or "deploy" in n or "charts" in n for n in services)


def test_spring_application_name_is_the_service_name(tmp_path):
    svc = tmp_path / "spring-petclinic-vets-service"
    _write(svc / "pom.xml", "<project><artifactId>spring-petclinic-vets-service</artifactId></project>")
    _write(svc / "src" / "main" / "resources" / "application.yml",
           "spring:\n  application:\n    name: vets-service\n")
    assert "vets-service" in discover_services(str(tmp_path))


# ── URL evaluation ────────────────────────────────────────────────────────────

def test_path_helpers():
    assert normalize_path("/orders/{id}/items/") == "/orders/{*}/items"
    assert normalize_path("/carts/5f3a9c1b2d4e") == "/carts/{*}"
    target, path, looks = parse_url("http://payment/paymentAuth", {"payment": "payment"})
    assert (target, path, looks) == ("payment", "/paymentAuth", True)
    assert paths_match("/carts/{*}/items", "/carts/{*}/items", False) > \
        paths_match("/carts/{*}/items", "/carts/{*}/{*}", False)


def test_format_arguments_are_substituted(tmp_path):
    """String.format("http://%s/shipping/", getenv("CART_ENDPOINT", "cart")) → cart."""
    _write(tmp_path / "cart" / "package.json", '{"name": "cart"}')
    _write(tmp_path / "cart" / "server.js", """
        const express = require('express');
        const app = express();
        app.post('/shipping/:id', (req, res) => res.send('ok'));
    """)
    _write(tmp_path / "shipping" / "pom.xml", "<project><artifactId>shipping</artifactId></project>")
    _write(tmp_path / "shipping" / "src" / "main" / "java" / "CartHelper.java", """
        public class CartHelper {
            private String baseUrl;
            public CartHelper(String baseUrl) { this.baseUrl = baseUrl; }
            public String addToCart(String id) {
                HttpPost post = new HttpPost(baseUrl + id);
                return client.execute(post);
            }
        }
    """)
    _write(tmp_path / "shipping" / "src" / "main" / "java" / "Controller.java", """
        public class Controller {
            private String CART_URL = String.format("http://%s/shipping/", getenv("CART_ENDPOINT", "cart"));
            private CartHelper helper = new CartHelper(CART_URL);
        }
    """)
    assert ("shipping", "cart", "http", "POST", "/shipping/{*}", "confirmed") in _links(tmp_path)


def test_each_construction_site_keeps_its_own_values(tmp_path):
    """new ServiceUri("payment", ...) and new ServiceUri("shipping", ...) must not mix."""
    _write(tmp_path / "payment" / "main.go", """
        package main
        func main() { http.HandleFunc("/paymentAuth", authorize) }
    """)
    _write(tmp_path / "payment" / "go.mod", "module payment\n")
    _write(tmp_path / "shipping" / "pom.xml", "<project><artifactId>shipping</artifactId></project>")
    _write(tmp_path / "shipping" / "src" / "main" / "java" / "Ship.java", """
        @RestController
        public class Ship {
            @RequestMapping(value = "/shipping", method = RequestMethod.POST)
            public String create() { return "ok"; }
        }
    """)
    _write(tmp_path / "orders" / "pom.xml", "<project><artifactId>orders</artifactId></project>")
    _write(tmp_path / "orders" / "src" / "main" / "java" / "ServiceUri.java", """
        public class ServiceUri {
            private final String hostname;
            private final String endpoint;
            public ServiceUri(String hostname, String endpoint) {
                this.hostname = hostname;
                this.endpoint = endpoint;
            }
            public URI toUri() { return URI.create("http://" + hostname + endpoint); }
        }
    """)
    _write(tmp_path / "orders" / "src" / "main" / "java" / "Orders.java", """
        public class Orders {
            public URI paymentUri() { return new ServiceUri("payment", "/paymentAuth").toUri(); }
            public URI shippingUri() { return new ServiceUri("shipping", "/shipping").toUri(); }
            public void place() {
                restTemplate.postForObject(paymentUri(), auth, Payment.class);
                restTemplate.postForObject(shippingUri(), shipment, Shipment.class);
            }
        }
    """)
    found = {(c, s, path) for c, s, _, _, path, conf in _links(tmp_path) if conf == "confirmed"}
    assert ("orders", "payment", "/paymentAuth") in found
    assert ("orders", "shipping", "/shipping") in found
    assert not any(path in ("/shippingshipping/paymentAuth", "/paymentAuth") and s == "shipping"
                   for _, s, path in found)


def test_env_default_and_no_link_to_unknown_host(tmp_path):
    _write(tmp_path / "catalogue" / "package.json", '{"name": "catalogue"}')
    _write(tmp_path / "catalogue" / "server.js", """
        const app = require('express')();
        app.get('/products', (req, res) => res.json([]));
    """)
    _write(tmp_path / "web" / "package.json", '{"name": "web"}')
    _write(tmp_path / "web" / "client.js", """
        const host = process.env.CATALOGUE_HOST || 'catalogue';
        function list() { return axios.get('http://' + host + ':8080/products'); }
        function ext() { return axios.get('https://api.github.com/products'); }
    """)
    links = _links(tmp_path)
    assert ("web", "catalogue", "http", "GET", "/products", "confirmed") in links
    assert len(links) == 1                     # the github.com call links nowhere


# ── generated code ────────────────────────────────────────────────────────────

def test_generated_marker_only_counts_in_comments(tmp_path):
    gen = _write(tmp_path / "a.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage pb\n")
    java = _write(tmp_path / "B.java", "/**\n * @generated\n */\nclass B {}\n")
    mention = _write(tmp_path / "c.py", 'MARK = "@generated"   # what generated files contain\n')
    assert _is_generated(gen) and _is_generated(java)
    assert not _is_generated(mention)
