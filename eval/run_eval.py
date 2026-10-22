"""
Kitsune quality evaluation — retrieval, call graph, routing, and concepts.

Indexes the fixture repos in eval/repos/ with the real embedding model and
scores Kitsune against the ground truth in eval/questions.json. No LLM calls.

Usage (from the folder that contains the kitsune package, or anywhere):
    python kitsune/eval/run_eval.py                    # local backends (ChromaDB + NetworkX)
    python kitsune/eval/run_eval.py --backend server   # Qdrant + Neo4j from env / defaults
    python kitsune/eval/run_eval.py --save-baseline    # store results as the new baseline
    python kitsune/eval/run_eval.py -v                 # per-question details

Every run is compared against eval/baseline.json (if present).
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
sys.path.insert(0, str(EVAL_DIR.parents[1]))  # folder containing the kitsune package

BASELINE = EVAL_DIR / "baseline.json"
RESULTS_DIR = EVAL_DIR / "results"


# ── Matching ──────────────────────────────────────────────────────────────────

def _chunk_matches(chunk: dict, expected: str) -> bool:
    """Does a retrieved chunk contain the expected symbol's code?"""
    name = chunk.get("name", "")
    qname = chunk.get("qualified_name") or name
    want_name = expected.split(".")[-1]
    if name != want_name:
        return False
    # "Order.save" must not be satisfied by User.save; an unscoped chunk
    # (e.g. a Go method Kitsune did not attach to its type) still counts.
    return "." not in expected or qname == expected or "." not in qname


def _prf(got: set[str], want: set[str]) -> tuple[float, float]:
    if not want:
        return (1.0 if not got else 0.0), 1.0
    if not got:
        return 1.0, 0.0
    hit = len(got & want)
    return hit / len(got), hit / len(want)


# ── Scoring ───────────────────────────────────────────────────────────────────

def evaluate_repo(name: str, spec: dict, backend: str, verbose: bool) -> dict:
    from kitsune.engine import RepositoryKnowledgeEngine
    from kitsune.query.router import route_query

    data_dir = tempfile.mkdtemp(prefix=f"kitsune-eval-{name}-")
    repo_copy = Path(tempfile.mkdtemp(prefix=f"kitsune-eval-repo-{name}-")) / name
    shutil.copytree((EVAL_DIR / spec["path"]).resolve(), repo_copy,
                    ignore=shutil.ignore_patterns(*spec.get("exclude", [])))
    engine = RepositoryKnowledgeEngine(data_dir=data_dir, namespace=f"eval-{name}")
    if backend == "server":
        assert not engine.vector_store.using_fallback, "Qdrant not reachable"
        assert not engine.graph_store.use_fallback, "Neo4j not reachable"
    engine.clear_all()

    try:
        t0 = time.time()
        metrics = engine.index_repository(str(repo_copy))
        index_seconds = time.time() - t0
        rows = []
        for item in spec["questions"]:
            q, kind = item["q"], item["kind"]
            route_ok = route_query(q).query_type in item["route"]
            row = {"q": q, "kind": kind, "route_ok": route_ok}

            if kind == "retrieval":
                t = time.time()
                result = engine.search(q)
                row["latency"] = time.time() - t
                context = (result["primary_chunks"] + result["dependency_chunks"]
                           + result["keyword_chunks"])
                found = [e for e in item["expect"] if any(_chunk_matches(c, e) for c in context)]
                row["recall"] = len(found) / len(item["expect"])
                row["top_hit"] = any(_chunk_matches(c, e) for c in result["primary_chunks"][:3]
                                     for e in item["expect"])
                row["context_chars"] = result["metrics"].total_context_chars
                row["missing"] = sorted(set(item["expect"]) - set(found))
            else:
                if kind == "parents":
                    got = set(engine.get_inheritance_tree(item["symbol"]))
                else:
                    direction = "callers" if kind == "callers" else "callees"
                    related = engine.graph_store.get_related(
                        item["symbol"], direction=direction, depth=item.get("depth", 1))
                    got = {r["qualified_name"] for r in related}
                want = set(item["expect"])
                row["precision"], row["recall"] = _prf(got, want)
                row["false"] = sorted(got - want)
                row["missing"] = sorted(want - got)
            rows.append(row)

        concepts = [c["name"].lower() for c in engine.get_all_concepts()]
        groups = spec["concepts"]
        covered = sum(any(a in c for c in concepts for a in g) for g in groups)
        relevant = sum(any(a in c for g in groups for a in g) for c in concepts)
        concept_score = {
            "coverage": covered / len(groups),
            "precision": relevant / len(concepts) if concepts else 0.0,
            "count": len(concepts),
            "names": concepts[:15],
        }

        if verbose:
            print(f"\n── {name} ──")
            for r in rows:
                if r["kind"] == "retrieval":
                    detail = f"recall={r['recall']:.2f} top_hit={r['top_hit']} chars={r['context_chars']}"
                else:
                    detail = f"P={r['precision']:.2f} R={r['recall']:.2f}"
                extra = ""
                if r.get("missing"):
                    extra += f" missing={r['missing']}"
                if r.get("false"):
                    extra += f" false={r['false']}"
                print(f"  {'✓' if r['route_ok'] else '✗'}route  {r['kind']:9} {detail}{extra}  | {r['q']}")
            print(f"  concepts: {concept_score['names']}")

        return {"rows": rows, "concepts": concept_score, "index_seconds": index_seconds,
                "chunks": metrics.total_chunks, "errors": metrics.errors}
    finally:
        try:
            engine.clear_all()
            if backend == "server":
                engine.vector_store._client.delete_collection(engine.vector_store._collection_name)
                engine.graph_store.close()
        except Exception:
            pass
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(repo_copy.parent, ignore_errors=True)


