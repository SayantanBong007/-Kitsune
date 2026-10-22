"""
Kitsune MCP server — gives coding agents (Claude Code, Cursor, …) Kitsune's
retrieval and call graph as tools.

Run (stdio):
    python /path/to/kitsune/mcp_server.py

Register with Claude Code:
    claude mcp add kitsune --env KITSUNE_REPO=/path/to/your/repo -- python /path/to/kitsune/mcp_server.py

Environment:
    KITSUNE_REPO        default repository for index_repository (optional)
    KITSUNE_NAMESPACE   project name when sharing Qdrant/Neo4j (optional)
    KITSUNE_DATA_DIR    where local indexes are stored (default: kitsune/data/kitsune)
    + the usual Kitsune settings (QDRANT_URL, NEO4J_URI, KITSUNE_CONTEXT_BUDGET, …)

No LLM is needed: the agent calling these tools does the reasoning.

Status: experimental. The tools work when called in-process, but over the stdio
transport the first tool call (which loads the engine) can hang — under
investigation.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

# Make `import kitsune` work when this file is run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

# stdout carries the MCP protocol — all logging goes to stderr
logging.basicConfig(stream=sys.stderr, level=os.getenv("KITSUNE_LOG_LEVEL", "WARNING"))

mcp = FastMCP(
    "kitsune",
    instructions=(
        "Kitsune indexes a code repository into semantic, keyword, and call-graph indexes. "
        "Call index_repository once (and again after editing files — it only re-indexes "
        "what changed). Then use search_code to find code relevant to a question, "
        "find_callers / find_callees / impact_analysis for exact call relationships, "
        "and get_source to read a specific function or class."
    ),
)

_engine = None
_engine_lock = threading.Lock()
_indexed_repo: str | None = None


def _get_engine():
    """Create the engine on first use (loads the embedding model, ~seconds)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            from kitsune.engine import RepositoryKnowledgeEngine
            _engine = RepositoryKnowledgeEngine(data_dir=os.getenv("KITSUNE_DATA_DIR") or None)
        return _engine


def _require_index():
    engine = _get_engine()
    if engine.vector_store.count() == 0:
        raise ValueError("The index is empty. Call index_repository first.")
    return engine


def _location(record: dict) -> str:
    return f"{record.get('file_path')}:{record.get('start_line')}"


def _related(engine, symbol: str, direction: str, depth: int,
             include_external: bool = False) -> dict:
    confirmed = engine.graph_store.get_related(symbol, direction=direction, depth=depth,
                                               include_external=include_external)
    with_possible = engine.graph_store.get_related(symbol, direction=direction, depth=depth,
                                                   include_external=include_external,
                                                   include_ambiguous=True)
    seen = {(r["qualified_name"], r["file_path"]) for r in confirmed}
    shape = lambda r: {"symbol": r["qualified_name"], "location": _location(r),  # noqa: E731
                       "hops": r["depth"]}
    return {
        "confirmed": [shape(r) for r in confirmed],
        # Calls Kitsune could not pin to one definition (e.g. obj.save() with
        # several `save` methods) — check these by reading the code.
        "possible": [shape(r) for r in with_possible
                     if (r["qualified_name"], r["file_path"]) not in seen],
    }


@mcp.tool()
def index_repository(repo_path: str = "", full: bool = False) -> dict:
    """Index (or refresh) a repository so it can be searched.

    Incremental by default: only files changed since the last run are
    re-parsed, so call it again after editing code. Set full=True to rebuild
    everything. repo_path defaults to KITSUNE_REPO.
    """
    global _indexed_repo
    repo = repo_path or os.getenv("KITSUNE_REPO", "")
    if not repo:
        raise ValueError("Pass repo_path or set KITSUNE_REPO.")
    if not Path(repo).is_dir():
        raise ValueError(f"Not a directory: {repo}")
    engine = _get_engine()
    metrics = engine.index_repository(repo, incremental=not full)
    _indexed_repo = repo
    return {
        "repo": repo,
        "files_indexed": metrics.total_files,
        "files_unchanged": metrics.skipped_files,
        "chunks_indexed": metrics.total_chunks,
        "call_edges": metrics.total_edges,
        "concepts": metrics.total_concepts,
        "errors": metrics.errors[:20],
        "total_chunks_in_index": engine.vector_store.count(),
    }


