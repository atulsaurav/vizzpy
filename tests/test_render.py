"""Tests for render.py: render_mermaid and _to_mermaid / _mermaid_id."""
import re
from pathlib import Path

import pytest

from vizzpy.render import render_mermaid, _mermaid_id, _to_mermaid


# ── _mermaid_id ───────────────────────────────────────────────────────────────

def test_mermaid_id_replaces_dots():
    assert _mermaid_id("pkg.mod.func") == "pkg__mod__func"

def test_mermaid_id_replaces_hyphens():
    assert _mermaid_id("my-pkg.func") == "my_pkg__func"

def test_mermaid_id_simple():
    assert _mermaid_id("main") == "main"


# ── _to_mermaid ───────────────────────────────────────────────────────────────

def _graph(tmp_path, src: str) -> dict:
    from vizzpy.graph import build_graph
    (tmp_path / "m.py").write_text(src)
    return build_graph(tmp_path)


def test_to_mermaid_contains_header(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    assert md.startswith("```mermaid\n")
    assert "flowchart LR" in md
    assert md.strip().endswith("```")


def test_to_mermaid_contains_subgraph(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    assert 'subgraph m["m"]' in md


def test_to_mermaid_nodes_present(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    assert 'm__a["a"]' in md
    assert 'm__b["b"]' in md


def test_to_mermaid_edge_present(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    assert "m__b --> m__a" in md


def test_to_mermaid_edge_count_label(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()\n    a()")
    md = _to_mermaid(g)
    assert '-->|"2x"| m__a' in md


def test_to_mermaid_single_call_no_count_label(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    # Single-call edge must not carry a label
    assert re.search(r"m__b -->\s*m__a", md)
    assert '-->|' not in md


def test_to_mermaid_cross_module(tmp_path):
    (tmp_path / "a.py").write_text("def helper(): pass")
    (tmp_path / "b.py").write_text("from a import helper\ndef caller():\n    helper()")
    from vizzpy.graph import build_graph
    g = build_graph(tmp_path)
    md = _to_mermaid(g)
    assert 'subgraph a["a"]' in md
    assert 'subgraph b["b"]' in md
    assert "b__caller --> a__helper" in md


def test_to_mermaid_empty_project(tmp_path):
    from vizzpy.graph import build_graph
    g = build_graph(tmp_path)
    md = _to_mermaid(g)
    assert "```mermaid" in md
    assert "flowchart LR" in md


# ── render_mermaid ────────────────────────────────────────────────────────────

def test_render_mermaid_writes_file(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "foo.py").write_text("def a(): pass\ndef b():\n    a()")
    out = tmp_path / "graph.md"
    render_mermaid(proj, out)
    assert out.exists()
    content = out.read_text()
    assert "```mermaid" in content
    assert "foo__a" in content


def test_to_mermaid_external_nodes_get_classDef(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef run():\n    os.getcwd()")
    from vizzpy.graph import build_graph
    g = build_graph(tmp_path)
    md = _to_mermaid(g)
    assert "classDef external" in md
    assert "os__getcwd" in md  # node present with safe id
    assert "class " in md      # class assignment line present


def test_to_mermaid_external_nodes_in_outer_subgraph(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef run():\n    os.getcwd()")
    from vizzpy.graph import build_graph
    g = build_graph(tmp_path)
    md = _to_mermaid(g)
    assert '__ext__["external libraries"]' in md
    assert 'subgraph os["os"]' in md


def test_to_mermaid_no_external_section_when_none(tmp_path):
    g = _graph(tmp_path, "def a(): pass\ndef b():\n    a()")
    md = _to_mermaid(g)
    assert "classDef external" not in md
    assert "__ext__" not in md


def test_render_mermaid_default_extension(tmp_path):
    """render_mermaid accepts any Path — caller chooses the name."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "x.py").write_text("def f(): pass\ndef g():\n    f()")
    out = tmp_path / "call_tree.md"
    render_mermaid(proj, out)
    assert out.read_text().startswith("```mermaid\n")


# ── module-level rendering ─────────────────────────────────────────────────────

def test_to_mermaid_module_level_flat_nodes(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass")
    (tmp_path / "b.py").write_text("from a import f\ndef g():\n    f()")
    from vizzpy.graph import build_graph, aggregate_to_modules
    g = aggregate_to_modules(build_graph(tmp_path))
    md = _to_mermaid(g, level="module")
    # Module nodes appear as flat items — no subgraph wrappers
    assert 'a["a"]' in md
    assert 'b["b"]' in md
    assert "subgraph" not in md


def test_to_mermaid_module_level_edge(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass")
    (tmp_path / "b.py").write_text("from a import f\ndef g():\n    f()")
    from vizzpy.graph import build_graph, aggregate_to_modules
    g = aggregate_to_modules(build_graph(tmp_path))
    md = _to_mermaid(g, level="module")
    assert "b --> a" in md


def test_to_mermaid_module_level_external_styled(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef run():\n    os.getcwd()")
    from vizzpy.graph import build_graph, aggregate_to_modules
    g = aggregate_to_modules(build_graph(tmp_path))
    md = _to_mermaid(g, level="module")
    assert "classDef external" in md
    assert "class os external" in md


def test_render_mermaid_module_level_writes_file(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("def f(): pass")
    (proj / "b.py").write_text("from a import f\ndef g():\n    f()")
    out = tmp_path / "modules.md"
    render_mermaid(proj, out, level="module")
    content = out.read_text()
    assert "```mermaid" in content
    assert 'a["a"]' in content
    assert "subgraph" not in content
