"""Kitsune benchmark v2: exact ground truth, per-question ranks, confidence intervals.

Usage: python kitsune/eval/eval_v2.py gson|petclinic [--index] [--validate-only]

  --index          (re)build the index from eval/repos/<repo> first (needed once;
                   uses Qdrant/Neo4j if configured, else the local stores)
  --validate-only  only check that every expected answer exists in the index

Indexes and results live in kitsune/data/<namespace>/ (git-ignored).

A retrieved chunk counts as correct only if file basename AND symbol name AND chunk_type
all equal the expected triple (or one of its listed alternates).
"""
import json
import math
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR.parents[1]))  # folder containing the kitsune package
sys.path.insert(1, str(EVAL_DIR))

from benchmark_v2_questions import GSON, PETCLINIC  # noqa: E402
from kitsune.embedder.embedder import get_model  # noqa: E402
from kitsune.engine import RepositoryKnowledgeEngine  # noqa: E402

BASE = EVAL_DIR.parent  # kitsune/ — results go to kitsune/data/<namespace>/
REPOS = {
    "gson": ("gson_v2", GSON, EVAL_DIR / "repos" / "gson"),
    "petclinic": ("petclinic_v2", PETCLINIC, EVAL_DIR / "repos" / "spring_petclinic"),
}
K = 10
RRF_K = 60
CE_TOP = 14
CE_MODELS = {
    "L-6": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "L-12": "cross-encoder/ms-marco-MiniLM-L-12-v2",
}


def key(c):
    return (c.get("file_path"), c.get("name"), c.get("start_line"))


