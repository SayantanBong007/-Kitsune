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


def _train_ticket_style(tmp_path) -> Path:
    """RestTemplate.exchange with HttpMethod, getServiceUrl helpers, if/else targets,
    a base URL passed as a parameter, a class prefix without "/", a gateway with
    Spring placeholders, and an unused URL property in plain config."""
    java = "src/main/java"
    for name in ("order", "order-other", "travel", "travel2", "preserve", "gateway"):
        _write(tmp_path / name / "pom.xml", f"<project><artifactId>{name}</artifactId></project>")
    _write(tmp_path / "order" / java / "OrderController.java", """
        @RestController
        @RequestMapping("api/v1/orderservice")
        public class OrderController {
            @PostMapping("/order") public Response create() { return null; }
            @GetMapping("/order/{id}") public Response get(@PathVariable String id) { return null; }
            @DeleteMapping("/order/{id}") public Response delete(@PathVariable String id) { return null; }
        }
    """)
    _write(tmp_path / "order-other" / java / "OtherController.java", """
        @RestController
        @RequestMapping("/api/v1/orderOtherService")
        public class OtherController {
            @PostMapping("/orderOther") public Response create() { return null; }
        }
    """)
    for name, svc in (("travel", "travelservice"), ("travel2", "travel2service")):
        _write(tmp_path / name / java / "TripController.java", f"""
            @RestController
            @RequestMapping("/api/v1/{svc}")
            public class TripController {{
                @PostMapping("/trip_detail") public Response detail() {{ return null; }}
            }}
        """)
    _write(tmp_path / "travel" / "src/main/resources/application.yml",
           "order:\n  url: http://${ORDER_HOST:order}/api/v1/orderservice/order\n")
    _write(tmp_path / "preserve" / java / "PreserveService.java", """
        public class PreserveService {
            private String getServiceUrl(String serviceName) { return "http://" + serviceName; }

            public void preserve(String tripId, String id) {
                String order_url = getServiceUrl("order");
                ResponseEntity<Response> re = restTemplate.exchange(
                        order_url + "/api/v1/orderservice/order", HttpMethod.POST, entity, Response.class);
                Response body = re.getBody();
                restTemplate.exchange(order_url + "/api/v1/orderservice/order/" + id,
                        HttpMethod.DELETE, entity, Response.class);
                String requestUrl = "";
                if (tripId.startsWith("G")) {
                    requestUrl = getServiceUrl("travel") + "/api/v1/travelservice/trip_detail";
                } else {
                    requestUrl = getServiceUrl("travel2") + "/api/v1/travel2service/trip_detail";
                }
                restTemplate.exchange(requestUrl, HttpMethod.POST, entity, Response.class);
                doOther(getServiceUrl("order-other"), entity);
            }

            private Response doOther(String url, HttpEntity e) {
                return restTemplate.exchange(url + "/api/v1/orderOtherService/orderOther",
                        HttpMethod.POST, e, Response.class).getBody();
            }
        }
    """)
    _write(tmp_path / "gateway" / "src/main/resources/application.yml", """
        spring:
          cloud:
            gateway:
              routes:
                - id: order
                  uri: lb://${ORDER_SERVICE_HOST:order}
                  predicates:
                    - Path=/api/v1/orderservice/**
    """)
    _write(tmp_path / "gateway" / java / "Main.java", """
        public class Main { static final String ROUTE = "/(.*)"; void f() { http.Get(ROUTE); } }
    """)
    return tmp_path


def test_spring_rest_template_patterns(tmp_path):
    links = _links(_train_ticket_style(tmp_path))
    confirmed = {(c, s, m, p) for c, s, _, m, p, conf in links if conf == "confirmed"}
    assert ("preserve", "order", "POST", "/api/v1/orderservice/order") in confirmed       # HttpMethod.POST
    assert ("preserve", "order", "DELETE", "/api/v1/orderservice/order/{*}") in confirmed  # prefix w/o "/"
    assert ("preserve", "travel", "POST", "/api/v1/travelservice/trip_detail") in confirmed    # if branch
    assert ("preserve", "travel2", "POST", "/api/v1/travel2service/trip_detail") in confirmed  # else branch
    assert ("preserve", "order-other", "POST", "/api/v1/orderOtherService/orderOther") in confirmed  # parameter
    assert not any(c == "preserve" and m == "GET" for c, _, m, _ in confirmed)   # re.getBody() is no call


def test_gateway_routes_but_not_plain_url_properties(tmp_path):
    links = _links(_train_ticket_style(tmp_path))
    assert any(c == "gateway" and s == "order" for c, s, *_ in links)       # lb://${..:order}
    assert not any(c == "travel" and s == "order" for c, s, *_ in links)    # order.url is just a property
    assert not any(c == "gateway" and p == "/{*}" for c, _, _, _, p, _ in links)  # "/(.*)" is a regex


