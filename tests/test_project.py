"""Tests for project-level analysis: module naming, test exclusion, edge collection."""
import pytest
from pathlib import Path

from vizzpy.parser.project import _is_test_file, get_module_name, analyze_project


# ── _is_test_file ─────────────────────────────────────────────────────────────

def test_is_test_by_prefix():
    assert _is_test_file(Path("/proj/test_foo.py"))

def test_is_test_by_suffix():
    assert _is_test_file(Path("/proj/foo_test.py"))

def test_is_test_by_dir():
    assert _is_test_file(Path("/proj/tests/helper.py"))

def test_is_test_nested_dir():
    assert _is_test_file(Path("/proj/src/test/utils.py"))

def test_not_test_file():
    assert not _is_test_file(Path("/proj/src/utils.py"))

def test_not_test_file_with_test_in_name():
    # "testament.py" should not be excluded
    assert not _is_test_file(Path("/proj/testament.py"))


# ── get_module_name ───────────────────────────────────────────────────────────

def test_module_name_simple(tmp_path):
    f = tmp_path / "utils.py"
    f.touch()
    assert get_module_name(f, tmp_path) == "utils"

def test_module_name_nested(tmp_path):
    (tmp_path / "services").mkdir()
    f = tmp_path / "services" / "order.py"
    f.touch()
    assert get_module_name(f, tmp_path) == "services.order"

def test_module_name_init(tmp_path):
    (tmp_path / "services").mkdir()
    f = tmp_path / "services" / "__init__.py"
    f.touch()
    assert get_module_name(f, tmp_path) == "services"


# ── analyze_project ───────────────────────────────────────────────────────────

def test_basic_edges(tmp_path):
    (tmp_path / "foo.py").write_text("def a(): pass\ndef b():\n    a()")
    edges, node_info, all_names = analyze_project(tmp_path)
    assert ("foo.b", "foo.a") in edges
    assert "foo.a" in node_info
    assert "foo.b" in node_info


def test_excludes_test_files(tmp_path):
    (tmp_path / "foo.py").write_text("def a(): pass")
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_foo.py").write_text(
        "from foo import a\ndef test_it():\n    a()"
    )
    edges, node_info, _ = analyze_project(tmp_path)
    # test functions must not appear as nodes or callers
    assert not any("test_it" in qn for qn in node_info)
    assert not any("test_it" in e[0] for e in edges)


def test_cross_module_edges(tmp_path):
    (tmp_path / "a.py").write_text("def helper(): pass")
    (tmp_path / "b.py").write_text(
        "from a import helper\ndef caller():\n    helper()"
    )
    edges, _, _names = analyze_project(tmp_path)
    assert ("b.caller", "a.helper") in edges


def test_syntax_error_file_skipped(tmp_path):
    (tmp_path / "good.py").write_text("def ok(): pass")
    (tmp_path / "bad.py").write_text("def broken(: pass")  # syntax error
    # Should not raise; the bad file is silently skipped
    edges, node_info, _ = analyze_project(tmp_path)
    assert "good.ok" in node_info


def test_no_duplicate_edges(tmp_path):
    # Two calls from the same caller to the same callee → one edge with count 2
    (tmp_path / "m.py").write_text(
        "def a(): pass\ndef b():\n    a()\n    a()"
    )
    edges, _, _names = analyze_project(tmp_path)
    matching = [e for e in edges if e == ("m.b", "m.a")]
    assert len(matching) == 2  # raw edges — duplicates expected; build_graph deduplicates


def test_all_names_returned(tmp_path):
    (tmp_path / "m.py").write_text("def a(): pass\ndef b():\n    a()")
    _, _, all_names = analyze_project(tmp_path)
    assert {"m.a", "m.b"} <= all_names


def test_external_call_edge_included(tmp_path):
    # Calls to imported library functions are now included as edges
    (tmp_path / "m.py").write_text(
        "import os\ndef run():\n    os.getcwd()"
    )
    edges, _, _ = analyze_project(tmp_path)
    assert any(e[0] == "m.run" and "getcwd" in e[1] for e in edges)


# ── .ipynb notebook support ───────────────────────────────────────────────────

def test_module_name_notebook(tmp_path):
    f = tmp_path / "analysis.ipynb"
    f.touch()
    assert get_module_name(f, tmp_path) == "analysis"


def test_module_name_nested_notebook(tmp_path):
    (tmp_path / "notebooks").mkdir()
    f = tmp_path / "notebooks" / "explore.ipynb"
    f.touch()
    assert get_module_name(f, tmp_path) == "notebooks.explore"


def _make_notebook(tmp_path, name, cells):
    """Write a minimal .ipynb file with the given list of code-cell source strings."""
    import json
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "source": src, "metadata": {}, "outputs": [], "execution_count": None}
            for src in cells
        ],
    }
    p = tmp_path / name
    p.write_text(json.dumps(nb))
    return p


def test_notebook_functions_discovered(tmp_path):
    _make_notebook(tmp_path, "nb.ipynb", ["def helper():\n    pass\n", "def main():\n    helper()\n"])
    edges, node_info, all_names = analyze_project(tmp_path)
    assert "nb.helper" in all_names
    assert "nb.main" in all_names
    assert ("nb.main", "nb.helper") in edges


def test_notebook_magic_lines_stripped(tmp_path):
    _make_notebook(tmp_path, "nb.ipynb", [
        "%matplotlib inline\n!pip install pandas\ndef compute():\n    pass\n"
    ])
    # Should not raise SyntaxError; function must be discovered
    _, node_info, _ = analyze_project(tmp_path)
    assert "nb.compute" in node_info


def test_notebook_markdown_cells_ignored(tmp_path):
    import json
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "markdown", "source": "# Title\nsome text", "metadata": {}},
            {"cell_type": "code", "source": "def real(): pass", "metadata": {}, "outputs": [], "execution_count": None},
        ],
    }
    (tmp_path / "nb.ipynb").write_text(json.dumps(nb))
    _, node_info, _ = analyze_project(tmp_path)
    assert "nb.real" in node_info


def test_notebook_checkpoints_excluded(tmp_path):
    cp_dir = tmp_path / ".ipynb_checkpoints"
    cp_dir.mkdir()
    _make_notebook(cp_dir, "nb-checkpoint.ipynb", ["def stale(): pass"])
    _make_notebook(tmp_path, "nb.ipynb", ["def live(): pass"])
    _, node_info, _ = analyze_project(tmp_path)
    assert "nb.live" in node_info
    assert not any("stale" in qn for qn in node_info)


def test_notebook_cross_module_edges(tmp_path):
    (tmp_path / "utils.py").write_text("def helper(): pass")
    _make_notebook(tmp_path, "nb.ipynb", [
        "from utils import helper\ndef run():\n    helper()\n"
    ])
    edges, _, _ = analyze_project(tmp_path)
    assert ("nb.run", "utils.helper") in edges