def dedupe(chunks):
    seen, out = set(), []
    for c in chunks:
        k = key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def matches(c, item):
    triples = [item["expect"]] + list(item["alts"])
    base = Path(c.get("file_path") or "").name
    return any(base == f and c.get("name") == n and c.get("chunk_type") == t for f, n, t in triples)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def main():
    repo = sys.argv[1]
    validate_only = "--validate-only" in sys.argv
    ns, questions, repo_path = REPOS[repo]
    engine = RepositoryKnowledgeEngine(data_dir=str(BASE / "data" / ns), namespace=ns)
    if "--index" in sys.argv or engine.vector_store.count() == 0:
        t0 = time.time()
        m = engine.index_repository(str(repo_path), incremental=False)
        print(f"[{repo}] indexed {m.total_files} files, {m.total_chunks} chunks "
              f"in {time.time() - t0:.1f}s")

    # ---- 1. Validate that every expected answer exists in the index ----
    invalid = []
    for i, item in enumerate(questions):
        _, name, _ = item["expect"]
        cands = engine.vector_store.get_by_name(name)
        if not any(matches(c, item) for c in cands):
            invalid.append((i, item["expect"], [(Path(c.get("file_path") or "").name, c.get("chunk_type")) for c in cands][:6]))
    print(f"[{repo}] questions: {len(questions)}  invalid expected answers: {len(invalid)}")
    for i, exp, found in invalid:
        print(f"  Q{i+1} expected {exp} -> index has {found}")
    if validate_only:
        return
    if invalid:
        bad = {i for i, _, _ in invalid}
        questions = [x for i, x in enumerate(questions) if i not in bad]
        print(f"Dropping invalid; evaluating {len(questions)} questions.")

    embedder = get_model()
    from sentence_transformers import CrossEncoder
    ces = {n: CrossEncoder(m) for n, m in CE_MODELS.items()}
    for ce in ces.values():  # warm up
        ce.predict([["warm", "up"]])
    embedder.encode("warm up", normalize_embeddings=True)

    vs, ks, gs = engine.vector_store, engine.keyword_store, engine.graph_store

    def graph_only(q):
        deps = []
        seeds = []
        for nid in gs.search_nodes(q, limit=4):
            meta = gs.get_node_metadata(nid) or {}
            n = meta.get("name")
            if n and n not in seeds:
                seeds.append(n)
                deps.extend(vs.get_by_name(n))
        for s in seeds[:3]:
            for d in gs.get_dependencies(s, depth=2):
                deps.extend(vs.get_by_name(d))
        return dedupe(deps)

    def graph_from(seed_chunks):
        names = [c.get("name") for c in seed_chunks if c.get("name")]
        deps = []
        for s in names[:3]:
            for d in gs.get_dependencies(s, depth=2):
                deps.extend(vs.get_by_name(d))
        return deps

    def rrf(lists):
        score, obj = {}, {}
        for lst in lists:
            for r, c in enumerate(lst):
                k = key(c)
                score[k] = score.get(k, 0.0) + 1.0 / (RRF_K + r + 1)
                obj[k] = c
        return [obj[k] for k in sorted(score, key=score.get, reverse=True)]

    def ce_rerank(q, pool, model):
        top = pool[:CE_TOP]
        if not top:
            return pool
        scores = ces[model].predict([[q, c.get("text", "")] for c in top])
        re = [c for _, c in sorted(zip(scores, top), key=lambda x: x[0], reverse=True)]
        return re + pool[CE_TOP:]

    names = ["T1 vector", "T2 graph", "T3 bm25", "T1+T3", "T1+T2", "All3 concat",
             "All3 RRF", "All3 concat+CE L-6", "All3 concat+CE L-12", "All3 RRF+CE L-6"]
    rows = {n: {"ranks": [], "lat": [], "pool": []} for n in names}

    for item in questions:
        q = item["q"]
        t = time.perf_counter()
        qv = embedder.encode(q, normalize_embeddings=True).tolist()
        t_embed = time.perf_counter() - t

        def timed(fn, uses_embed):
            t0 = time.perf_counter()
            res = fn()
            return res, time.perf_counter() - t0 + (t_embed if uses_embed else 0.0)

        t1, l1 = timed(lambda: vs.search(qv, k=K), True)
        t3, l3 = timed(lambda: ks.search(q, k=K), False)
        t2, l2 = timed(lambda: graph_only(q), False)
        g13, lg13 = timed(lambda: graph_from(t1 or t3), False)
        g1, lg1 = timed(lambda: graph_from(t1), False)

        out = {}
        out["T1 vector"] = (dedupe(t1), l1)
        out["T2 graph"] = (t2, l2)
        out["T3 bm25"] = (dedupe(t3), l3)
        out["T1+T3"] = (dedupe(t1 + t3), l1 + l3)
        out["T1+T2"] = (dedupe(t1 + g1), l1 + lg1)
        concat = dedupe(t1 + t3 + g13)
        lat_all = l1 + l3 + lg13
        out["All3 concat"] = (concat, lat_all)
        r0 = time.perf_counter()
        fused = rrf([dedupe(t1), dedupe(t3), dedupe(g13)])
        l_rrf = lat_all + (time.perf_counter() - r0)
        out["All3 RRF"] = (fused, l_rrf)
        for m in ("L-6", "L-12"):
            c0 = time.perf_counter()
            res = ce_rerank(q, concat, m)
            out[f"All3 concat+CE {m}"] = (res, lat_all + time.perf_counter() - c0)
        c0 = time.perf_counter()
        res = ce_rerank(q, fused, "L-6")
        out["All3 RRF+CE L-6"] = (res, l_rrf + time.perf_counter() - c0)

        for n, (pool, lat) in out.items():
            rank = next((i + 1 for i, c in enumerate(pool) if matches(c, item)), None)
            rows[n]["ranks"].append(rank)
            rows[n]["lat"].append(lat)
            rows[n]["pool"].append(len(pool))

    N = len(questions)
    lines = [f"Repo: {repo}  namespace: {ns}  questions: {N}", ""]
    hdr = f"{'Config':<22}| Hit@1 (95% CI)        | Hit@5 (95% CI)        | MRR@10 | Found-any | Lat ms | Pool"
    lines += [hdr, "-" * len(hdr)]
    summary = {}
    for n, r in rows.items():
        ranks = r["ranks"]
        h1 = sum(1 for x in ranks if x == 1)
        h5 = sum(1 for x in ranks if x is not None and x <= 5)
        fa = sum(1 for x in ranks if x is not None)
        mrr = sum(1.0 / x for x in ranks if x is not None and x <= K) / N
        c1, c5 = wilson(h1, N), wilson(h5, N)
        lat = sum(r["lat"]) / N * 1000
        pool = sum(r["pool"]) / N
        lines.append(f"{n:<22}| {h1:>2}/{N} {h1/N*100:5.1f}% [{c1[0]*100:3.0f}-{c1[1]*100:3.0f}] | "
                     f"{h5:>2}/{N} {h5/N*100:5.1f}% [{c5[0]*100:3.0f}-{c5[1]*100:3.0f}] | {mrr:6.3f} | "
                     f"{fa:>2}/{N}     | {lat:6.0f} | {pool:4.1f}")
        summary[n] = {"hit1": h1, "hit5": h5, "found_any": fa, "mrr10": mrr, "lat_ms": lat,
                      "pool": pool, "ranks": ranks}
    text = "\n".join(lines)
    print(text)
    out_path = BASE / "data" / ns / "eval_v2_results.json"
    out_path.write_text(json.dumps({"n": N, "invalid": [list(map(str, x[:2])) for x in invalid],
                                    "questions": [x["q"] for x in questions], "results": summary},
                                   indent=2), encoding="utf-8")
    (BASE / "data" / ns / "eval_v2_table.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
