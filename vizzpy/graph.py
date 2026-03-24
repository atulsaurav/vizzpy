"""
Converts raw (caller, callee) edges into a JSON-serializable graph structure
that the frontend and render layer both consume.
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path

from .parser.project import analyze_project
from .parser.scope import FuncSpan


def aggregate_to_modules(graph: dict) -> dict:
    """
    Collapse a function-level graph to module-level.

    Each module becomes a single node.  Intra-module edges are dropped;
    cross-module edges are aggregated by summing the underlying call counts.
    A module is marked external only when *all* of its functions are external.

    The returned dict has the same shape as build_graph():
      {nodes, edges, modules}
    where modules maps each module name to a list containing just itself
    (so renderers that iterate modules still work without subgraph nesting).
    """
    node_by_id = {n["id"]: n for n in graph["nodes"]}

    # A module is external only if every node in it is external.
    module_external: dict[str, bool] = {}
    for n in graph["nodes"]:
        mod = n["module"]
        if mod not in module_external:
            module_external[mod] = n["external"]
        elif not n["external"]:
            module_external[mod] = False

    # Set module to the parent namespace so renderers assign each module node to its
    # parent cluster rather than a redundant same-named wrapper cluster.
    nodes = []
    for mod in sorted(module_external):
        last_dot = mod.rfind(".")
        parent_ns = mod[:last_dot] if last_dot != -1 else mod
        nodes.append({
            "id": mod,
            "label": mod,
            "module": parent_ns,
            "docstring": None,
            "external": module_external[mod],
        })

    # Aggregate cross-module edges
    edge_counts: dict[tuple[str, str], int] = {}
    for e in graph["edges"]:
        src_node = node_by_id.get(e["source"])
        tgt_node = node_by_id.get(e["target"])
        if not src_node or not tgt_node:
            continue
        src_mod = src_node["module"]
        tgt_mod = tgt_node["module"]
        if src_mod == tgt_mod:
            continue  # drop intra-module calls
        key = (src_mod, tgt_mod)
        edge_counts[key] = edge_counts.get(key, 0) + e.get("count", 1)

    edges = [
        {"source": src, "target": tgt, "count": cnt}
        for (src, tgt), cnt in sorted(edge_counts.items())
    ]

    # modules dict: parent namespace → [direct child module IDs].
    # Top-level modules (no dotted parent) are left unclustered.
    modules: dict[str, list[str]] = {}
    for mod in sorted(module_external):
        last_dot = mod.rfind(".")
        if last_dot == -1:
            continue  # top-level: no parent namespace cluster
        parent_ns = mod[:last_dot]
        modules.setdefault(parent_ns, []).append(mod)

    return {"nodes": nodes, "edges": edges, "modules": modules}


def build_graph(root: Path) -> dict:
    """
    Analyze the project at *root* and return a dict with shape:

    {
      "nodes": [{"id": str, "label": str, "module": str, "docstring": str|null}],
      "edges": [{"source": str, "target": str, "count": int}],
      "modules": {"module.name": ["node_id", ...]}
    }
    """
    edges_raw, node_info, project_names = analyze_project(root)

    # Collect unique node ids from edges only (skip isolates)
    node_ids: set[str] = set()
    for src, tgt in edges_raw:
        node_ids.add(src)
        node_ids.add(tgt)

    # Deduplicate edges and count multiplicity
    edge_counts: dict[tuple[str, str], int] = defaultdict(int)
    for src, tgt in edges_raw:
        edge_counts[(src, tgt)] += 1

    # Build node list
    nodes = []
    for nid in sorted(node_ids):
        span: FuncSpan | None = node_info.get(nid)
        nodes.append({
            "id": nid,
            "label": span.display_name if span else _fallback_label(nid),
            "module": span.module_name if span else _fallback_module(nid),
            "docstring": span.docstring if span else None,
            # External: resolved via import but not defined in the project.
            # __main__ sentinels are synthetic project callers, not external.
            "external": nid not in project_names and not nid.endswith(".__main__"),
        })

    # Build module → [node_id] grouping
    modules: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        modules[node["module"]].append(node["id"])

    return {
        "nodes": nodes,
        "edges": [
            {"source": src, "target": tgt, "count": cnt}
            for (src, tgt), cnt in sorted(edge_counts.items())
        ],
        "modules": dict(modules),
    }


def _fallback_label(qname: str) -> str:
    """Best-effort display label when a node has no FuncSpan (e.g. __main__ calls)."""
    return qname.split(".")[-1]


def _fallback_module(qname: str) -> str:
    parts = qname.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else qname
