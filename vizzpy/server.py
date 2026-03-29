"""
FastAPI application.

Routes:
  GET  /               → serves VizzX interactive frontend (via vizzx package)
  GET  /api/preloaded  → returns pre-analyzed graph JSON (if a project was supplied at startup)
  POST /api/analyze    → accepts a .zip, .tar.gz, .egg, or .whl upload, returns graph JSON
  GET  /static/*       → static assets (JS, CSS, vendor libs)
"""
from __future__ import annotations
import logging
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from vizzx import create_ui_router, mount_static

from .graph import build_graph

logger = logging.getLogger(__name__)

app = FastAPI(title="VizPy")

_preloaded_graph: Optional[dict] = None


def preload_project(path: Path) -> None:
    """Pre-analyze a local project directory and cache the graph for /api/preloaded."""
    global _preloaded_graph
    _preloaded_graph = build_graph(path)

# Mount the VizzX interactive frontend
app.include_router(create_ui_router(
    title="VizzPy",
    upload_accept=".zip,.tar.gz,.tgz,.egg,.whl",
    upload_label="Upload a Python project (.zip, .tar.gz, .egg, or .whl) to visualize its call tree",
))
mount_static(app)


@app.get("/api/preloaded")
async def preloaded():
    """Return the pre-analyzed graph if a project path was supplied at startup, else null."""
    return JSONResponse(content=_preloaded_graph)


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """
    Accept a .zip, .tar.gz, .egg, or .whl archive of a Python project and return the call graph as JSON.
    .egg and .whl files are zip-format archives and are extracted the same way as .zip.
    """
    fname = file.filename or ""
    is_zip    = fname.endswith(".zip") or fname.endswith(".egg") or fname.endswith(".whl")
    is_targz  = fname.endswith(".tar.gz") or fname.endswith(".tgz")
    if not (is_zip or is_targz):
        raise HTTPException(status_code=400, detail="Upload must be a .zip, .tar.gz, .egg, or .whl file")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        upload_path = tmp_path / ("upload.zip" if is_zip else "upload.tar.gz")

        content = await file.read()
        upload_path.write_bytes(content)

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        if is_zip:
            try:
                with zipfile.ZipFile(upload_path) as zf:
                    zf.extractall(project_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file")
        else:
            try:
                with tarfile.open(upload_path, "r:gz") as tf:
                    tf.extractall(project_dir)
            except tarfile.TarError:
                raise HTTPException(status_code=400, detail="Invalid tar.gz file")

        project_root = _find_project_root(project_dir)

        try:
            graph = build_graph(project_root)
        except Exception as exc:
            logger.exception("Error analyzing project")
            raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(content=graph)


def _find_project_root(extracted: Path) -> Path:
    """
    If the zip contained a single top-level directory, use that as the root
    so that module names are computed from the actual package, not the zip wrapper.
    """
    children = [c for c in extracted.iterdir() if not c.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extracted