@mcp.tool()
def search_code(question: str, max_chars: int = 0) -> str:
    """Find the code relevant to a natural-language question.

    Returns code chunks (with file paths and lines) from semantic search,
    keyword search, and the call graph (callers/callees), trimmed to a
    character budget (max_chars, default KITSUNE_CONTEXT_BUDGET / 10000).
    Good first step for "how does X work?", "where is Y handled?".
    """
    engine = _require_index()
    from kitsune.query.router import route_query
    plan = route_query(question)
    results = engine.searcher.search(question, plan=plan, max_context_chars=max_chars or None)
    header = f"[query type: {results['query_type']}]\n"
    return header + engine.synthesizer.format_context(results)


@mcp.tool()
def find_callers(symbol: str, depth: int = 1) -> dict:
    """Who calls this function/method? symbol is a name or Class.method.

    depth > 1 follows callers of callers. Results are split into
    "confirmed" (resolved by imports/types/receivers) and "possible"
    (same-named methods Kitsune could not tell apart).
    """
    engine = _require_index()
    return {"symbol": symbol, **_related(engine, symbol, "callers", max(1, depth))}


@mcp.tool()
def find_callees(symbol: str, depth: int = 1, include_external: bool = False) -> dict:
    """What does this function/method call? symbol is a name or Class.method.

    include_external adds calls into code outside the repository
    (standard library, third-party packages) by name.
    """
    engine = _require_index()
    return {"symbol": symbol,
            **_related(engine, symbol, "callees", max(1, depth), include_external)}


@mcp.tool()
def impact_analysis(symbol: str, max_depth: int = 3) -> dict:
    """What could break if this function/method changes?

    Lists everything that calls it, directly or transitively (up to
    max_depth hops), with locations — the code to review or re-test.
    """
    engine = _require_index()
    callers = _related(engine, symbol, "callers", max(1, max_depth))
    callees = _related(engine, symbol, "callees", 1)
    return {
        "symbol": symbol,
        "affected_callers": callers["confirmed"],
        "possibly_affected": callers["possible"],
        "depends_on": callees["confirmed"],
        "total_affected": len(callers["confirmed"]),
    }


@mcp.tool()
def get_source(symbol: str) -> list[dict]:
    """Source code of a function, method, or class (name or Class.method).

    Returns every match when the name is defined in several places.
    """
    engine = _require_index()
    name = symbol.split(".")[-1]
    matches = engine.vector_store.get_by_name(name)
    if "." in symbol:
        matches = [m for m in matches if m.get("qualified_name") == symbol] or matches
    if not matches:
        raise ValueError(f"No definition named {symbol!r} in the index.")
    return [{"symbol": m.get("qualified_name"), "location": _location(m),
             "type": m.get("chunk_type"), "code": m.get("text", "")}
            for m in matches[:10]]


@mcp.tool()
def list_concepts() -> list[dict]:
    """Business/domain concepts in the codebase (e.g. Payment, Order, Auth),
    with how much code each covers, what they depend on, and a risk level
    based on how many other concepts use them."""
    engine = _require_index()
    return [{"name": c["name"], "chunks": c["chunk_count"], "risk": c["risk_level"],
             "depends_on": sorted(c.get("depends_on", [])), "used_by": sorted(c.get("used_by", []))}
            for c in engine.get_all_concepts()]


@mcp.tool()
def index_status() -> dict:
    """What is indexed: repository, chunk/graph counts, and storage backends."""
    engine = _get_engine()
    return {"repo": _indexed_repo or os.getenv("KITSUNE_REPO"),
            "namespace": engine.namespace or None, **engine.stats()}


def main():
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
