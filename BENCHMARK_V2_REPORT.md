# Kitsune Benchmark v2 — Results (Gson and PetClinic)

All numbers below were measured. Raw per-question ranks are written to
`data/gson_v2/eval_v2_results.json` and `data/petclinic_v2/eval_v2_results.json`
(git-ignored, created when you run the benchmark).
Code: `eval/eval_v2.py`, questions: `eval/benchmark_v2_questions.py`.

**Reproduce:**

```bash
python kitsune/eval/eval_v2.py gson --index        # first run builds the index
python kitsune/eval/eval_v2.py petclinic --index
```

The repositories are vendored in `eval/repos/` (see `eval/repos/README.md` for upstream
commits). These results were measured with the Neo4j backend, before the review fixes of
2026-10-27 (exact graph lookups, de-duplicated reranking, per-project library catalog);
re-running may give slightly different numbers.

## 1. What changed from the old benchmark

| Old (6 questions) | New (about 30 questions) |
|---|---|
| Matched by name or substring | Must match **file + symbol name + type** exactly |
| One run, no error bars | Counts (k/N) and 95% confidence ranges |
| Pool by simple concatenation only | Also tests Reciprocal Rank Fusion (RRF) |
| Cross-encoder reloaded each call | Cross-encoder loaded and warmed up once |

Each question is a plain-English description (the symbol name is not in the question),
for example "Which builder method turns on indented, human-readable output?" -> `GsonBuilder.setPrettyPrinting`.

Before running, the script checks every expected answer exists in the index.
- PetClinic: 30 of 30 valid.
- Gson: 28 of 30 valid. `FieldNamingPolicy` and `LongSerializationPolicy` are **enums**, which the indexer does **not** chunk. They were dropped from the score. This is a real indexing gap (see section 5).

## 2. What the columns mean

| Column | Meaning |
|---|---|
| Hit@1 | Questions where the correct chunk is ranked **#1**. Shown as count/N, percent, and a 95% range. |
| Hit@5 | Correct chunk is somewhere in the top 5. |
| MRR@10 | Average of 1/rank of the correct chunk (rank 1 = 1.0, rank 2 = 0.5 ...; 0 if not in top 10). Higher is better. |
| Found-any | Correct chunk appears **anywhere** in the returned pool. Measures coverage, not ordering. |
| Lat ms | Average time per question, including embedding the question. Excludes model loading. |
| Pool | Average number of chunks returned. |

The 95% range is a Wilson interval. With about 30 questions the ranges are wide:
differences of one or two questions are noise.

Configurations: **T1** = vector, **T2** = graph, **T3** = BM25 keyword.
**concat** = T1 results, then T3, then graph neighbours. **RRF** = merge the three ranked lists by
score 1/(60+rank). **CE** = cross-encoder re-sorts the top 14.

## 3. Results

### Gson (28 questions)

| Config | Hit@1 | Hit@5 | MRR@10 | Found-any | Lat ms |
|---|---|---|---|---|---|
| T1 vector | 7 (25.0%) | 12 (42.9%) | 0.341 | 16 | 29 |
| T2 graph | 0 | 0 | 0.000 | 0 | 105 |
| T3 bm25 | 2 (7.1%) | 8 (28.6%) | 0.181 | 10 | 10 |
| T1+T3 | 7 (25.0%) | 12 (42.9%) | 0.341 | 17 | 39 |
| T1+T2 | 7 (25.0%) | 12 (42.9%) | 0.341 | 19 | 157 |
| All3 concat | 7 (25.0%) | 12 (42.9%) | 0.341 | 20 | 194 |
| All3 RRF | 8 (28.6%) | 16 (57.1%) | 0.388 | 20 | 194 |
| All3 concat + CE L-6 | 8 (28.6%) | 12 (42.9%) | 0.356 | 20 | 1213 |
| All3 concat + CE L-12 | 9 (32.1%) | 12 (42.9%) | 0.373 | 20 | 2211 |
| All3 RRF + CE L-6 | 8 (28.6%) | 13 (46.4%) | 0.373 | 20 | 1254 |

### PetClinic (30 questions)

