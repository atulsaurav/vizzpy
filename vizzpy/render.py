"""
Headless SVG rendering via the graphviz Python package.

The graphviz package is a thin wrapper around the `dot` binary (Graphviz).
Install Graphviz system package first:
  macOS:  brew install graphviz
  Ubuntu: apt install graphviz
"""
from __future__ import annotations
from pathlib import Path

from .graph import aggregate_to_modules, build_graph


def render_mermaid(project_root: Path, output_path: Path, level: str = "function", layout: str = "elk") -> None:
    """
    Analyze *project_root* and write the call graph as a Mermaid markdown
    flowchart to *output_path*.

    *level* controls granularity: ``"function"`` (default) shows individual
    functions; ``"module"`` collapses each module to a single node.

    *layout* selects the Mermaid layout engine: ``"elk"`` (default, handles
    nested subgraphs well) or ``"dagre"`` (classic Mermaid default).
    """
    graph_data = build_graph(project_root)
    if level == "module":
        graph_data = aggregate_to_modules(graph_data)
    md = _to_mermaid(graph_data, level=level, layout=layout)
    output_path.write_text(md, encoding="utf-8")


def _build_module_tree(module_names: list[str]) -> tuple[dict, list[str]]:
    """
    Build a tree from dotted module names.

    Returns ``(nodes, top_level_keys)`` where ``nodes`` maps each full dotted
    path to ``{"short": last_segment, "children": set_of_full_paths}``.
    """
    nodes: dict[str, dict] = {}
    for m in module_names:
        parts = m.split(".")
        for i, part in enumerate(parts):
            prefix = ".".join(parts[: i + 1])
            if prefix not in nodes:
                nodes[prefix] = {"short": part, "children": set()}
            if i > 0:
                parent = ".".join(parts[:i])
                nodes[parent]["children"].add(prefix)
    top_level = sorted(
        k for k in nodes if "." not in k or ".".join(k.split(".")[:-1]) not in nodes
    )
    return nodes, top_level


# Subgraph fill/stroke/label colours per nesting depth (0 = outermost).
# Each tuple is (fill, stroke, label_color).
# Chosen to be distinguishable and readable in both light and dark mode.
_SUBGRAPH_DEPTH_STYLES: list[tuple[str, str, str]] = [
    ("#dbeafe", "#3b82f6", "#1e3a8a"),  # depth 0 – blue  (top-level pkg)
    ("#dcfce7", "#16a34a", "#14532d"),  # depth 1 – green (sub-package)
    ("#fef9c3", "#ca8a04", "#713f12"),  # depth 2 – amber (deep module)
    ("#ffe4e6", "#e11d48", "#881337"),  # depth 3 – rose
    ("#ccfbf1", "#0d9488", "#134e4a"),  # depth 4 – teal
]
# Fallback for depths beyond the palette length
_SUBGRAPH_DEPTH_STYLES_FALLBACK = ("#f3e8ff", "#9333ea", "#3b0764")  # purple

_EXT_OUTER_STYLE = ("#f1f5f9", "#64748b", "#1e293b")  # slate – external wrapper
_EXT_INNER_STYLE = ("#f8fafc", "#94a3b8", "#334155")  # light grey – stdlib subgraphs


def _subgraph_style(depth_index: int) -> tuple[str, str, str]:
    if depth_index < len(_SUBGRAPH_DEPTH_STYLES):
        return _SUBGRAPH_DEPTH_STYLES[depth_index]
    return _SUBGRAPH_DEPTH_STYLES_FALLBACK


def _emit_module_subtree(
    keys: list[str],
    tree: dict,
    modules: dict[str, list[str]],
    node_by_id: dict,
    depth: int,
    lines: list[str],
    style_lines: list[str],
    depth_index: int = 0,
) -> None:
    """Recursively emit nested Mermaid ``subgraph`` blocks and style directives."""
    ind = "    " * depth
    fill, stroke, label_color = _subgraph_style(depth_index)
    for full_path in sorted(keys):
        node = tree[full_path]
        safe = full_path.replace(".", "_").replace("-", "_")
        is_actual = full_path in modules
        has_children = bool(node["children"])
        if not (is_actual or has_children):
            continue
        lines.append(f'{ind}subgraph {safe}["{full_path}"]')
        style_lines.append(
            f"    style {safe}"
            f" fill:{fill},stroke:{stroke},color:{label_color},stroke-width:2px"
        )
        if has_children:
            lines.append(f"{ind}    direction LR")
        if is_actual:
            for nid in modules[full_path]:
                n = node_by_id[nid]
                lines.append(f'{ind}    {_mermaid_id(nid)}["{n["label"]}"]')
        if has_children:
            _emit_module_subtree(
                sorted(node["children"]),
                tree,
                modules,
                node_by_id,
                depth + 1,
                lines,
                style_lines,
                depth_index + 1,
            )
        lines.append(f"{ind}end")


