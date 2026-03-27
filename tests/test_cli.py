"""Tests for CLI helpers — focused on _report_missing_docstrings."""
import pytest
from pathlib import Path

from vizzpy.cli import _report_missing_docstrings


def test_report_all_have_docstrings(tmp_path, capsys):
    (tmp_path / "m.py").write_text(
        'def a():\n    """Does a."""\n    pass\n'
        'def b():\n    """Does b."""\n    pass\n'
    )
    _report_missing_docstrings(tmp_path)
    out = capsys.readouterr().out
    assert "All functions have docstrings" in out


def test_report_lists_missing(tmp_path, capsys):
    (tmp_path / "m.py").write_text(
        'def has_doc():\n    """Yes."""\n    pass\n'
        'def no_doc():\n    pass\n'
    )
    _report_missing_docstrings(tmp_path)
    out = capsys.readouterr().out
    assert "no_doc" in out
    assert "has_doc" not in out


def test_report_grouped_by_module(tmp_path, capsys):
    (tmp_path / "alpha.py").write_text("def foo(): pass\n")
    (tmp_path / "beta.py").write_text("def bar(): pass\n")
    _report_missing_docstrings(tmp_path)
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out
    assert "foo" in out
    assert "bar" in out


def test_report_total_count(tmp_path, capsys):
    (tmp_path / "m.py").write_text("def a(): pass\ndef b(): pass\ndef c(): pass\n")
    _report_missing_docstrings(tmp_path)
    out = capsys.readouterr().out
    assert "3 total" in out


def test_report_empty_project(tmp_path, capsys):
    # No .py files → no functions → treated as all-documented
    _report_missing_docstrings(tmp_path)
    out = capsys.readouterr().out
    assert "All functions have docstrings" in out


# ── --fail-on-missing-docs ────────────────────────────────────────────────────

def test_fail_flag_exits_nonzero_when_missing(tmp_path):
    (tmp_path / "m.py").write_text("def no_doc(): pass\n")
    with pytest.raises(SystemExit) as exc_info:
        _report_missing_docstrings(tmp_path, fail=True)
    assert exc_info.value.code == 1


def test_fail_flag_no_exit_when_all_documented(tmp_path):
    (tmp_path / "m.py").write_text('def ok():\n    """Documented."""\n    pass\n')
    # Should not raise even with fail=True
    _report_missing_docstrings(tmp_path, fail=True)


def test_no_fail_flag_does_not_exit_when_missing(tmp_path):
    (tmp_path / "m.py").write_text("def no_doc(): pass\n")
    # Default fail=False — must not raise
    _report_missing_docstrings(tmp_path, fail=False)
