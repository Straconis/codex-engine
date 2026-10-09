"""API-level checks (needs fastapi + httpx: pip install -r requirements-dev.txt)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("pymupdf")

from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ENGINE_DB", str(tmp_path / "api.sqlite3"))
    from codex_engine import app as app_module

    app_module._schema_ready = False
    return TestClient(app_module.app, base_url="http://127.0.0.1")


def test_health_identifies_codex_engine(client):
    body = client.get("/api/health").json()
    assert body["app"] == "Codex Engine"


def test_post_without_client_header_is_rejected(client):
    r = client.post("/api/ingest", json={"path": "/nope.pdf"})
    assert r.status_code == 403


def test_post_with_client_header_is_allowed(client, tmp_path):
    r = client.post("/api/ingest", json={"path": str(tmp_path / "nope.pdf")}, headers={"X-Codex-Engine-Client": "1"})
    assert r.status_code == 404  # reaches the handler; the file doesn't exist


def test_search_bad_syntax_is_not_a_500(client):
    r = client.get("/api/search", params={"query": "half-orc"})
    assert r.status_code == 200


def test_foreign_host_header_rejected(client):
    r = client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_unknown_host_header_is_rejected(client):
    # "testserver" used to be allowed in production, for the tests' sake.
    r = client.get("/api/health", headers={"Host": "testserver"})
    assert r.status_code == 400


def test_shutdown_needs_the_exact_token(client, monkeypatch):
    from codex_engine import app as app_module

    stopped = []
    monkeypatch.setattr(app_module, "stop_and_exit", lambda: stopped.append(True))
    monkeypatch.setenv("CODEX_ENGINE_SHUTDOWN_TOKEN", "secret")
    for token in (None, "", "secre", "secret2"):
        headers = {} if token is None else {"X-Codex-Engine-Shutdown-Token": token}
        assert client.post("/api/shutdown", headers=headers).status_code == 403
    monkeypatch.delenv("CODEX_ENGINE_SHUTDOWN_TOKEN")
    assert client.post("/api/shutdown", headers={"X-Codex-Engine-Shutdown-Token": ""}).status_code == 403
    assert stopped == []


def test_open_pdf_refuses_a_folder(client, tmp_path):
    folder = tmp_path / "looks-like.pdf"
    folder.mkdir()
    r = client.post("/api/open-pdf", json={"path": str(folder), "page": 1}, headers={"X-Codex-Engine-Client": "1"})
    assert r.status_code == 404


def test_cors_allows_only_what_the_dev_ui_sends(client):
    preflight = {"Origin": "http://127.0.0.1:1420", "Access-Control-Request-Method": "PUT"}
    ok = client.options("/api/settings", headers={**preflight, "Access-Control-Request-Headers": "content-type,x-codex-engine-client"})
    assert ok.status_code == 200
    assert "access-control-allow-credentials" not in ok.headers
    odd = client.options("/api/settings", headers={**preflight, "Access-Control-Request-Headers": "x-something-else"})
    assert odd.status_code == 400
    other = client.options("/api/settings", headers={**preflight, "Origin": "https://evil.example"})
    assert other.status_code == 400


def test_bad_requests_are_rejected_before_reaching_the_handlers(client):
    headers = {"X-Codex-Engine-Client": "1"}
    assert client.post("/api/open-pdf", json={"path": "x.pdf", "page": 0}, headers=headers).status_code == 422
    assert client.post("/api/ingest/duplicate", json={"ingest_id": 1, "action": "keep both"}, headers=headers).status_code == 422
    assert client.post("/api/ingest", json={"path": ""}, headers=headers).status_code == 422
    assert client.get("/api/search", params={"query": "x" * 1001}).status_code == 422
