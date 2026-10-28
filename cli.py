"""
kitsune — command-line interface.

    kitsune index ./payments-api --namespace payments
    kitsune index-system ./shop --namespace shop
    kitsune ask "What breaks if I change process_payment?" --namespace payments
    kitsune callers charge --namespace payments
    kitsune links --service cart --direction in --namespace shop
    kitsune teach host basket-api cart --namespace shop

Every command takes --namespace (one project / system each) and --json (machine-readable
output). Indexes live in --data-dir (default: KITSUNE_DATA_DIR, else ~/.kitsune/data).
Servers come from the environment: QDRANT_URL, QDRANT_API_KEY, NEO4J_URI, NEO4J_USER,
NEO4J_PASSWORD (without them Kitsune uses local stores), and the LLM from GROQ_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _engine(args):
    from kitsune.engine import RepositoryKnowledgeEngine
    data_dir = args.data_dir or os.getenv("KITSUNE_DATA_DIR") or str(Path.home() / ".kitsune" / "data")
    return RepositoryKnowledgeEngine(data_dir=data_dir, namespace=args.namespace)


def _out(args, value, text=None):
    if args.json:
        print(json.dumps(value, indent=2, default=str))
    elif text is not None:
        print(text)
    elif isinstance(value, list):
        print("\n".join(str(v) for v in value) if value else "(none)")
    else:
        print(json.dumps(value, indent=2, default=str))


def cmd_index(args):
    m = _engine(args).index_repository(args.path, incremental=not args.full)
    _out(args, {"files": m.total_files, "unchanged": m.skipped_files, "chunks": m.total_chunks,
                "errors": m.errors[:20]},
         f"Indexed {m.total_files} files ({m.skipped_files} unchanged), {m.total_chunks} chunks"
         + (f", {len(m.errors)} errors" if m.errors else ""))


def cmd_index_system(args):
    s = _engine(args).index_system(args.path, incremental=not args.full)
    lines = [f"{name:32} {v['chunks']:6} chunks" for name, v in sorted(s["services"].items())]
    _out(args, s, "\n".join(lines) + f"\n{len(s['services'])} services · {s['cross_service_links']} links "
                                     f"({s['confirmed_links']} confirmed)")


def cmd_search(args):
    r = _engine(args).search(args.query, top_k=args.k)
    rows = [{"name": c.get("qualified_name") or c.get("name"), "file": c.get("file_path"),
             "line": c.get("start_line"), "service": c.get("service", "")}
            for c in r["primary_chunks"] + r["dependency_chunks"] + r["keyword_chunks"]]
    _out(args, rows, "\n".join(f"{x['name']:40} {x['file']}:{x['line']}" for x in rows) or "(no results)")


def cmd_ask(args):
    print(_engine(args).ask(args.question))


def _related(args, direction):
    """Callers / callees with where each lives; --cross-service also follows HTTP, gRPC and
    queue links into other services."""
    rows = _engine(args).graph_store.get_related(args.name, direction=direction, depth=args.depth,
                                                 cross_service=args.cross_service)
    _out(args, rows, "\n".join(
        f"{'  ' * (r['depth'] - 1)}{r['qualified_name']:44} {r['service'] or '':26} "
        f"{(r['file_path'] or '').split('/')[-1].split(chr(92))[-1]}:{r['start_line'] or ''}"
        + (f"  via {r['via']}" if r.get("via") and r["via"] != "CALLS" else "")
        for r in rows) or "(none)")


def cmd_callers(args):
    _related(args, "callers")


def cmd_callees(args):
    _related(args, "callees")


def cmd_impact(args):
    _out(args, _engine(args).get_impact_analysis(args.name, max_depth=args.depth))


def cmd_links(args):
    links = _engine(args).get_service_links(args.service, args.direction)
    _out(args, links, "\n".join(
        f"{l['client_service']} → {l['server_service']}  {l['kind']} {l['method']} {l['path'] or '(service)'}"
        f"  [{l['confidence']}]  {l['client_file']}:{l['client_line']}" for l in links) or "(none)")


def cmd_map(args):
    m = _engine(args).service_map()
    _out(args, m, "\n".join(f"{e['from']} → {e['to']} ({e['kind']}, {len(e.get('endpoints', []))} endpoints)"
                            for e in m.get("edges", [])) or "(no links: run index-system first)")


def cmd_endpoint(args):
    _out(args, _engine(args).get_endpoint_callers(args.path, args.method, args.service))


def cmd_blind_spots(args):
    spots = _engine(args).blind_spots()
    _out(args, spots, "\n".join(f"{b.get('service', ''):28} {b['kind']:5} {b['what']}  — {b['why']}"
                                for b in spots) or "(none)")


def cmd_teach(args):
    e = _engine(args)
    if args.what == "host":
        n = e.teach_host(args.a, args.b)
    elif args.what == "env":
        n = e.teach_env(args.a, args.b)
    else:
        client, server, kind, method, path = args.a, args.b, *args.rest
        n = e.teach_link(client, server, kind, method, path, correct=not args.wrong)
    _out(args, {"links": n}, f"Learned. {n} cross-service links now.")


def cmd_age(args):
    a = _engine(args).kitsune_age()
    _out(args, a, f"{'🦊' * a['tails']}  {a['tails']} tail(s) · {a['lessons']} lessons · "
                  f"{a['taught_lessons']} taught · systems: {', '.join(a['systems_seen']) or '-'}")


def cmd_stats(args):
    _out(args, _engine(args).stats())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kitsune", description="Kitsune code knowledge engine")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--namespace", "-n", default=None, help="project / system name")
    common.add_argument("--data-dir", default=None, help="where local indexes live")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("index", cmd_index, "index one repository")
    sp.add_argument("path")
    sp.add_argument("--full", action="store_true", help="re-index every file, not just changed ones")
    sp = add("index-system", cmd_index_system, "index a system of services and link them")
    sp.add_argument("path")
    sp.add_argument("--full", action="store_true")
    sp = add("search", cmd_search, "find the code relevant to a question")
    sp.add_argument("query")
    sp.add_argument("-k", type=int, default=None)
    add("ask", cmd_ask, "answer a question with an LLM, citing code").add_argument("question")
    for name, fn, h in (("callers", cmd_callers, "who calls this function"),
                        ("callees", cmd_callees, "what this function calls"),
                        ("impact", cmd_impact, "what is affected if this changes")):
        sp = add(name, fn, h)
        sp.add_argument("name")
        sp.add_argument("--depth", type=int, default=2 if name != "impact" else 3)
        if name != "impact":
            sp.add_argument("--cross-service", action="store_true",
                            help="also follow HTTP / gRPC / queue links into other services")
    sp = add("links", cmd_links, "cross-service links")
    sp.add_argument("--service", default=None)
    sp.add_argument("--direction", choices=["in", "out", "both"], default="both")
    add("map", cmd_map, "which service calls which")
    sp = add("endpoint", cmd_endpoint, "who calls an HTTP endpoint")
    sp.add_argument("path")
    sp.add_argument("--method", default=None)
    sp.add_argument("--service", default=None)
    add("blind-spots", cmd_blind_spots, "what Kitsune could not connect")
    sp = add("teach", cmd_teach, "teach Kitsune: host ALIAS SERVICE | env VAR SERVICE | "
                                 "link CLIENT SERVER KIND METHOD PATH [--wrong]")
    sp.add_argument("what", choices=["host", "env", "link"])
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("rest", nargs="*")
    sp.add_argument("--wrong", action="store_true", help="for link: this link is wrong")
    add("age", cmd_age, "how wise Kitsune has grown")
    add("stats", cmd_stats, "index statistics")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "teach" and args.what == "link" and len(args.rest) != 3:
        print("usage: kitsune teach link CLIENT SERVER KIND METHOD PATH [--wrong]", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if not args.verbose:
        import warnings
        warnings.filterwarnings("ignore")   # library notices (version checks, deprecations)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