| Config | Hit@1 | Hit@5 | MRR@10 | Found-any | Lat ms |
|---|---|---|---|---|---|
| T1 vector | 18 (60.0%) | 26 (86.7%) | 0.694 | 27 | 26 |
| T2 graph | 3 (10.0%) | 3 (10.0%) | 0.100 | 3 | 84 |
| T3 bm25 | 7 (23.3%) | 14 (46.7%) | 0.337 | 17 | 1 |
| T1+T3 | 18 (60.0%) | 26 (86.7%) | 0.694 | 27 | 27 |
| T1+T2 | 18 (60.0%) | 26 (86.7%) | 0.694 | 27 | 104 |
| All3 concat | 18 (60.0%) | 26 (86.7%) | 0.694 | 27 | 124 |
| All3 RRF | 11 (36.7%) | 23 (76.7%) | 0.522 | 27 | 124 |
| All3 concat + CE L-6 | 15 (50.0%) | 22 (73.3%) | 0.610 | 27 | 1028 |
| All3 concat + CE L-12 | 14 (46.7%) | 22 (73.3%) | 0.599 | 27 | 1942 |
| All3 RRF + CE L-6 | 15 (50.0%) | 22 (73.3%) | 0.606 | 27 | 1051 |

## 4. What the data supports

1. **The vector search (T1) is the strongest single tail** on both repos. Nothing beat it on Hit@1 by a clear margin.
2. **Combining tails widens coverage** on Gson: the right chunk is somewhere in the pool for 20/28 questions versus 16/28 for T1 alone. On PetClinic coverage did not change (27/30).
3. **Combining does not improve ordering with plain concat.** Top-1 equals T1's because T1 results are placed first. This is by construction.
4. **RRF gave mixed results.** Gson: Hit@5 rose from 12 to 16 of 28. PetClinic: Hit@1 fell from 18 to 11 of 30. Not a reliable win; the weak tails (T2, T3) pull wrong chunks up on PetClinic.
5. **Cross-encoders did not clearly help.** Gson: +1 to +2 Hit@1, within noise. PetClinic: Hit@1 dropped (18 -> 15 and 14). Cost is about 1 to 2 seconds per query. L-12 was not better than L-6 overall.
6. **Graph alone (T2) is weak for natural-language questions** (0/28 and 3/30). Likely reason (a hypothesis, not tested): it looks nodes up by name text, and these questions avoid the names. It is designed to expand from a seed, which the "All3" rows do.
7. **BM25 (T3) alone is weak here** (7% and 23% Hit@1) because the questions avoid exact identifiers. It is a safeguard for identifier searches; this benchmark does not test that case.

> [!NOTE]
> With 28 to 30 questions, the confidence ranges overlap for most rows. Only large gaps
> (for example T1 vs T2 or T3) are clearly real. Treat 1 to 3 question differences as noise.

## 5. Indexing results (measured)

| Item | Result |
|---|---|
| Gson re-index with batch Neo4j writes | **609.4 s** (old baseline 656.9 s, about 7% faster). About 35 s overlapped with a test run, so treat the gain as small and uncertain. |
| PetClinic re-index | 61.1 s, 50 files, 278 chunks |
| Gson graph in Neo4j | 3,075 chunk nodes + 3,715 id-less nodes = 6,790. Edges: CALLS 33,887; IMPORTS 3,338; DEFINES 2,524; EXTENDS 73 |
| DEFINES edges exist | Yes. Samples: `ExclusionStrategy -> shouldSkipField`, `ExclusionStrategy -> shouldSkipClass` |
| Enums indexed | **No.** 2 of 30 Gson expected answers were enums and are missing from the index |

Not verified: that the 3,715 id-less nodes are all external-library stubs, and what share of
indexing time is embedding versus Neo4j writes.

## 6. Limitations

- Small question sets (28 and 30), written by us; the answer key is single-symbol, so a correct but different chunk counts as a miss.
- One run per config; latency varies run to run.
- Questions avoid exact names on purpose. Results for identifier-style questions would differ, especially for BM25.
- PetClinic is a small repo (278 chunks), so retrieval is easier than on Gson.

## 7. See the graph yourself

Neo4j Browser: http://localhost:7474 — log in with your `NEO4J_USER` / `NEO4J_PASSWORD`
(the local Docker setup in the README uses the dev default from `.env.example`).

```
MATCH (p:Chunk {ns:'gson_v2'})-[:DEFINES]->(c) RETURN p,c LIMIT 40
MATCH (n:Chunk {ns:'gson_v2'})-[r:CALLS]->(m) RETURN n,r,m LIMIT 40
MATCH (p:Chunk {ns:'petclinic_v2'})-[:DEFINES]->(c) RETURN p,c LIMIT 40
```

## 8. Suggested next steps (not done)

- Add enum support to the Java chunker, then re-index and re-run.
- Try fusing only T1 + T3 with RRF, and a smaller weight for T2.
- Grow the question set and add identifier-style questions.
