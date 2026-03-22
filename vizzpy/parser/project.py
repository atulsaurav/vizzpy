"""
Multi-file project analysis: discovers all .py files, builds scopes,
resolves cross-module imports, and emits the full edge list.
"""
from __future__ import annotations
import ast
import logging
from pathlib import Path

from .scope import FuncSpan
from .walker import build_scope, build_import_map, CallVisitor

logger = logging.getLogger(__name__)


def _is_test_file(f: Path) -> bool:
    """Return True for files that are part of a test suite."""
    parts = f.parts
    return (
        f.name.startswith("test_")
        or f.name.endswith("_test.py")
        or any(p in ("tests", "test") for p in parts)
    )


def get_module_name(file_path: Path, root: Path) -> str:
    """Convert an absolute file path to its dotted module name relative to *root*."""
    rel = file_path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]  # strip .py
    return ".".join(parts) if parts else "__root__"


def analyze_project(root: Path) -> tuple[list[tuple[str, str]], dict[str, FuncSpan], set[str]]:
    """
    Parse every .py file under *root* and return:
      edges        — list of (caller_qname, callee_qname)
      node_info    — dict mapping qname -> FuncSpan (for label/docstring lookup)
      all_names    — set of all project-defined qualified function names
    """
    py_files = sorted(f for f in root.rglob("*.py") if not _is_test_file(f))

    # Pass 1: parse every file and build per-module scopes
    modules: dict[str, tuple[ast.AST, object]] = {}  # module_name -> (tree, scope)
    for f in py_files:
        module_name = get_module_name(f, root)
        try:
            source = f.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(f))
            scope = build_scope(module_name, tree)
            modules[module_name] = (tree, scope)
        except SyntaxError as exc:
            logger.warning("Skipping %s — syntax error: %s", f, exc)

    # Collect the project-wide set of known qualified names
    all_names: set[str] = set()
    node_info: dict[str, FuncSpan] = {}
    for _module_name, (_tree, scope) in modules.items():
        all_names |= scope.names
        for span in scope.spans:
            node_info[span.qualified_name] = span

    # Build suffix index: maps every trailing dotted suffix of each qname to that qname.
    # Used to resolve imports whose top-level package name differs from the folder name
    # (e.g. code imports "xml_framework.x" but the folder is "xml_framework_updated").
    # Values of None indicate an ambiguous suffix (two qnames share the same suffix).
    suffix_index: dict[str, str | None] = {}
    for qname in all_names:
        parts = qname.split(".")
        for i in range(1, len(parts)):        # skip i=0 (full name already in all_names)
            suffix = ".".join(parts[i:])
            if suffix in suffix_index:
                suffix_index[suffix] = None   # ambiguous — mark, don't use
            else:
                suffix_index[suffix] = qname

    # Pass 2: extract edges from each module
    edges: list[tuple[str, str]] = []
    for module_name, (tree, _scope) in modules.items():
        import_map = build_import_map(tree, module_name)
        visitor = CallVisitor(module_name, import_map, all_names, suffix_index)
        visitor.visit(tree)
        edges.extend(visitor.edges)

    return edges, node_info, all_names
