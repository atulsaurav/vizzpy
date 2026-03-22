"""Tests for the FastAPI server endpoints."""
import io
import tarfile
import zipfile
import pytest

pytest.importorskip(
    "fastapi",
    reason="serve dependencies not installed — pip install 'vizzpy[serve]'",
)

from httpx import AsyncClient, ASGITransport  # noqa: E402

from vizzpy.server import app, _find_project_root  # noqa: E402
from pathlib import Path  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_zip(files: dict[str, str]) -> bytes:
    """Build an in-memory zip from {relative_path: content} pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_targz(files: dict[str, str]) -> bytes:
    """Build an in-memory .tar.gz from {relative_path: content} pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── GET / ─────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_index_returns_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── POST /api/analyze (zip) ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_analyze_valid_zip(client):
    zip_bytes = _make_zip({
        "proj/foo.py": "def a(): pass\ndef b():\n    a()",
    })
    r = await client.post(
        "/api/analyze",
        files={"file": ("proj.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data and "modules" in data
    node_ids = {n["id"] for n in data["nodes"]}
    assert "foo.a" in node_ids


@pytest.mark.anyio
async def test_analyze_rejects_non_archive(client):
    r = await client.post(
        "/api/analyze",
        files={"file": ("code.py", b"def f(): pass", "text/plain")},
    )
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "zip" in detail or "tar" in detail


@pytest.mark.anyio
async def test_analyze_rejects_bad_zip(client):
    r = await client.post(
        "/api/analyze",
        files={"file": ("bad.zip", b"not a zip", "application/zip")},
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_analyze_zip_single_top_level_dir(client):
    # Zip wraps everything in a top-level folder — _find_project_root should unwrap it
    zip_bytes = _make_zip({
        "myproject/utils.py": "def helper(): pass\ndef run():\n    helper()",
    })
    r = await client.post(
        "/api/analyze",
        files={"file": ("myproject.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200
    node_ids = {n["id"] for n in r.json()["nodes"]}
    assert "utils.helper" in node_ids  # module name from inside myproject/, not myproject.utils


# ── POST /api/analyze (tar.gz) ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_analyze_valid_targz(client):
    tgz_bytes = _make_targz({
        "proj/foo.py": "def a(): pass\ndef b():\n    a()",
    })
    r = await client.post(
        "/api/analyze",
        files={"file": ("proj.tar.gz", tgz_bytes, "application/gzip")},
    )
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data and "modules" in data
    node_ids = {n["id"] for n in data["nodes"]}
    assert "foo.a" in node_ids


@pytest.mark.anyio
async def test_analyze_valid_tgz_extension(client):
    tgz_bytes = _make_targz({
        "bar.py": "def x(): pass\ndef y():\n    x()",
    })
    r = await client.post(
        "/api/analyze",
        files={"file": ("proj.tgz", tgz_bytes, "application/gzip")},
    )
    assert r.status_code == 200
    node_ids = {n["id"] for n in r.json()["nodes"]}
    assert "bar.x" in node_ids


@pytest.mark.anyio
async def test_analyze_rejects_bad_targz(client):
    r = await client.post(
        "/api/analyze",
        files={"file": ("bad.tar.gz", b"not a tar", "application/gzip")},
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_analyze_targz_single_top_level_dir(client):
    tgz_bytes = _make_targz({
        "myproject/utils.py": "def helper(): pass\ndef run():\n    helper()",
    })
    r = await client.post(
        "/api/analyze",
        files={"file": ("myproject.tar.gz", tgz_bytes, "application/gzip")},
    )
    assert r.status_code == 200
    node_ids = {n["id"] for n in r.json()["nodes"]}
    assert "utils.helper" in node_ids


# ── _find_project_root ────────────────────────────────────────────────────────

def test_find_project_root_unwraps_single_dir(tmp_path):
    inner = tmp_path / "myproject"
    inner.mkdir()
    (inner / "foo.py").touch()
    assert _find_project_root(tmp_path) == inner


def test_find_project_root_keeps_flat(tmp_path):
    (tmp_path / "foo.py").touch()
    (tmp_path / "bar.py").touch()
    assert _find_project_root(tmp_path) == tmp_path


def test_find_project_root_ignores_dot_files(tmp_path):
    (tmp_path / ".DS_Store").touch()
    inner = tmp_path / "src"
    inner.mkdir()
    assert _find_project_root(tmp_path) == inner
