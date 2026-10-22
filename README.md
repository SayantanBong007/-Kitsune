# 🦊 Kitsune — Repository Knowledge Engine

> **A system that makes AI truly *understand* your codebase — not just search it.**

Kitsune indexes source code into a hybrid knowledge graph combining semantic embeddings,
a real dependency graph, and keyword search. When any AI agent asks a question about your
code, Kitsune retrieves the exact relevant code *plus its full execution context* — in
milliseconds, at scale, on your own infrastructure.

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install sentence-transformers chromadb qdrant-client rank-bm25 networkx \
            tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-c tree-sitter-cpp tree-sitter-go tree-sitter-java tree-sitter-rust \
            openai python-dotenv
```

### 2. Configure Your API Key

```bash
cd kitsune
cp .env.example .env
```

Open `.env` and add your Groq API key (free at [console.groq.com](https://console.groq.com)):

```env
GROQ_API_KEY=your-groq-api-key-here
```

That's it — everything else has sensible defaults. You're ready to go.

### 3. Index a Repository & Ask Questions

```python
from kitsune.engine import RepositoryKnowledgeEngine

engine = RepositoryKnowledgeEngine()

# Index a repository (incremental — only changed files on subsequent runs)
metrics = engine.index_repository("/path/to/your/repo")
print(metrics.summary())

# Ask a question about the codebase
answer = engine.ask("How does the authentication system work?")
print(answer)

# Retrieve context for an AI agent's prompt
context = engine.search_semantic("What does the payment flow look like?")

# Analyze the blast radius of changing a function
impact = engine.get_impact_analysis("process_payment", max_depth=3)
print(f"This change impacts {impact['total_impact']} functions")
```

---

## ⚙️ Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in
your values. **Only `GROQ_API_KEY` is required** — everything else is optional with sensible
defaults.

### LLM Configuration (Required)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROQ_API_KEY` | ✅ Yes | — | Your Groq API key for LLM inference |
| `LLM_MODEL` | No | `openai/gpt-oss-20b` | The LLM model to use |
| `LLM_BASE_URL` | No | `https://api.groq.com/openai/v1` | API base URL (change for OpenAI, OpenRouter, Ollama) |
| `LLM_API_KEY` | No | — | Alternative API key (takes precedence over `GROQ_API_KEY`) |

**Using a different LLM provider?**

```env
# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
GROQ_API_KEY=sk-your-openai-key
LLM_MODEL=gpt-4o-mini

# OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
GROQ_API_KEY=your-openrouter-key
LLM_MODEL=anthropic/claude-3.5-sonnet

# Ollama (local, fully offline)
LLM_BASE_URL=http://localhost:11434/v1
GROQ_API_KEY=ollama
LLM_MODEL=llama3.1:8b
```

### Vector Store (Optional — defaults to ChromaDB)

Kitsune uses **Qdrant** if `QDRANT_URL` is set, otherwise falls back to **ChromaDB**
(embedded, no server needed).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `QDRANT_URL` | No | — | Qdrant server URL (enables Qdrant mode) |
| `QDRANT_API_KEY` | No | — | API key for Qdrant Cloud |
| `QDRANT_LOCAL_PATH` | No | — | Path for embedded Qdrant (no server) |
| `CHROMA_PATH` | No | `./data/kitsune/chroma` | ChromaDB storage path (fallback mode) |

```env
# Qdrant Cloud (production — free tier at cloud.qdrant.io)
QDRANT_URL=https://your-cluster.qdrant.io
QDRANT_API_KEY=your-qdrant-api-key

# Qdrant embedded (local, no server, Qdrant engine)
QDRANT_LOCAL_PATH=./data/qdrant

# ChromaDB (default — no config needed, works out of the box)
```

### Graph Store (Optional — defaults to NetworkX)

Kitsune uses **Neo4j** if available, otherwise falls back to **NetworkX** (in-memory with
disk persistence via pickle).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEO4J_URI` | No | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | No | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | No | `kitsune_password` | Neo4j password |

```env
# Neo4j via Docker:
#   docker run -p 7687:7687 -p 7474:7474 -e NEO4J_AUTH=neo4j/kitsune_password neo4j:latest
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=kitsune_password

# NetworkX (default — no config needed, persists to disk automatically)
```

### Embedding Model (Optional — defaults to local model)

Kitsune uses **BAAI/bge-small-en-v1.5** (384-dim, 120MB, runs on CPU). Downloaded once
and cached locally — no API key needed.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HF_HUB_OFFLINE` | No | — | Set to `1` to force offline mode (cached model only) |
| `TRANSFORMERS_OFFLINE` | No | — | Set to `1` to force offline mode |

---

## 📁 Project Structure