def _to_mermaid(graph_data: dict, level: str = "function", layout: str = "elk") -> str:
    node_by_id = {n["id"]: n for n in graph_data["nodes"]}
    external_ids = {n["id"] for n in graph_data["nodes"] if n.get("external")}

    lines = ["```mermaid"]
    if layout == "elk":
        lines.append('%%{init: {"flowchart": {"defaultRenderer": "elk"}} }%%')
    lines.append("flowchart LR")

    # Split modules into project vs external for subgraph grouping.
    # Works for both function and module level (module level uses parent-namespace clusters).
    project_modules: dict[str, list[str]] = {}
    external_modules: dict[str, list[str]] = {}
    for module_name, node_ids in graph_data["modules"].items():
        if all(nid in external_ids for nid in node_ids):
            external_modules[module_name] = node_ids
        else:
            project_modules[module_name] = node_ids

    style_lines: list[str] = []

    # Project subgraphs — hierarchically nested by dotted module path
    tree, top_keys = _build_module_tree(list(project_modules.keys()))
    _emit_module_subtree(
        top_keys, tree, project_modules, node_by_id, 1, lines, style_lines, depth_index=0
    )

    # Emit any nodes not covered by a subgraph (e.g. top-level modules with no parent namespace).
    clustered_ids = {nid for ids in graph_data["modules"].values() for nid in ids}
    for n in graph_data["nodes"]:
        if n["id"] not in clustered_ids:
            lines.append(f"    {_mermaid_id(n['id'])}[\"{n['label']}\"]")

    # External subgraphs — hierarchically nested under a single outer wrapper
    if external_modules:
        ext_fill, ext_stroke, ext_color = _EXT_OUTER_STYLE
        lines.append('    subgraph __ext__["external libraries"]')
        lines.append("        direction LR")
        style_lines.append(
            f"    style __ext__"
            f" fill:{ext_fill},stroke:{ext_stroke},color:{ext_color},stroke-width:2px"
        )
        ext_tree, ext_top = _build_module_tree(list(external_modules.keys()))
        ext_style_lines: list[str] = []
        _emit_module_subtree(
            ext_top, ext_tree, external_modules, node_by_id, 2, lines, ext_style_lines, depth_index=0
        )
        # Override ext inner-module colours with the neutral external palette
        inner_fill, inner_stroke, inner_color = _EXT_INNER_STYLE
        for full_path in ext_tree:
            safe = full_path.replace(".", "_").replace("-", "_")
            style_lines.append(
                f"    style {safe}"
                f" fill:{inner_fill},stroke:{inner_stroke},color:{inner_color},stroke-width:1px"
            )
        lines.append("    end")

    # Flush subgraph style directives
    lines.extend(style_lines)

    # Edges
    for edge in graph_data["edges"]:
        src = _mermaid_id(edge["source"])
        tgt = _mermaid_id(edge["target"])
        if edge["count"] > 1:
            lines.append(f"    {src} -->|\"{edge['count']}x\"| {tgt}")
        else:
            lines.append(f"    {src} --> {tgt}")

    # Style external nodes
    if external_ids:
        lines.append(
            "    classDef external"
            " fill:#f0f0f0,stroke:#aaaaaa,color:#888888,stroke-dasharray:4"
        )
        lines.append(
            "    class " + ",".join(_mermaid_id(nid) for nid in sorted(external_ids))
            + " external"
        )

    lines.append("```")
    return "\n".join(lines) + "\n"


def _mermaid_id(qname: str) -> str:
    """Convert a qualified name to a safe Mermaid node ID."""
    return qname.replace(".", "__").replace("-", "_") + "_"


def _dot_tooltip(n: dict) -> str:
    """Return the tooltip string for a Graphviz node: qname + docstring (if any)."""
    parts = [n["id"]]
    doc = (n.get("docstring") or "").strip()
    if doc:
        parts.append(doc)
    return "\n".join(parts)


