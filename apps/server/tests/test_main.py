import contextlib

from fastapi.testclient import TestClient

from apps.server.app.main import app


def test_healthz_returns_ok(monkeypatch, postgres_dsn):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lifespan_attaches_pool_checkpointer_and_graph(monkeypatch, postgres_dsn):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as client:
        assert client.app.state.pool is not None
        assert client.app.state.checkpointer is not None
        assert client.app.state.graph is not None
        assert client.app.state.embedding_model is not None
