"""
vizzpy CLI entry point.

Usage:
  vizzpy --serve   [--host HOST] [--port PORT] [--project PROJECT_PATH]
  vizzpy --headless --project PROJECT_PATH [--format svg|mermaid] [--level function|module|both] [--output OUTPUT] [--fail-on-missing-docs]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="vizzpy",
        description="Python call tree visualizer",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true", help="Start the web server")
    mode.add_argument("--headless", action="store_true", help="Render call graph without a web server (requires --project)")

    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (serve mode)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (serve mode)")
    parser.add_argument("--project", default=None, metavar="PROJECT_PATH",
                        help="Pre-load a local project directory in serve mode (skips browser upload)")
    parser.add_argument("--output", default=None, help="Output file path (headless mode); defaults to call_tree.svg or call_tree.md")
    parser.add_argument("--format", choices=["svg", "mermaid"], default="mermaid", help="Output format for headless mode (default: mermaid)")
    parser.add_argument(
        "--level",
        choices=["function", "module", "both"],
        default="function",
        help="Granularity of the call graph: function (default), module, or both",
    )
    parser.add_argument(
        "--layout",
        choices=["elk", "dagre"],
        default="dagre",
        help="Mermaid layout engine: elk (better for highly nested subgraphs) or dagre (default)",
    )
    parser.add_argument(
        "--fail-on-missing-docs",
        action="store_true",
        help="Exit with code 1 if any functions are missing docstrings (useful as a CI quality gate)",
    )

    args = parser.parse_args()

    if args.serve:
        _run_server(args.host, args.port, Path(args.project) if args.project else None)
    else:
        if not args.project:
            parser.error("--headless requires --project PROJECT_PATH")
        project_path = Path(args.project)
        ext = ".md" if args.format == "mermaid" else ".svg"
        if args.level == "both":
            if args.output:
                base = Path(args.output)
                func_out = base.parent / (base.stem + "_functions" + base.suffix)
                mod_out  = base.parent / (base.stem + "_modules"   + base.suffix)
            else:
                func_out = Path(f"{project_path.name}_call_tree_functions{ext}")
                mod_out  = Path(f"{project_path.name}_call_tree_modules{ext}")
            _run_headless(project_path, func_out, args.format, "function", args.layout)
            _run_headless(project_path, mod_out,  args.format, "module",   args.layout)
        else:
            output_path = Path(args.output) if args.output else Path(f"{project_path.name}_call_tree_functions{ext}")
            _run_headless(project_path, output_path, args.format, args.level, args.layout)
        _report_missing_docstrings(project_path, fail=args.fail_on_missing_docs)


def _report_missing_docstrings(project_path: Path, fail: bool = False) -> None:
    """Print a grouped summary of functions/methods that have no docstring.

    If *fail* is True, exits with code 1 when any are found (CI quality gate).
    """
    from vizzpy.parser.project import analyze_project
    from collections import defaultdict

    _, node_info, _ = analyze_project(project_path)

    missing: dict[str, list[str]] = defaultdict(list)
    for span in node_info.values():
        if not (span.docstring or "").strip():
            missing[span.module_name].append(span.display_name)

    if not missing:
        print("\nAll functions have docstrings.")
        return

    total = sum(len(v) for v in missing.values())
    print(f"\nFunctions missing docstrings ({total} total):")
    for module in sorted(missing):
        print(f"  {module}")
        for name in sorted(missing[module]):
            print(f"    - {name}")

    if fail:
        sys.exit(1)


def _run_server(host: str, port: int, project_path: "Path | None" = None) -> None:
    try:
        import uvicorn  # noqa: F401
        from vizzpy.server import app, preload_project
    except ImportError as exc:
        sys.exit(
            f"Server dependencies missing ({exc}).\n"
            "Install with: pip install 'vizzpy[serve]'"
        )
    if project_path is not None:
        if not project_path.exists():
            sys.exit(f"Project path does not exist: {project_path}")
        print(f"Pre-loading project: {project_path} ...")
        preload_project(project_path)
        print("Done. Starting server...")
    uvicorn.run(app, host=host, port=port)


def _run_headless(project_path: Path, output_path: Path, fmt: str, level: str, layout: str = "dagre") -> None:
    if not project_path.exists():
        sys.exit(f"Project path does not exist: {project_path}")

    print(f"Analyzing {project_path} ...")
    if fmt == "mermaid":
        from vizzpy.render import render_mermaid
        render_mermaid(project_path, output_path, level=level, layout=layout)
        print(f"Mermaid markdown written to {output_path}")
    else:
        from vizzpy.render import render_svg
        try:
            if layout == "elk":
                layout = "ortho"
            elif layout == "dagre":
                layout = "spline"
            render_svg(project_path, output_path, level=level, layout=layout)
        except ImportError as exc:
            sys.exit(
                f"SVG rendering requires the graphviz package ({exc}).\n"
                "Install with: pip install 'vizzpy[svg]'\n"
                "Also ensure the Graphviz system package is installed "
                "(brew install graphviz / apt install graphviz)."
            )
        print(f"SVG written to {output_path}")


if __name__ == "__main__":
    cli()
