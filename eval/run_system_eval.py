"""
Kitsune multi-service evaluation — are services connected correctly?

Indexes each system in eval/system_questions.json with index_system() and scores
the cross-service links against ground truth written from the projects' source.

The systems are public projects fetched by eval/fetch_external.py (git-ignored):
    python kitsune/eval/fetch_external.py
    python kitsune/eval/run_system_eval.py [-v] [--backend server] [--system sockshop]

Metrics
  link_precision / link_recall   endpoint-level links (confirmed links only)
  link_recall_any                same, counting "possible" links too
  service_edge_precision/recall  service → service pairs (incl. runtime-only pairs)
  forbidden_links                links that must not exist (should be 0)
  impact_accuracy                "which services are affected if X changes"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR.parents[1]))


def _key(link) -> tuple:
    return (link["client_service"], link["server_service"], link["kind"], link["method"], link["path"])


def evaluate_system(name: str, spec: dict, backend: str, verbose: bool, lore_path: str) -> dict:
    from kitsune.engine import RepositoryKnowledgeEngine

    source = EVAL_DIR / spec["source"]
    if not source.exists():
        raise SystemExit(f"{source} missing — run: python kitsune/eval/fetch_external.py")
    data_dir = tempfile.mkdtemp(prefix=f"kitsune-sys-{name}-")
    os.environ["KITSUNE_LORE"] = lore_path
    engine = RepositoryKnowledgeEngine(data_dir=data_dir, namespace=f"syseval-{name}")
    engine.clear_all()
    try:
        t0 = time.time()
        summary = engine.index_system(str(source), incremental=False)
        seconds = time.time() - t0
        links = engine.get_service_links()

        truth = {tuple(l) for l in spec["links"]}
        # endpoint-level links only (service-level links, e.g. gateway routes in
        # config or runtime-built paths, have no path and are scored as edges)
        confirmed = {_key(l) for l in links if l["confidence"] == "confirmed" and l["path"]}
        anylink = {_key(l) for l in links if l["path"]}
        tp = len(truth & confirmed)
        precision = tp / len(confirmed) if confirmed else 1.0
        recall = tp / len(truth) if truth else 1.0
        recall_any = len(truth & anylink) / len(truth) if truth else 1.0

        truth_edges = {(c, s, k) for c, s, k, _, _ in spec["links"]} | {tuple(e) for e in spec["runtime_edges"]}
        found_edges = {(l["client_service"], l["server_service"], l["kind"]) for l in links}
        edge_p = len(truth_edges & found_edges) / len(found_edges) if found_edges else 1.0
        edge_r = len(truth_edges & found_edges) / len(truth_edges) if truth_edges else 1.0
        forbidden = [l for l in links if [l["client_service"], l["server_service"]] in spec["forbidden"]]

        impact_rows = []
        for item in spec["impact"]:
            affected = {l["client_service"] for l in engine.get_service_links(item["service"], "in")}
            impact_rows.append((item, affected, affected == set(item["expect"])))

        if verbose:
            print(f"\n── {name}: {len(summary['services'])} services, "
                  f"{sum(v['chunks'] for v in summary['services'].values())} chunks, {seconds:.0f}s")
            for miss in sorted(truth - anylink):
                print(f"   MISSING   {miss}")
            for miss in sorted((truth & anylink) - confirmed):
                print(f"   POSSIBLE  {miss}  (found, but not confirmed)")
            for extra in sorted(confirmed - truth):
                print(f"   EXTRA     {extra}")
            for e in sorted(truth_edges - found_edges):
                print(f"   MISSING EDGE  {e}")
            for e in sorted(found_edges - truth_edges):
                print(f"   EXTRA EDGE    {e}")
            for l in forbidden:
                print(f"   FORBIDDEN {_key(l)}")
            for item, affected, ok in impact_rows:
                print(f"   {'✓' if ok else '✗'} impact {item['service']}: {sorted(affected)} "
                      f"(want {sorted(item['expect'])})")

        return {"links_truth": len(truth), "links_confirmed": len(confirmed),
                "link_precision": precision, "link_recall": recall, "link_recall_any": recall_any,
                "service_edge_precision": edge_p, "service_edge_recall": edge_r,
                "forbidden_links": len(forbidden),
                "impact_accuracy": sum(ok for *_, ok in impact_rows) / len(impact_rows),
                "index_seconds": seconds,
                "chunks": sum(v["chunks"] for v in summary["services"].values())}
    finally:
        try:
            engine.clear_all()
            if backend == "server":
                engine.vector_store._client.delete_collection(engine.vector_store._collection_name)
                engine.graph_store.close()
        except Exception:
            pass
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Kitsune multi-service evaluation")
    parser.add_argument("--backend", choices=["local", "server"], default="local")
    parser.add_argument("--system", help="evaluate only this system")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--grow", action="store_true",
                        help="share one lore across the systems, in order, and report what Kitsune "
                             "learned (default: every system starts with no lore, so scores are honest)")
    args = parser.parse_args()

    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    if args.backend == "local":
        os.environ.pop("QDRANT_URL", None)
        os.environ["NEO4J_URI"] = "bolt://127.0.0.1:1"
    else:
        os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
        os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")

    spec = json.loads((EVAL_DIR / "system_questions.json").read_text(encoding="utf-8"))
    results = {}
    lore_dir = tempfile.mkdtemp(prefix="kitsune-lore-")
    for name, system in spec["systems"].items():
        if args.system and name != args.system:
            continue
        lore_path = os.path.join(lore_dir, "lore.json" if args.grow else f"{name}.json")
        results[name] = evaluate_system(name, system, args.backend, args.verbose, lore_path)

    keys = [k for k in next(iter(results.values())) if k not in ("links_truth", "links_confirmed")]
    print(f"\nKitsune multi-service eval -- backend={args.backend}")
    print(f"{'metric':24}" + "".join(f"{n:>14}" for n in results))
    for k in keys:
        print(f"{k:24}" + "".join(f"{r[k]:14.3f}" for r in results.values()))
    out = EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    (out / "system_latest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if args.grow:
        from kitsune.services.lore import Lore
        age = Lore(os.path.join(lore_dir, "lore.json")).age()
        print(f"\nKitsune grew to {age['tails']} tails ({age['lessons']} lessons): "
              f"learned {age['learned_habits']}, forming {age['habits_forming']}")
    shutil.rmtree(lore_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
