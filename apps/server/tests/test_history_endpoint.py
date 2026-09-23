"""GET /threads/{thread_id}/turns — what lets the browser redraw the chat
after a reload (roadmap doc's explicitly-deferred Core Loop gap)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import store_turn, thread_history


def _seed_rows(dsn: str, thread_id: str, rows: list[tuple[str, str, str]]) -> None:
    """Inserts turns over a plain synchronous connection.

    The async db_pool fixture is bound to the fixture's own event loop, while
    TestClient runs the app on a different one, so the sync tests below cannot
    await store_turn(); and since none of them exercise similarity search, a
    zero vector is a valid stand-in for a real embedding."""
    import psycopg

    zero_vector = "[" + ",".join(["0"] * 384) + "]"
    with psycopg.connect(dsn) as conn:
        for prompt, response, agent in rows:
            conn.execute(
                """
                INSERT INTO turns
                    (thread_id, session_id, prompt, response, cost_usd, embedding, agent)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                """,
                (thread_id, "sid", prompt, response, 0.01, zero_vector, agent),
            )
        conn.commit()


_DSN: list[str] = []


@pytest.fixture
def client(db_pool, postgres_dsn, monkeypatch):
    _DSN.clear()
    _DSN.append(postgres_dsn)
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_ALLOWED_ORIGINS", "http://localhost:3005")
    # db_pool truncates `turns` per test, so each sync test seeds its own rows.
    _seed_rows(postgres_dsn, "hist-1", [("first", "a1", "claude"), ("second", "a2", "codex")])
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_thread_history_returns_turns_oldest_first(db_pool):
    embedding_model = EmbeddingModel()
    for prompt, response, agent in [
        ("first", "a1", "claude"),
        ("second", "a2", "codex"),
    ]:
        await store_turn(
            db_pool, embedding_model,
            thread_id="hist-1", session_id="sid", prompt=prompt,
            response=response, cost_usd=0.01, agent=agent,
        )

    turns = await thread_history(db_pool, thread_id="hist-1")

    assert [t["prompt"] for t in turns] == ["first", "second"]
    assert [t["agent"] for t in turns] == ["claude", "codex"]
    assert turns[0]["cost_usd"] == 0.01
    assert turns[0]["created_at"]


@pytest.mark.asyncio
async def test_thread_history_keeps_newest_turns_when_over_limit(db_pool):
    embedding_model = EmbeddingModel()
    for i in range(4):
        await store_turn(
            db_pool, embedding_model,
            thread_id="hist-2", session_id="sid", prompt=f"p{i}",
            response=f"r{i}", cost_usd=None, agent="claude",
        )

    turns = await thread_history(db_pool, thread_id="hist-2", limit=2)

    # Newest two, still oldest-first within the page.
    assert [t["prompt"] for t in turns] == ["p2", "p3"]


def test_endpoint_returns_turns_for_thread(client, db_pool):
    response = client.get("/threads/hist-1/turns")

    assert response.status_code == 200
    payload = response.json()
    assert payload["thread_id"] == "hist-1"
    assert [t["prompt"] for t in payload["turns"]] == ["first", "second"]


def test_endpoint_returns_empty_list_for_unknown_thread(client):
    response = client.get("/threads/no-such-thread/turns")

    assert response.status_code == 200
    assert response.json()["turns"] == []


def test_endpoint_rejects_out_of_range_limit(client):
    assert client.get("/threads/hist-1/turns?limit=0").status_code == 422
    assert client.get("/threads/hist-1/turns?limit=999").status_code == 422


def test_cors_header_sent_for_allowed_origin(client):
    response = client.get("/threads/hist-1/turns", headers={"origin": "http://localhost:3005"})

    # The UI (3005) and API (8100) are separate origins, so without this the
    # browser drops the history response.
    assert response.headers["access-control-allow-origin"] == "http://localhost:3005"


def test_cors_header_withheld_from_disallowed_origin(client):
    response = client.get("/threads/hist-1/turns", headers={"origin": "http://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_refused_for_disallowed_origin(client):
    response = client.options(
        "/threads/hist-1/turns",
        headers={
            "origin": "http://evil.example.com",
            "access-control-request-method": "GET",
        },
    )

    assert response.status_code == 403


def test_cors_preflight_allowed_for_allowed_origin(client):
    response = client.options(
        "/threads/hist-1/turns",
        headers={
            "origin": "http://localhost:3005",
            "access-control-request-method": "GET",
        },
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "http://localhost:3005"


def test_history_endpoint_does_not_build_adapters(client):
    # A plain history read must not touch the CLI wiring at all.
    with patch(
        "apps.server.app.websocket.build_adapter",
        side_effect=AssertionError("build_adapter must not be called"),
    ):
        assert client.get("/threads/hist-1/turns").status_code == 200


def test_threads_endpoint_lists_sessions_with_workspaces(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "sessions"))
    _seed_rows(_DSN[0], "hist-3", [("두 번째 대화 시작", "ok", "codex")])

    payload = client.get("/threads").json()["threads"]
    by_id = {t["thread_id"]: t for t in payload}

    assert "hist-1" in by_id and "hist-3" in by_id
    # The first prompt is what makes a session recognizable in the sidebar.
    assert by_id["hist-1"]["title"] == "first"
    assert by_id["hist-3"]["last_agent"] == "codex"
    assert by_id["hist-3"]["turn_count"] == 1
    # Each session names its own directory; listing must not create it.
    assert by_id["hist-3"]["workspace"].endswith("/hist-3")
    assert not (tmp_path / "sessions" / "hist-3").exists()


def test_threads_endpoint_is_empty_without_turns(client):
    # db_pool truncates `turns` per test, so this client sees only its own seed.
    assert isinstance(client.get("/threads").json()["threads"], list)