def _add_dot_cluster_tree(
    parent: "graphviz.Digraph",
    keys: list[str],
    tree: dict,
    modules: dict[str, list[str]],
    node_by_id: dict,
    depth_index: int,
    is_external: bool = False,
) -> None:
    """Recursively add nested Graphviz cluster subgraphs mirroring the module hierarchy."""
    for full_path in sorted(keys):
        node = tree[full_path]
        is_actual = full_path in modules
        has_children = bool(node["children"])
        if not (is_actual or has_children):
            continue
        if is_external:
            fill, stroke, label_color = _EXT_INNER_STYLE
            style, penwidth = "rounded,dashed,filled", "1"
        else:
            fill, stroke, label_color = _subgraph_style(depth_index)
            style, penwidth = "rounded,filled", "2"
        safe = full_path.replace(".", "_").replace("-", "_")
        with parent.subgraph(name=f"cluster_{safe}") as sub:
            sub.attr(
                label=full_path,
                style=style,
                fillcolor=fill,
                color=stroke,
                fontcolor=label_color,
                fontsize="10",
                penwidth=penwidth,
            )
            if is_actual:
                for nid in modules[full_path]:
                    n = node_by_id[nid]
                    if n.get("external"):
                        sub.node(
                            nid,
                            label=n["label"],
                            tooltip=_dot_tooltip(n),
                            fillcolor="#f0f0f0",
                            style="rounded,filled,dashed",
                            fontcolor="#888888",
                            color="#aaaaaa",
                        )
                    else:
                        sub.node(nid, label=n["label"], tooltip=_dot_tooltip(n))
            if has_children:
                _add_dot_cluster_tree(
                    sub,
                    sorted(node["children"]),
                    tree,
                    modules,
                    node_by_id,
                    depth_index + 1,
                    is_external,
                )


def render_svg(project_root: Path, output_path: Path, level: str = "function") -> None:
    """
    Analyze *project_root* and write the call graph as an SVG to *output_path*.

    *level* controls granularity: ``"function"`` (default) shows individual
    functions; ``"module"`` collapses each module to a single node.
    """
    import graphviz  # type: ignore  # requires system graphviz + pip package

    graph_data = build_graph(project_root)
    if level == "module":
        graph_data = aggregate_to_modules(graph_data)
    dot = _to_dot(graph_data, level=level)
    svg_source = dot.pipe(format="svg").decode("utf-8")
    output_path.write_text(svg_source, encoding="utf-8")


def _to_dot(graph_data: dict, level: str = "function") -> "graphviz.Digraph":
    import graphviz  # type: ignore

    node_by_id = {n["id"]: n for n in graph_data["nodes"]}

    dot = graphviz.Digraph(
        graph_attr={
            "rankdir": "LR",
            "fontname": "Helvetica",
            "splines": "ortho",
            "nodesep": "0.5",
            "ranksep": "1.0",
        },
        node_attr={
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#dbe9f4",
            "fontname": "Helvetica",
            "fontsize": "11",
        },
        edge_attr={
            "fontname": "Helvetica",
            "fontsize": "9",
        },
    )

    # Separate project vs external modules then build nested cluster hierarchy.
    # Works for both function and module level (module level uses parent-namespace clusters).
    project_modules: dict[str, list[str]] = {}
    external_modules: dict[str, list[str]] = {}
    for module_name, node_ids in graph_data["modules"].items():
        if all(node_by_id[nid].get("external") for nid in node_ids):
            external_modules[module_name] = node_ids
        else:
            project_modules[module_name] = node_ids

    tree, top_keys = _build_module_tree(list(project_modules.keys()))
    _add_dot_cluster_tree(dot, top_keys, tree, project_modules, node_by_id, 0)

    # Emit any nodes not covered by a cluster (e.g. top-level modules with no parent namespace).
    clustered_ids = {nid for ids in graph_data["modules"].values() for nid in ids}
    for n in graph_data["nodes"]:
        if n["id"] not in clustered_ids:
            if n.get("external"):
                dot.node(
                    n["id"],
                    label=n["label"],
                    tooltip=_dot_tooltip(n),
                    fillcolor="#f0f0f0",
                    style="rounded,filled,dashed",
                    fontcolor="#888888",
                    color="#aaaaaa",
                )
            else:
                dot.node(n["id"], label=n["label"], tooltip=_dot_tooltip(n))

    if external_modules:
        ext_fill, ext_stroke, ext_color = _EXT_OUTER_STYLE
        with dot.subgraph(name="cluster___ext__") as ext_sub:
            ext_sub.attr(
                label="external libraries",
                style="rounded,filled",
                fillcolor=ext_fill,
                color=ext_stroke,
                fontcolor=ext_color,
                fontsize="10",
                penwidth="2",
            )
            ext_tree, ext_top = _build_module_tree(list(external_modules.keys()))
            _add_dot_cluster_tree(
                ext_sub, ext_top, ext_tree, external_modules, node_by_id, 0, is_external=True
            )

    # Add edges
    for edge in graph_data["edges"]:
        label = str(edge["count"]) if edge["count"] > 1 else ""
        dot.edge(edge["source"], edge["target"], label=label)

    return dot
