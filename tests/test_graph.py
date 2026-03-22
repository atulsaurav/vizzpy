"""Tests for graph.py: build_graph, aggregate_to_modules, helpers."""
import pytest
from pathlib import Path

from vizzpy.graph import aggregate_to_modules, build_graph, _fallback_label, _fallback_module


# ── _fallback_label / _fallback_module ───────────────────────────────────────

def test_fallback_label_simple():
    assert _fallback_label("pkg.utils.helper") == "helper"

def test_fallback_label_no_dot():
    assert _fallback_label("main") == "main"

def test_fallback_module_with_dot():
    assert _fallback_module("pkg.utils.helper") == "pkg.utils"

def test_fallback_module_no_dot():
    assert _fallback_module("main") == "main"


# ── build_graph ───────────────────────────────────────────────────────────────

def test_build_graph_basic(tmp_path):
    (tmp_path / "foo.py").write_text("def a(): pass\ndef b():\n    a()")
    g = build_graph(tmp_path)
    node_ids = {n["id"] for n in g["nodes"]}
    assert "foo.a" in node_ids
    assert "foo.b" in node_ids
    assert any(e["source"] == "foo.b" and e["target"] == "foo.a" for e in g["edges"])


def test_build_graph_edge_count(tmp_path):
    # Two calls to the same callee → count == 2
    (tmp_path / "m.py").write_text("def a(): pass\ndef b():\n    a()\n    a()")
    g = build_graph(tmp_path)
    edge = next(e for e in g["edges"] if e["source"] == "m.b" and e["target"] == "m.a")
    assert edge["count"] == 2


def test_build_graph_modules_grouping(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def helper(): pass\ndef run():\n    helper()")
    g = build_graph(tmp_path)
    assert "pkg.utils" in g["modules"]
    assert "pkg.utils.helper" in g["modules"]["pkg.utils"]


def test_build_graph_node_label_and_module(tmp_path):
    (tmp_path / "svc.py").write_text("def process(): pass\ndef main():\n    process()")
    g = build_graph(tmp_path)
    node = next(n for n in g["nodes"] if n["id"] == "svc.process")
    assert node["label"] == "process"
    assert node["module"] == "svc"


def test_build_graph_cross_module(tmp_path):
    (tmp_path / "a.py").write_text("def helper(): pass")
    (tmp_path / "b.py").write_text("from a import helper\ndef caller():\n    helper()")
    g = build_graph(tmp_path)
    assert any(e["source"] == "b.caller" and e["target"] == "a.helper" for e in g["edges"])


def test_build_graph_fallback_node_for_unknown_callee(tmp_path):
    # Callee comes from an external package — still shows up as a node if referenced
    # within-project edges only (external callee won't appear unless it's in all_names)
    # This test just verifies build_graph doesn't crash on an empty project.
    g = build_graph(tmp_path)
    assert g["nodes"] == []
    assert g["edges"] == []
    assert g["modules"] == {}


def test_build_graph_external_flag_false_for_project_nodes(tmp_path):
    (tmp_path / "m.py").write_text("def a(): pass\ndef b():\n    a()")
    g = build_graph(tmp_path)
    for node in g["nodes"]:
        assert node["external"] is False


def test_build_graph_external_flag_true_for_library_call(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef run():\n    os.getcwd()")
    g = build_graph(tmp_path)
    ext_node = next(n for n in g["nodes"] if n["id"] == "os.getcwd")
    assert ext_node["external"] is True
    project_node = next(n for n in g["nodes"] if n["id"] == "m.run")
    assert project_node["external"] is False


def test_build_graph_main_sentinel_not_external(tmp_path):
    # __main__ synthetic callers are project-internal, not external
    (tmp_path / "m.py").write_text("def setup(): pass\nsetup()")
    g = build_graph(tmp_path)
    main_node = next((n for n in g["nodes"] if n["id"] == "m.__main__"), None)
    if main_node:
        assert main_node["external"] is False


# ── aggregate_to_modules ──────────────────────────────────────────────────────

def test_aggregate_to_modules_nodes_one_per_module(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass")
    (tmp_path / "b.py").write_text("from a import f\ndef g():\n    f()")
    g = aggregate_to_modules(build_graph(tmp_path))
    module_ids = {n["id"] for n in g["nodes"]}
    assert module_ids == {"a", "b"}


def test_aggregate_to_modules_cross_module_edge(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass")
    (tmp_path / "b.py").write_text("from a import f\ndef g():\n    f()")
    g = aggregate_to_modules(build_graph(tmp_path))
    assert any(e["source"] == "b" and e["target"] == "a" for e in g["edges"])


def test_aggregate_to_modules_drops_intra_module_edges(tmp_path):
    (tmp_path / "m.py").write_text("def a(): pass\ndef b():\n    a()")
    g = aggregate_to_modules(build_graph(tmp_path))
    # Only one module, so no cross-module edges
    assert g["edges"] == []


def test_aggregate_to_modules_sums_counts(tmp_path):
    # Three distinct function calls from b → a should sum to 3 at module level
    (tmp_path / "a.py").write_text("def x(): pass\ndef y(): pass\ndef z(): pass")
    (tmp_path / "b.py").write_text(
        "from a import x, y, z\ndef run():\n    x()\n    y()\n    z()"
    )
    g = aggregate_to_modules(build_graph(tmp_path))
    edge = next(e for e in g["edges"] if e["source"] == "b" and e["target"] == "a")
    assert edge["count"] == 3


def test_aggregate_to_modules_external_module_flag(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef run():\n    os.getcwd()")
    g = aggregate_to_modules(build_graph(tmp_path))
    os_node = next(n for n in g["nodes"] if n["id"] == "os")
    m_node  = next(n for n in g["nodes"] if n["id"] == "m")
    assert os_node["external"] is True
    assert m_node["external"] is False


def test_aggregate_to_modules_node_label_equals_id(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "utils.py").write_text("def h(): pass\ndef r():\n    h()")
    g = aggregate_to_modules(build_graph(tmp_path))
    node = g["nodes"][0]
    assert node["label"] == node["id"]


def test_aggregate_to_modules_modules_dict_keys(tmp_path):
    (tmp_path / "a.py").write_text("def f(): pass")
    (tmp_path / "b.py").write_text("from a import f\ndef g():\n    f()")
    g = aggregate_to_modules(build_graph(tmp_path))
    assert set(g["modules"].keys()) == {"a", "b"}
    assert g["modules"]["a"] == ["a"]


def test_aggregate_to_modules_mixed_module_not_external(tmp_path):
    # A module with both a project function and an external call
    # should NOT be marked external
    (tmp_path / "m.py").write_text(
        "import os\ndef run():\n    os.getcwd()\ndef local(): pass\ndef caller():\n    local()"
    )
    g = aggregate_to_modules(build_graph(tmp_path))
    m_node = next(n for n in g["nodes"] if n["id"] == "m")
    assert m_node["external"] is False


def test_build_graph_node_docstring(tmp_path):
    (tmp_path / "m.py").write_text(
        'def a():\n    """Does something."""\n    pass\ndef b():\n    a()'
    )
    g = build_graph(tmp_path)
    node = next(n for n in g["nodes"] if n["id"] == "m.a")
    assert node["docstring"] == "Does something."