def summarize(per_repo: dict) -> dict:
    rows = [r for repo in per_repo.values() for r in repo["rows"]]
    ret = [r for r in rows if r["kind"] == "retrieval"]
    graph = [r for r in rows if r["kind"] != "retrieval"]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    gp, gr = mean([r["precision"] for r in graph]), mean([r["recall"] for r in graph])
    return {
        "questions": len(rows),
        "retrieval_recall": mean([r["recall"] for r in ret]),
        "retrieval_top_hit": mean([1.0 if r["top_hit"] else 0.0 for r in ret]),
        "graph_precision": gp,
        "graph_recall": gr,
        "graph_f1": 2 * gp * gr / (gp + gr) if gp + gr else 0.0,
        "graph_exact": mean([1.0 if not r["false"] and not r["missing"] else 0.0 for r in graph]),
        "router_accuracy": mean([1.0 if r["route_ok"] else 0.0 for r in rows]),
        "concept_coverage": mean([v["concepts"]["coverage"] for v in per_repo.values()]),
        "concept_precision": mean([v["concepts"]["precision"] for v in per_repo.values()]),
        "avg_context_chars": mean([r["context_chars"] for r in ret]),
        "avg_retrieval_latency_s": mean([r["latency"] for r in ret]),
        "index_seconds": sum(v["index_seconds"] for v in per_repo.values()),
        "index_errors": sum(len(v["errors"]) for v in per_repo.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--backend", choices=["local", "server"], default="local")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--budget", type=int, help="context budget (chars) to evaluate with")
    args = parser.parse_args()

    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    if args.backend == "local":
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("QDRANT_LOCAL_PATH", None)
        os.environ["NEO4J_URI"] = "bolt://127.0.0.1:1"  # unreachable → NetworkX
    else:
        os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
        os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
    os.environ.pop("CHROMA_PATH", None)
    os.environ.pop("KITSUNE_CONTEXT_BUDGET", None)
    if args.budget:
        os.environ["KITSUNE_CONTEXT_BUDGET"] = str(args.budget)

    spec = json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))
    per_repo = {name: evaluate_repo(name, repo, args.backend, args.verbose)
                for name, repo in spec["repos"].items()}
    summary = summarize(per_repo)

    # Router-only paraphrases (no indexing needed)
    from kitsune.query.router import route_query
    router_rows = [(q, route_query(q).query_type, ok) for q, ok in spec.get("router", [])]
    summary["router_paraphrase_accuracy"] = (
        sum(t in ok for _, t, ok in router_rows) / len(router_rows) if router_rows else 0.0)
    if args.verbose:
        print("\n── router paraphrases ──")
        for q, t, ok in router_rows:
            print(f"  {'✓' if t in ok else '✗'} {t:10} (want {'/'.join(ok)})  | {q}")

    baseline = (json.loads(BASELINE.read_text(encoding="utf-8")).get("summary")
                if BASELINE.exists() else None)
    print(f"\nKitsune eval — {summary['questions']} questions, backend={args.backend}")
    print(f"{'metric':26} {'value':>10} {'baseline':>10} {'Δ':>9}")
    for key, value in summary.items():
        if key == "questions":
            continue
        base = baseline.get(key) if baseline else None
        delta = f"{value - base:+.3f}" if isinstance(base, (int, float)) else ""
        base_s = f"{base:.3f}" if isinstance(base, (int, float)) else "—"
        print(f"{key:26} {value:10.3f} {base_s:>10} {delta:>9}")

    record = {"summary": summary, "backend": args.backend,
              "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "repos": per_repo}
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"\nSaved baseline → {BASELINE}")


if __name__ == "__main__":
    main()
