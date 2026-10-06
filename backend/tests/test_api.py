"""API-level checks (needs fastapi + httpx: pip install -r requirements-dev.txt)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymupdf")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ENGINE_DB", str(tmp_path / "api.sqlite3"))
    from codex_engine import app as app_module

    app_module._schema_ready = False
    return TestClient(app_module.app)


def test_health_identifies_codex_engine(client):
    body = client.get("/api/health").json()
    assert body["app"] == "Codex Engine"


def test_post_without_client_header_is_rejected(client):
    r = client.post("/api/ingest", json={"path": "C:/nope.pdf"})
    assert r.status_code == 403


def test_post_with_client_header_is_allowed(client):
    r = client.post("/api/ingest", json={"path": "C:/nope.pdf"}, headers={"X-Codex-Engine-Client": "1"})
    assert r.status_code == 400  # reaches the handler; file doesn't exist


def test_search_bad_syntax_is_not_a_500(client):
    r = client.get("/api/search", params={"query": "half-orc"})
    assert r.status_code == 200


def test_foreign_host_header_rejected(client):
    r = client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