# ── generated code ────────────────────────────────────────────────────────────

def test_generated_marker_only_counts_in_comments(tmp_path):
    gen = _write(tmp_path / "a.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage pb\n")
    java = _write(tmp_path / "B.java", "/**\n * @generated\n */\nclass B {}\n")
    mention = _write(tmp_path / "c.py", 'MARK = "@generated"   # what generated files contain\n')
    assert _is_generated(gen) and _is_generated(java)
    assert not _is_generated(mention)


def test_feign_client_is_a_call_not_an_offer(tmp_path):
    java = "src/main/java"
    _write(tmp_path / "customers" / "pom.xml", "<project><artifactId>customers</artifactId></project>")
    _write(tmp_path / "customers" / "src/main/resources/application.yml",
           "spring:\n  application:\n    name: customers-service\n")
    _write(tmp_path / "customers" / java / "OwnerResource.java", """
        @RestController
        @RequestMapping("/owners")
        public class OwnerResource {
            @GetMapping("/{ownerId}") public Owner findOwner(@PathVariable int ownerId) { return null; }
        }
    """)
    _write(tmp_path / "visits" / "pom.xml", "<project><artifactId>visits</artifactId></project>")
    _write(tmp_path / "visits" / java / "CustomersClient.java", """
        @FeignClient(name = "customers-service", path = "/owners")
        public interface CustomersClient {
            @GetMapping("/{id}") Owner getOwner(@PathVariable("id") int id);
        }
    """)
    services = discover_services(str(tmp_path))
    contracts = extract_contracts(services)
    assert not any(o.service == "visits" for o in contracts.offers)    # the client serves nothing
    links = _links(tmp_path)
    assert ("visits", "customers-service", "http", "GET", "/owners/{*}", "confirmed") in links


def test_spring_cloud_config_server_context_path_oauth2_and_routes(tmp_path):
    """PiggyMetrics-style: per-service config served by a config server (shared/<app>.yml),
    a context-path prefix, an external Feign API, OAuth2 settings, and a Zuul gateway."""
    java = "src/main/java"
    shared = tmp_path / "config" / "src/main/resources/shared"
    _write(tmp_path / "config" / "pom.xml", "<project><artifactId>config</artifactId></project>")
    _write(shared / "application.yml",
           "security:\n  oauth2:\n    resource:\n      user-info-uri: http://auth-service:5000/uaa/users/current\n")
    _write(shared / "account-service.yml", "server:\n  servlet:\n    context-path: /accounts\n")
    _write(shared / "statistics-service.yml",
           "server:\n  servlet:\n    context-path: /statistics\nrates:\n  url: https://api.exchangeratesapi.io\n")
    _write(shared / "gateway.yml",
           "spring:\n  cloud:\n    config:\n      uri: http://config:8888\n"
           "zuul:\n  routes:\n    accounts:\n      path: /accounts/**\n      serviceId: account-service\n")
    for name in ("account-service", "statistics-service", "auth-service", "gateway"):
        _write(tmp_path / name / "pom.xml", f"<project><artifactId>{name}</artifactId></project>")
        _write(tmp_path / name / "src/main/resources/bootstrap.yml",
               f"spring:\n  application:\n    name: {name}\n  cloud:\n    config:\n      uri: http://config:8888\n")
    _write(tmp_path / "auth-service" / java / "UserController.java", """
        @RestController
        @RequestMapping("/users")
        public class UserController { @GetMapping("/current") public Principal get(Principal p) { return p; } }
    """)
    _write(tmp_path / "account-service" / java / "AccountController.java", """
        @RestController
        public class AccountController { @GetMapping("/{name}") public Account get(@PathVariable String name) { return null; } }
    """)
    _write(tmp_path / "statistics-service" / java / "Clients.java", """
        @EnableResourceServer
        public class Config {}
        @FeignClient(name = "account-service")
        interface AccountClient { @GetMapping("/accounts/{name}") Account get(@PathVariable("name") String n); }
        @FeignClient(url = "${rates.url}", name = "rates-client")
        interface RatesClient { @GetMapping("/latest") Rates latest(); }
    """)
    links = _links(tmp_path)
    pairs = {(c, s) for c, s, *_ in links}
    # context-path from the config server puts AccountController under /accounts
    assert ("statistics-service", "account-service", "http", "GET", "/accounts/{*}", "confirmed") in links
    assert ("gateway", "account-service") in pairs           # Zuul route served by the config server
    assert ("gateway", "config") not in pairs                # spring.cloud.config.uri is a setting
    assert ("statistics-service", "auth-service") in pairs   # OAuth2 resource server → user-info-uri
    assert ("account-service", "auth-service") not in pairs  # no OAuth2 in its code
    assert len([l for l in links if l[0] == "statistics-service"]) == 2   # external rates API: no link