```
kitsune/
├── .env.example                    ← Copy to .env and fill in your API key
├── KITSUNE_ARCHITECTURE.md         ← Full architecture document (for architects)
├── __init__.py                     ← Public API entry point
├── indexer.py                      ← K1+K2+K3 orchestrator (parallel, incremental)
│
├── engine/
│   └── knowledge_engine.py         ← RepositoryKnowledgeEngine (the main facade)
│
├── models/
│   ├── chunk.py                    ← Chunk dataclass (the atomic unit)
│   ├── concept.py                  ← Business concept entity
│   └── metrics.py                  ← IndexMetrics, QueryMetrics, Timer
│
├── parser/                         ← K1: PARSE
│   ├── treesitter_parser.py        ← 8-language tree-sitter parser
│   ├── extractor.py                ← Extract calls/inheritance/imports/decorators
│   └── chunker.py                  ← AST-aware chunking + nested methods
│
├── embedder/                       ← K2: EMBED
│   ├── embedder.py                 ← Singleton model (batched, memory-safe)
│   ├── contextualizer.py           ← Template + LLM context notes
│   └── reranker.py                 ← Cross-encoder reranking
│
├── store/                          ← STORAGE BACKENDS
│   ├── qdrant_store.py             ← Qdrant (server, scalable) + ChromaDB fallback
│   ├── vector_store.py             ← ChromaDB (embedded, for dev/CI)
│   ├── graph_store.py              ← Neo4j + NetworkX (pickle-persisted, O(1) index)
│   └── keyword_store.py            ← BM25 (dict-based, O(1) upsert)
│
├── graph/
│   └── traversal.py                ← Multi-hop BFS, path finding, impact analysis
│
├── query/                          ← QUERY TIME
│   ├── router.py                   ← Intent classification → strategy selection
│   ├── cache.py                    ← LRU cache (exact + semantic similarity)
│   ├── hybrid_search.py            ← Vector + Graph + Keyword + Rerank + Budget
│   ├── context_budget.py           ← Prevents LLM context overflow
│   └── synthesizer.py              ← Context formatting + LLM answer
│
├── concepts/                       ← BUSINESS CONCEPT LAYER
│   ├── extractor.py                ← Discover concepts from naming patterns
│   └── store.py                    ← Concept persistence + risk scoring
│
└── agents/                         ← KITSUNE'S OWN AI AGENTS
    ├── investigator.py             ← Bug report → root cause analysis
    └── planner.py                  ← Root cause → fix payload
```

---

## 💻 Supported Languages

| Language | Extensions | | Language | Extensions |
|----------|-----------|---|----------|-----------|
| 🐍 Python | `.py` | | 🔵 C | `.c`, `.h` |
| 🟨 JavaScript | `.js`, `.jsx` | | 🟦 C++ | `.cpp`, `.hpp`, `.cc` |
| 🟦 TypeScript | `.ts`, `.tsx` | | 🐹 Go | `.go` |
| ☕ Java | `.java` | | 🦀 Rust | `.rs` |

Adding a new language requires only installing its tree-sitter package and adding one line
to the language registry in `parser/treesitter_parser.py`.

---

## 🔌 API Reference

### `RepositoryKnowledgeEngine`

The single entry point. All methods are available after indexing a repository.

```python
from kitsune.engine import RepositoryKnowledgeEngine

engine = RepositoryKnowledgeEngine()
```

#### Indexing

```python
# Index a repository (incremental by default — only re-indexes changed files)
metrics = engine.index_repository("/path/to/repo", incremental=True)
print(metrics.summary())
# Output:
#   Kitsune Indexing Summary
#   ========================================
#     Files indexed:    42
#     Files skipped:    8
#     Total chunks:     156
#     Graph edges:      234
#     Concepts:         12
#     Indexing time:    3.42s
```

#### Asking Questions

```python
# Ask a question and get a cited answer
answer = engine.ask("What happens if I change the get_user function?")

# Stream an answer (for chat UIs)
for chunk in engine.ask_streaming("Explain the authentication system"):
    print(chunk, end="", flush=True)

# Retrieve formatted context for an AI agent's prompt
context = engine.search_semantic("how does the payment flow work?")

# Get raw search results (for programmatic use)
results = engine.search("how does the payment flow work?")
# → {"primary_chunks": [...], "dependency_chunks": [...], "keyword_chunks": [...], ...}
```

#### Graph Queries

```python
# What calls this function? (multi-hop, depth=2)
callers = engine.get_upstream_callers("charge_stripe", depth=2)

# What does this function call? (multi-hop, depth=3)
deps = engine.get_downstream_dependencies("handle_checkout", depth=3)

# Full call graph topology
graph = engine.get_call_graph("process_payment", depth=2)

# Inheritance tree
parents = engine.get_inheritance_tree("PaymentService")

# Impact analysis — blast radius of changing a function
impact = engine.get_impact_analysis("process_payment", max_depth=3)
# → {"upstream": [...], "downstream": [...], "total_impact": 12}
```

