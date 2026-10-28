"""
Render report images (SVG) for a multi-service system indexed in Neo4j.

Reads the graph Kitsune stored in Neo4j (Chunk nodes, HTTP_CALLS / GRPC_CALLS /
MESSAGES edges between functions) and draws:

  service-map.svg          every service and who calls whom
  impact-<service>.svg     everyone that depends on one service, with endpoints
  fanout-<service>.svg     everything one service calls, with endpoints
  trace-<name>.svg         function-level path across services (file:line)

Usage:
    python kitsune/eval/render_system_images.py --namespace train_ticket \
        --out kitsune/docs/images/train-ticket --data-dir <engine data dir> \
        --impact ts-order-service --fanout ts-preserve-service

Pure Python + the neo4j driver; no plotting libraries needed.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

# Reference palette (light surface): ink, chrome and categorical slots in fixed order
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
LANG_ORDER = ["Java", "Python", "Go", "JavaScript"]          # slot order = legend order
FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'
EDGE_TYPES = ("HTTP_CALLS", "GRPC_CALLS", "MESSAGES")


def short(service: str) -> str:
    s = service[3:] if service.startswith("ts-") else service
    return s[:-8] if s.endswith("-service") else s


def svg(width: int, height: int, body: list[str], title: str) -> str:
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        '<defs>'
        f'<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>'
        f'<marker id="arrq" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{SLOTS[6]}"/></marker>'
        f'<marker id="arrh" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{SLOTS[1]}"/></marker>'
        '</defs>',
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>',
        *body, "</svg>"])


def text(x, y, s, size=12, color=INK, anchor="start", weight="normal"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" {FONT} font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(str(s))}</text>')


def node_box(x, y, w, h, label, color, sub=None, bold=False):
    out = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" rx="6" fill="#ffffff" '
           f'stroke="{color}" stroke-width="1.6"/>',
           f'<circle cx="{x + 11:.1f}" cy="{y + h / 2:.1f}" r="4" fill="{color}"/>',
           text(x + 21, y + (h / 2 + 4 if sub is None else 16), label, 12, INK, weight="600" if bold else "normal")]
    if sub:
        out.append(text(x + 21, y + 31, sub, 10.5, INK2))
    return out


def legend(x, y, items, dashed_queue=False):
    out, cx = [], x
    for label, color in items:
        out += [f'<circle cx="{cx + 5}" cy="{y - 4}" r="5" fill="{color}"/>', text(cx + 15, y, label, 12, INK2)]
        cx += 26 + 7.2 * len(label)
    out += [f'<line x1="{cx}" y1="{y - 4}" x2="{cx + 34}" y2="{y - 4}" stroke="{MUTED}" stroke-width="1.5" '
            f'marker-end="url(#arr)"/>', text(cx + 42, y, "HTTP call", 12, INK2)]
    cx += 120
    if dashed_queue:
        out += [f'<line x1="{cx}" y1="{y - 4}" x2="{cx + 34}" y2="{y - 4}" stroke="{SLOTS[6]}" '
                f'stroke-width="1.8" stroke-dasharray="5 4" marker-end="url(#arrq)"/>',
                text(cx + 42, y, "message queue", 12, INK2)]
    return out


# ── data ──────────────────────────────────────────────────────────────────────

class GraphData:
    def __init__(self, ns: str):
        from neo4j import GraphDatabase
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        auth = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "kitsune_password"))
        self.driver = GraphDatabase.driver(uri, auth=auth, notifications_min_severity="OFF")
        self.ns = ns

    def q(self, cypher: str, **params):
        with self.driver.session() as s:
            return s.run(cypher, ns=self.ns, **params).data()

    def service_edges(self):
        return self.q(f"""
            MATCH (a:Chunk {{ns: $ns}})-[r:{'|'.join(EDGE_TYPES)}]->(b:Chunk {{ns: $ns}})
            WHERE a.service IS NOT NULL AND b.service IS NOT NULL AND a.service <> b.service
            RETURN a.service AS src, b.service AS dst, type(r) AS kind,
                   collect(DISTINCT r.method + ' ' + r.path) AS endpoints,
                   sum(CASE WHEN r.ambiguous THEN 0 ELSE 1 END) AS confirmed""")

    def languages(self):
        rows = self.q("""MATCH (c:Chunk {ns: $ns}) WHERE c.service IS NOT NULL AND c.language IS NOT NULL
                         RETURN c.service AS s, c.language AS lang, count(*) AS n""")
        best = {}
        for r in rows:
            if r["s"] not in best or r["n"] > best[r["s"]][1]:
                best[r["s"]] = (r["lang"], r["n"])
        names = {"java": "Java", "python": "Python", "go": "Go", "javascript": "JavaScript",
                 "typescript": "JavaScript"}
        return {s: names.get(l.lower(), l) for s, (l, _) in best.items()}

    def link(self, src_service, dst_service, method=None, path=None):
        return self.q(f"""
            MATCH (a:Chunk {{ns: $ns, service: $src}})-[r:{'|'.join(EDGE_TYPES)}]->(b:Chunk {{ns: $ns, service: $dst}})
            WHERE ($method IS NULL OR r.method = $method) AND ($path IS NULL OR r.path = $path)
            RETURN a.id AS aid, a.qualified_name AS a, a.file_path AS afile, a.start_line AS aline,
                   b.id AS bid, b.qualified_name AS b, b.file_path AS bfile, b.start_line AS bline,
                   type(r) AS kind, r.method AS method, r.path AS path, r.ambiguous AS ambiguous""",
                      src=src_service, dst=dst_service, method=method, path=path)

    def callers(self, node_id, service, limit=2):
        return self.q("""MATCH (x:Chunk {ns: $ns})-[:CALLS]->(a:Chunk {id: $id})
                         WHERE x.service = $service AND x.name <> '<module>'
                         RETURN x.qualified_name AS name, x.file_path AS file, x.start_line AS line LIMIT $limit""",
                      id=node_id, service=service, limit=limit)

    def callees(self, node_id, service, limit=3):
        return self.q("""MATCH (b:Chunk {id: $id})-[:CALLS]->(y:Chunk {ns: $ns})
                         WHERE y.service = $service AND y.file_path IS NOT NULL
                         RETURN DISTINCT y.qualified_name AS name, y.file_path AS file, y.start_line AS line
                         LIMIT $limit""", id=node_id, service=service, limit=limit)


def rel(path: str, root: str) -> str:
    p = (path or "").replace("\\", "/")
    r = root.replace("\\", "/").rstrip("/") + "/"
    p = p[len(r):] if p.lower().startswith(r.lower()) else p
    return p.split("/src/main/java/")[-1] if "/src/main/java/" in p else p


# ── 1. service map ────────────────────────────────────────────────────────────

def service_map(edges, langs, services, routes, out: Path, title: str):
    pair = {(e["src"], e["dst"]): e for e in edges}
    nodes = sorted(set(services) | {s for p in pair for s in p})
    succ, pred = defaultdict(set), defaultdict(set)
    for a, b in pair:
        succ[a].add(b)
        pred[b].add(a)
    routers = {r["from"] for r in routes}
    # Layers: longest path from the callers (cycles broken by a depth cap)
    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for a, b in pair:
            if layer[b] < layer[a] + 1 and layer[a] + 1 < 6:
                layer[b] = layer[a] + 1
                changed = True
        if not changed:
            break
    isolated = [n for n in nodes if not succ[n] and not pred[n] and n not in routers]
    placed = [n for n in nodes if n not in isolated and n not in routers]
    cols = defaultdict(list)
    for n in placed:
        cols[layer[n] + 1].append(n)
    for r in routers:
        cols[0].append(r)
    # Barycentre ordering to reduce crossings
    order = {n: i for c in cols.values() for i, n in enumerate(sorted(c))}
    for c in cols:
        cols[c].sort()
    for _ in range(6):
        for c in sorted(cols)[1:]:
            cols[c].sort(key=lambda n: (sum(order[p] for p in pred[n]) / len(pred[n])) if pred[n] else order[n])
            order.update({n: i for i, n in enumerate(cols[c])})
        for c in sorted(cols, reverse=True)[1:]:
            cols[c].sort(key=lambda n: (sum(order[s] for s in succ[n]) / len(succ[n])) if succ[n] else order[n])
            order.update({n: i for i, n in enumerate(cols[c])})
    W, H, GAPX, GAPY, TOP, LEFT = 160, 26, 92, 9, 96, 30
    tallest = max(len(c) for c in cols.values())
    height = TOP + tallest * (H + GAPY) + 120
    width = LEFT * 2 + len(cols) * (W + GAPX) - GAPX
    pos = {}
    for c, members in cols.items():
        x = LEFT + c * (W + GAPX)
        step = (H + 22 + GAPY) if c == 0 else (H + GAPY)   # router boxes carry a second line
        y0 = TOP + (tallest * (H + GAPY) - len(members) * step) / 2
        for i, n in enumerate(members):
            pos[n] = (x, y0 + i * step)
    color = lambda n: SLOTS[LANG_ORDER.index(langs[n])] if langs.get(n) in LANG_ORDER else MUTED
    body = [text(LEFT, 30, title, 18, INK, weight="600"),
            text(LEFT, 52, f"{len(nodes)} services · {len(pair)} code-level service connections read from Neo4j"
                           f" + {sum(r['count'] for r in routes)} gateway/UI routes from config · columns run from "
                           f"entry points (left) to leaf services (right)", 12.5, INK2)]
    body += legend(LEFT, 78, [(l, SLOTS[i]) for i, l in enumerate(LANG_ORDER)], dashed_queue=True)
    for (a, b), e in sorted(pair.items(), key=lambda kv: kv[1]["kind"]):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        sx, sy, tx, ty = x1 + W, y1 + H / 2, x2, y2 + H / 2
        q = e["kind"] == "MESSAGES"
        if tx <= sx:   # same or earlier column: loop under
            d = f"M{sx},{sy} C{sx + 60},{sy + 40} {tx - 60},{ty + 40} {tx},{ty}"
        else:
            mx = (sx + tx) / 2
            d = f"M{sx},{sy} C{mx},{sy} {mx},{ty} {tx - 2},{ty}"
        w = 1.1 + min(len(e["endpoints"]), 6) * 0.25
        dash = 'stroke-dasharray="5 4"' if q else ""
        body.append(f'<path d="{d}" fill="none" stroke="{SLOTS[6] if q else MUTED}" stroke-opacity="{0.9 if q else 0.55}" '
                    f'stroke-width="{w:.2f}" {dash} marker-end="url(#{"arrq" if q else "arr"})"/>')
    # gateway / UI routes are summarised in the box, not drawn: one line per route would hide the rest
    route_count = {r["from"]: r["count"] for r in routes}
    for n, (x, y) in pos.items():
        if n in routers:
            body += node_box(x, y, W, H + 22, short(n), color(n),
                             sub=f"routes to {route_count[n]} service{'s' if route_count[n] != 1 else ''} (config)",
                             bold=True)
        else:
            body += node_box(x, y, W, H, short(n), color(n))
    if isolated:
        y = TOP + tallest * (H + GAPY) + 40
        body.append(text(LEFT, y, "No calls to or from other services in code:", 12, INK2, weight="600"))
        body.append(text(LEFT, y + 20, ", ".join(short(n) for n in isolated), 12, INK2))
    out.write_text(svg(int(width), int(height), body, title), encoding="utf-8")


# ── 2/5. star: who depends on X, or what X calls ──────────────────────────────

def star(center, rows, langs, out: Path, title: str, subtitle: str, incoming: bool, extra=()):
    rows = sorted(rows, key=lambda r: r["peer"])
    rows += [{"peer": p, "endpoints": [label], "kind": "ROUTE"} for p, label in extra]
    rows.sort(key=lambda r: r["peer"])
    W, H, ROWH, TOP, LEFT = 190, 28, 44, 92, 30
    n = len(rows)
    height = TOP + n * ROWH + 40
    width = 1080
    cx = LEFT if not incoming else width - LEFT - W
    px = width - LEFT - W if not incoming else LEFT
    cy = TOP + (n * ROWH) / 2 - H / 2
    color = lambda s: SLOTS[LANG_ORDER.index(langs[s])] if langs.get(s) in LANG_ORDER else MUTED
    body = [text(LEFT, 30, title, 18, INK, weight="600"), text(LEFT, 52, subtitle, 12.5, INK2)]
    body += legend(LEFT, 76, [(l, SLOTS[i]) for i, l in enumerate(LANG_ORDER)], dashed_queue=True)
    labels = []
    for i, r in enumerate(rows):
        y = TOP + i * ROWH
        q = r["kind"] == "MESSAGES"
        route = r["kind"] == "ROUTE"
        if incoming:
            sx, sy, tx, ty = px + W, y + H / 2, cx, cy + H / 2
        else:
            sx, sy, tx, ty = cx + W, cy + H / 2, px, y + H / 2
        mx = (sx + tx) / 2
        stroke = SLOTS[6] if q else MUTED
        dash = 'stroke-dasharray="5 4"' if (q or route) else ""
        body.append(f'<path d="M{sx},{sy} C{mx},{sy} {mx},{ty} {tx - 2},{ty}" fill="none" stroke="{stroke}" '
                    f'stroke-opacity="0.75" stroke-width="1.4" {dash} marker-end="url(#{"arrq" if q else "arr"})"/>')
        eps = sorted(r["endpoints"])
        label = eps[0] + (f"  (+{len(eps) - 1} more)" if len(eps) > 1 else "")
        lx = (px + W + 12) if incoming else (px - 12)
        labels.append((lx, y + H / 2 + 4, label))
        body += node_box(px, y, W, H, short(r["peer"]), color(r["peer"]))
    # endpoint labels last, on a surface backing, so no edge runs through the text
    for lx, ly, label in labels:
        lw = 5.9 * len(label) + 8
        bx = (lx - 4) if incoming else (lx - lw + 4)
        body.append(f'<rect x="{bx:.1f}" y="{ly - 12:.1f}" width="{lw:.1f}" height="16" rx="3" fill="{SURFACE}"/>')
        body.append(text(lx, ly, label, 10.5, INK2, anchor="start" if incoming else "end"))
    body += node_box(cx, cy - 6, W, H + 12, short(center), color(center), bold=True)
    out.write_text(svg(width, int(height), body, title), encoding="utf-8")


# ── 3/4. function-level trace across services ─────────────────────────────────

def trace(g: GraphData, links: list[dict], root: str, out: Path, title: str, subtitle: str):
    """links: rows from GraphData.link() that share one calling function."""
    a = links[0]
    src_service = a["afile"] and g.q("MATCH (c:Chunk {id: $id}) RETURN c.service AS s", id=a["aid"])[0]["s"]
    ups = g.callers(a["aid"], src_service, limit=2)
    W, H = 330, 44
    LX, RX, TOP = 40, 650, 110
    body = [text(LX, 30, title, 18, INK, weight="600"), text(LX, 52, subtitle, 12.5, INK2)]
    # swimlane for the calling service
    lane_h = (len(ups) + 1) * (H + 26) + 40
    rows_right = []
    for l in links:
        dst = g.q("MATCH (c:Chunk {id: $id}) RETURN c.service AS s", id=l["bid"])[0]["s"]
        rows_right.append((l, dst, g.callees(l["bid"], dst, limit=2)))
    right_h = sum((1 + len(c)) * (H + 26) + 50 for _, _, c in rows_right)
    height = TOP + max(lane_h, right_h) + 60
    body.append(f'<rect x="{LX - 16}" y="{TOP - 34}" width="{W + 32}" height="{lane_h}" rx="10" fill="none" '
                f'stroke="{AXIS}" stroke-dasharray="4 4"/>')
    body.append(text(LX, TOP - 14, f"service: {src_service}", 12.5, INK2, weight="600"))
    y = TOP
    up_pos = []
    for u in ups:
        body += node_box(LX, y, W, H, u["name"], SLOTS[0], sub=f"{rel(u['file'], root)}:{u['line']}")
        up_pos.append(y)
        y += H + 26
    for yy in up_pos:
        body.append(f'<line x1="{LX + W / 2}" y1="{yy + H}" x2="{LX + W / 2}" y2="{yy + H + 24}" stroke="{MUTED}" '
                    f'stroke-width="1.4" marker-end="url(#arr)"/>')
    caller_y = y
    body += node_box(LX, caller_y, W, H, a["a"], SLOTS[0], sub=f"{rel(a['afile'], root)}:{a['aline']}", bold=True)
    # right side: one lane per target service
    ry = TOP
    for l, dst, downs in rows_right:
        lane = (1 + len(downs)) * (H + 26) + 30
        body.append(f'<rect x="{RX - 16}" y="{ry - 34}" width="{W + 32}" height="{lane}" rx="10" fill="none" '
                    f'stroke="{AXIS}" stroke-dasharray="4 4"/>')
        body.append(text(RX, ry - 14, f"service: {dst}", 12.5, INK2, weight="600"))
        body += node_box(RX, ry, W, H, l["b"], SLOTS[1], sub=f"{rel(l['bfile'], root)}:{l['bline']}", bold=True)
        sx, sy, tx, ty = LX + W, caller_y + H / 2, RX, ry + H / 2
        mx = (sx + tx) / 2
        body.append(f'<path d="M{sx},{sy} C{mx},{sy} {mx},{ty} {tx - 2},{ty}" fill="none" stroke="{SLOTS[1]}" '
                    f'stroke-width="2.4" marker-end="url(#arrh)"/>')
        conf = "possible" if l["ambiguous"] else "confirmed"
        body.append(f'<rect x="{mx - 125}" y="{(sy + ty) / 2 - 30}" width="250" height="40" rx="6" '
                    f'fill="{SURFACE}" stroke="{SLOTS[1]}" stroke-width="1"/>')
        body.append(text(mx, (sy + ty) / 2 - 14, f"{l['kind']} · {l['method']} · {conf}", 11, INK, "middle", "600"))
        body.append(text(mx, (sy + ty) / 2 + 2, l["path"], 10.5, INK2, "middle"))
        dy = ry + H + 26
        for d in downs:   # each is called by the handler: elbow from the handler, not a chain
            body.append(f'<path d="M{RX + 24},{ry + H} V{dy + H / 2} H{RX + 34}" fill="none" stroke="{MUTED}" '
                        f'stroke-width="1.4" marker-end="url(#arr)"/>')
            body += node_box(RX + 36, dy, W - 36, H, d["name"], SLOTS[1], sub=f"{rel(d['file'], root)}:{d['line']}")
            dy += H + 26
        ry += lane + 30
    body.append(text(LX, height - 22, "Thin arrows: function calls inside a service (CALLS).  "
                                      "Thick arrow: the cross-service edge Kitsune added.", 11.5, INK2))
    out.write_text(svg(1040, int(height), body, title), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-dir", required=True, help="engine data dir (holds <namespace>/system.json)")
    ap.add_argument("--source-root", required=True, help="the system's folder, to shorten file paths")
    ap.add_argument("--title", default="")
    ap.add_argument("--impact", action="append", default=[])
    ap.add_argument("--fanout", action="append", default=[])
    ap.add_argument("--trace", action="append", default=[],
                    help="name:src:dst[:METHOD:/path][,dst2...] e.g. "
                         "booking:ts-preserve-service:ts-order-service:POST:/api/v1/orderservice/order")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g = GraphData(args.namespace)
    system = json.loads((Path(args.data_dir) / args.namespace / "system.json").read_text(encoding="utf-8"))
    services = list(system["services"])
    langs = g.languages()
    edges = g.service_edges()
    route_counts = defaultdict(set)
    for l in system["links"]:
        if not l["client_node"]:          # config routes (gateway, nginx) have no calling function
            route_counts[l["client_service"]].add(l["server_service"])
    routes = [{"from": k, "count": len(v)} for k, v in route_counts.items()]
    name = args.title or args.namespace
    service_map(edges, langs, services, routes, out / "service-map.svg", f"{name}: service map")
    for s in args.impact:
        rows = [{"peer": e["src"], "endpoints": e["endpoints"], "kind": e["kind"]} for e in edges if e["dst"] == s]
        extra = [(k, "(gateway route, config)") for k, v in route_counts.items() if s in v]
        star(s, rows, langs, out / f"impact-{short(s)}.svg", f"Who is affected if {s} changes?",
             f"{len(rows) + len(extra)} services call it · edges and endpoints read from Neo4j",
             incoming=True, extra=extra)
    for s in args.fanout:
        rows = [{"peer": e["dst"], "endpoints": e["endpoints"], "kind": e["kind"]} for e in edges if e["src"] == s]
        star(s, rows, langs, out / f"fanout-{short(s)}.svg", f"What does {s} call?",
             f"{len(rows)} services · endpoints read from Neo4j", incoming=False)
    for spec in args.trace:
        tname, src, rest = spec.split(":", 2)
        parts = rest.split(",")
        links = []
        for p in parts:
            bits = p.split(":")
            dst = bits[0]
            method, path = (bits[1], ":".join(bits[2:])) if len(bits) > 2 else (None, None)
            links += g.link(src, dst, method, path)[:1]
        if links:
            caller = links[0]["aid"]
            links = [l for l in links if l["aid"] == caller]
            trace(g, links, args.source_root, out / f"trace-{tname}.svg",
                  f"From one function to another service: {short(src)} → "
                  + " + ".join(short(g.q('MATCH (c:Chunk {id: $id}) RETURN c.service AS s', id=l['bid'])[0]['s'])
                               for l in links),
                  "Read from Neo4j: CALLS edges inside each service, and the cross-service edge between them")
    print("wrote", sorted(p.name for p in out.glob("*.svg")))


if __name__ == "__main__":
    main()
