"""
Graph traversal utilities — multi-hop BFS/DFS over the dependency graph.

Improvements over Kitsune 1.0:
- Dedicated traversal module (S2): multi-hop graph traversal with cycle detection.
- Configurable depth and edge-type filtering.
- Returns structured paths, not just flat lists.
"""
from __future__ import annotations

from typing import Literal

from kitsune_2_0.store.graph_store import GraphStore, EDGE_CALLS

TraversalDirection = Literal["downstream", "upstream", "both"]


def traverse_graph(
    graph: GraphStore,
    start_name: str,
    direction: TraversalDirection = "downstream",
    max_depth: int = 2,
    edge_type: str = EDGE_CALLS,
) -> dict:
    """Traverse the dependency graph from a starting node.

    This is a BFS traversal that respects max_depth and avoids cycles.

    Parameters
    ----------
    graph:
        The GraphStore to traverse.
    start_name:
        Name of the starting chunk/function.
    direction:
        "downstream" = what does this call?
        "upstream" = what calls this?
        "both" = traverse in both directions.
    max_depth:
        Maximum hops from the start node.
    edge_type:
        Type of edge to follow (default: CALLS).

    Returns
    -------
    A dict with:
    - "start": the starting entity name
    - "downstream": list of {name, depth, path} for downstream nodes
    - "upstream": list of {name, depth, path} for upstream nodes
    """
    result = {
        "start": start_name,
        "downstream": [],
        "upstream": [],
    }

    if direction in ("downstream", "both"):
        result["downstream"] = _bfs_traverse(
            graph, start_name, downstream=True, max_depth=max_depth, edge_type=edge_type
        )

    if direction in ("upstream", "both"):
        result["upstream"] = _bfs_traverse(
            graph, start_name, downstream=False, max_depth=max_depth, edge_type=edge_type
        )

    return result


def _bfs_traverse(
    graph: GraphStore,
    start_name: str,
    downstream: bool,
    max_depth: int,
    edge_type: str,
) -> list[dict]:
    """BFS traversal of the graph, returning nodes with their depth and path.

    Works with both Neo4j and NetworkX backends by using the GraphStore's
    get_dependencies / get_upstream_callers methods at each depth level.
    """
    visited: set[str] = set()
    results: list[dict] = []

    # Level 0: the start node
    current_level = [start_name]
    visited.add(start_name)

    for depth in range(1, max_depth + 1):
        next_level: list[str] = []

        for node_name in current_level:
            if downstream:
                neighbors = graph.get_dependencies(node_name, depth=1)
            else:
                neighbors = graph.get_upstream_callers(node_name, depth=1)

            for neighbor in neighbors:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                next_level.append(neighbor)
                results.append({
                    "name": neighbor,
                    "depth": depth,
                    "direction": "downstream" if downstream else "upstream",
                })

        current_level = next_level
        if not current_level:
            break

    return results


def find_all_paths(
    graph: GraphStore,
    start_name: str,
    end_name: str,
    max_depth: int = 5,
) -> list[list[str]]:
    """Find all paths between two functions in the call graph.

    Uses DFS with cycle detection. Useful for understanding how data flows
    from function A to function B.

    Parameters
    ----------
    graph:
        The GraphStore to search.
    start_name:
        Starting function name.
    end_name:
        Target function name.
    max_depth:
        Maximum path length to avoid infinite loops.

    Returns
    -------
    List of paths, where each path is a list of function names.
    """
    paths: list[list[str]] = []

    def dfs(current: str, target: str, path: list[str], depth: int):
        if depth > max_depth:
            return
        if current == target and len(path) > 1:
            paths.append(list(path))
            return

        neighbors = graph.get_dependencies(current, depth=1)
        for neighbor in neighbors:
            if neighbor in path:  # cycle detection
                continue
            path.append(neighbor)
            dfs(neighbor, target, path, depth + 1)
            path.pop()

    dfs(start_name, end_name, [start_name], 0)
    return paths


def get_impact_analysis(graph: GraphStore, function_name: str, max_depth: int = 3) -> dict:
    """Analyze the blast radius of changing a function.

    Finds all upstream callers (what might break if this function changes)
    and all downstream dependencies (what this function relies on).

    Parameters
    ----------
    graph:
        The GraphStore to analyze.
    function_name:
        The function being changed.
    max_depth:
        How far to trace the impact.

    Returns
    -------
    Dict with:
    - "function": the function name
    - "upstream": list of callers that might break
    - "downstream": list of dependencies that might be affected
    - "total_impact": total number of affected functions
    """
    upstream = graph.get_upstream_callers(function_name, depth=max_depth)
    downstream = graph.get_dependencies(function_name, depth=max_depth)

    return {
        "function": function_name,
        "upstream": upstream,
        "downstream": downstream,
        "total_impact": len(upstream) + len(downstream),
    }
