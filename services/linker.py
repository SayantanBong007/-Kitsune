"""
Linker — connects calls in one service to offers in another.

  grpc   proto service + rpc. If no indexed service implements it (e.g. a service
         in an unsupported language), the call is linked to the service whose
         name matches the proto service ("CartService" → cartservice), without
         a handler.
  http   target service (from the URL host) + method + path. With an unknown
         target, a path served by exactly one other service gives a *possible* link.
         A known target with an unknown path gives a service-level link.
  queue  topic/queue name: every producer → every consumer of that name.

Every link carries a confidence: "confirmed" or "possible".
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from kitsune.services.contracts import Call, Offer, ServiceContracts, paths_match
from kitsune.services.discovery import service_aliases


@dataclass
class Link:
    kind: str            # http | grpc | queue
    client_service: str
    client_node: str     # chunk node_id of the calling function ("" if unknown)
    client_symbol: str
    client_file: str
    client_line: int
    server_service: str
    server_node: str     # chunk node_id of the handler ("" if not indexed)
    server_symbol: str
    server_file: str
    server_line: int
    method: str
    path: str            # endpoint called / "pkg.Service/Rpc" / topic
    confidence: str      # confirmed | possible
    reason: str
    route: str = ""      # the handler's route pattern, if different (e.g. a catch-all prefix)

    def to_dict(self) -> dict:
        return asdict(self)


def _http_verb_ok(call: Call, offer: Offer) -> bool:
    return call.method == "ANY" or offer.method == "ANY" or call.method == offer.method


def link_contracts(contracts: ServiceContracts, services: list[str]) -> list[Link]:
    offers_by_service: dict[str, list[Offer]] = {}
    for o in contracts.offers:
        offers_by_service.setdefault(o.service, []).append(o)
    alias_to_service = {}
    for s in services:
        for a in service_aliases(s):
            alias_to_service.setdefault(a, s)

    links: list[Link] = []

    # Routes that only forward to another service (an API gateway / BFF route that
    # itself calls service X with the same path) are not the real handler: when
    # the target host is unknown, they must not win over X.
    forwarding = [(c.service, c.path) for c in contracts.calls
                  if c.kind == "http" and c.target and c.target != c.service and c.path]

    def is_forwarding(o: Offer) -> bool:
        return any(s == o.service and paths_match(p, o.path, o.prefix) >= 0 for s, p in forwarding)

    def add(call: Call, offer: Offer | None, server_service: str, confidence: str, reason: str,
            path: str | None = None, method: str | None = None):
        if path is None and call.kind == "http" and call.path:
            path = call.path   # the endpoint actually called, not the matching route pattern
        method = method or (offer.method if offer and offer.method != "ANY" else call.method)
        links.append(Link(
            call.kind, call.service, call.node_id, call.symbol, call.file, call.line,
            server_service,
            offer.node_id if offer else "", offer.symbol if offer else "",
            offer.file if offer else "", offer.line if offer else 0,
            method,
            path if path is not None else (offer.path if offer else call.path),
            confidence, reason,
            offer.path + ("*" if offer.prefix else "") if offer and offer.path != path else ""))

    for call in contracts.calls:
        if call.kind == "grpc":
            impls = [o for o in contracts.offers if o.kind == "grpc" and o.path == call.path
                     and o.service != call.service]
            if any(o.service == call.service for o in contracts.offers
                   if o.kind == "grpc" and o.path == call.path):
                continue  # a service calling its own API (demo/test client)
            if impls:
                for o in impls:
                    add(call, o, o.service, "confirmed", "gRPC stub call to implemented rpc")
            else:
                short = call.path.split("/")[0].split(".")[-1].lower()
                target = alias_to_service.get(short, "")
                if target and target != call.service:
                    add(call, None, target, "confirmed",
                        "gRPC call; implementation not indexed (unsupported language?)")

        elif call.kind == "queue":
            for o in contracts.offers:
                if o.kind == "queue" and o.path == call.path and o.service != call.service:
                    add(call, o, o.service, "confirmed", f"publishes to '{call.path}'",
                        method="publish")

        elif call.kind == "http":
            if call.target and call.target != call.service:
                candidates = offers_by_service.get(call.target, [])
                if not call.path:
                    if call.raw.startswith("route:"):
                        add(call, None, call.target, "confirmed",
                            f"gateway route in config ({call.raw[6:]})", path="")
                    else:
                        add(call, None, call.target, "possible",
                            "HTTP call to this service; path only known at runtime", path="")
                    continue
                scored = [(paths_match(call.path, o.path, o.prefix), o) for o in candidates
                          if o.kind == "http" and _http_verb_ok(call, o)]
                scored = [(s, o) for s, o in scored if s >= 0]
                if scored:
                    best = max(s for s, _ in scored)
                    top = [o for s, o in scored if s == best]
                    # Among equals prefer the exact verb, then concrete routes; a call
                    # with an unknown verb (a wrapper like simpleHttpRequest) is
                    # almost always a fetch, so prefer GET for it.
                    exact = [o for o in top if o.method == call.method and not o.prefix] \
                        or ([o for o in top if o.method == "GET"] if call.method == "ANY" else []) \
                        or [o for o in top if not o.prefix] or top
                    if len(exact) > 1 and len({(o.method, o.path) for o in exact}) == 1:
                        exact = exact[:1]
                    conf = "confirmed" if len(exact) == 1 else "possible"
                    for o in exact:
                        add(call, o, call.target, conf,
                            "HTTP call matched by host, method and path"
                            + ("" if conf == "confirmed" else " (several routes match)"))
                else:
                    add(call, None, call.target, "possible",
                        "HTTP call to this service; no matching route found", path=call.path)
            elif not call.target and call.path.replace("{*}", "").strip("/"):
                # host unknown: only a path with real words in it can identify a service
                matches = []
                for svc, offers in offers_by_service.items():
                    if svc == call.service:
                        continue
                    for o in offers:
                        if o.kind == "http" and _http_verb_ok(call, o) and not is_forwarding(o):
                            s = paths_match(call.path, o.path, o.prefix)
                            if s >= 0:
                                matches.append((s, o))
                if matches:
                    best = max(s for s, _ in matches)
                    top = [o for s, o in matches if s == best]
                    if len({o.service for o in top}) == 1:
                        exact = [o for o in top if o.method == call.method and not o.prefix] or top
                        for o in exact[:3]:
                            add(call, o, o.service, "possible",
                                "HTTP path served by exactly one other service (URL host unknown)")
    # de-duplicate identical links
    seen, unique = set(), []
    for link in links:
        # client_service too: a shared config file (Spring Cloud Config's application.yml)
        # gives several services a link from the same file and line
        key = (link.kind, link.client_service, link.client_node or link.client_file, link.client_line,
               link.server_service, link.server_node, link.method, link.path)
        if key not in seen:
            seen.add(key)
            unique.append(link)
    return unique


def service_map(links: list[Link]) -> dict:
    """Aggregate links into service → service edges."""
    edges: dict[tuple, dict] = {}
    for link in links:
        key = (link.client_service, link.server_service, link.kind)
        e = edges.setdefault(key, {"from": link.client_service, "to": link.server_service,
                                   "kind": link.kind, "endpoints": set(), "confirmed": 0,
                                   "possible": 0})
        e["endpoints"].add(f"{link.method} {link.path}".strip())
        e[link.confidence] += 1
    result = []
    for e in edges.values():
        e["endpoints"] = sorted(e["endpoints"])
        result.append(e)
    return {"edges": sorted(result, key=lambda e: (e["from"], e["to"], e["kind"]))}