#### Concept Queries

```python
# List all business concepts
concepts = engine.get_all_concepts()
for c in concepts:
    print(f"  {c['name']}: {c['chunk_count']} chunks, risk={c['risk_level']}")

# Get high-risk concepts
high_risk = engine.get_high_risk_concepts()

# Get a concept's dependency tree
deps = engine.get_concept_dependencies("Payment")
```

#### Code Lookup

```python
# Get exact source code for a function/class
code = engine.get_source_code("get_user")
```

#### System Stats

```python
stats = engine.stats()
# → {"vector_store": {"count": 156, "backend": "chromadb"},
#    "graph_store": {"nodes": 200, "edges": 234, "backend": "networkx"},
#    "keyword_store": {"count": 156},
#    "concept_store": {"count": 12},
#    "query_cache": {"size": 45, "hit_rate_percent": 42.3}}
```

---

## 🚀 Deployment Options

### Option A: Quick Start (Pilot / Small Team)

Zero infrastructure — just an API key.

```env
GROQ_API_KEY=your-key
```

| Property | Value |
|----------|-------|
| Cost | **Free** (except LLM API calls) |
| Setup | `pip install` + `.env` — no servers |
| Capacity | ~10,000 files |
| Storage | ChromaDB (embedded) + NetworkX (in-memory) |

### Option B: Production (Medium Team)

```env
GROQ_API_KEY=your-key
QDRANT_URL=https://your-cluster.qdrant.io
QDRANT_API_KEY=your-qdrant-key
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
```

| Property | Value |
|----------|-------|
| Cost | Low (self-hosted) |
| Setup | Docker Compose for Qdrant + Neo4j |
| Capacity | ~50,000 files |
| Concurrent users | 10-50 |

### Option C: Enterprise (Large Team, Air-Gapped)

```env
LLM_BASE_URL=http://localhost:11434/v1
GROQ_API_KEY=ollama
LLM_MODEL=llama3.1:8b
QDRANT_URL=https://your-cluster.qdrant.io
QDRANT_API_KEY=your-qdrant-key
NEO4J_URI=bolt://neo4j-cluster:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

| Property | Value |
|----------|-------|
| Cost | Moderate (self-hosted GPU for LLM) |
| Setup | Kubernetes / managed services |
| Capacity | 100,000+ files |
| Code leaves network | **Never** (local LLM + local embeddings) |

---

## 🔒 Security & Privacy

| Concern | How Kitsune Addresses It |
|---------|------------------------|
| **Code leaves the network** | Embeddings use a local model (120MB, CPU). Code never leaves your machine for indexing. |
| **LLM API sees code context** | Only the synthesis step calls an LLM. Replace with a **local LLM** (Ollama) for full air-gap. |
| **Vector DB access** | Qdrant supports API key auth + TLS. ChromaDB is local-only. |
| **Graph DB access** | Neo4j supports auth + RBAC. NetworkX is in-process. |
| **Telemetry** | ChromaDB telemetry disabled. No external analytics. No phone-home. |
| **Data residency** | All data stays on your infrastructure. |

---

## 📊 Performance

| Codebase Size | First Index | Incremental Index | Query (cache miss) | Query (cache hit) |
|--------------|-------------|-------------------|--------------------|-------------------|
| 100 files | ~5s | ~1s | < 100ms | ~0ms |
| 1,000 files | ~30s | ~2-3s | < 200ms | ~0ms |
| 10,000 files | ~5min | ~5-10s | < 500ms | ~0ms |
| 50,000 files | ~20min | ~10-30s | < 1s | ~0ms |

---

## 📚 Documentation

- **[KITSUNE_ARCHITECTURE.md](KITSUNE_ARCHITECTURE.md)** — Full architecture document with
  flow diagrams, sequence diagrams, and detailed working. Share this with your architect.

---

## 📦 Requirements

```
sentence-transformers>=2.2
chromadb>=0.4
qdrant-client>=1.9          # optional (falls back to ChromaDB)
rank-bm25>=0.2
networkx>=3.0
neo4j>=5.0                   # optional (falls back to NetworkX)
openai>=1.30
python-dotenv>=1.0
tree-sitter>=0.21
tree-sitter-python>=0.21
tree-sitter-javascript>=0.21
tree-sitter-typescript>=0.21
tree-sitter-c>=0.21
tree-sitter-cpp>=0.22
tree-sitter-go>=0.21
tree-sitter-java>=0.21
tree-sitter-rust>=0.21
```

---

*Kitsune — because a fox with many tails sees more than one.*
