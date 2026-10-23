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


def _seed_tool_turns(dsn: str, thread_id: str, *, turns: int, calls_per_turn: int) -> None:
    """Turns whose meta carries recorded tool blocks, which is what a real
    transcript is mostly made of."""
    import json
    import psycopg

    zero_vector = "[" + ",".join(["0"] * 384) + "]"
    with psycopg.connect(dsn) as conn:
        for turn in range(turns):
            blocks = [
                {
                    "kind": "tool",
                    "tool": {
                        "tool_use_id": f"call-{turn}-{call}",
                        "tool_name": "Bash",
                        "input": {
                            "command": "echo " + "x" * 500,
                            "description": "y" * 500,
                            "noise": "z" * 2000,
                        },
                        "verdict": "allow",
                        "decided_by": "rule",
                        "rule_id": None,
                        "category": None,
                    },
                }
                for call in range(calls_per_turn)
            ]
            blocks.append({"kind": "text", "text": f"answer {turn}"})
            conn.execute(
                """
                INSERT INTO turns
                    (thread_id, session_id, prompt, response, cost_usd, embedding, agent, meta)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
                """,
                (
                    thread_id, "sid", f"p{turn}", f"answer {turn}", 0.02,
                    zero_vector, "claude",
                    json.dumps({"blocks": blocks, "tokens": 100}),
                ),
            )
        conn.commit()


def test_endpoint_pages_backwards_through_a_long_thread(client):
    _seed_tool_turns(_DSN[0], "hist-page", turns=5, calls_per_turn=1)

    first = client.get("/threads/hist-page/turns?limit=2").json()

    assert [t["prompt"] for t in first["turns"]] == ["p3", "p4"]
    assert first["has_more"] is True
    # The whole thread's totals, not this page's: two turns on screen, five
    # turns paid for.
    assert first["turn_count"] == 5
    assert first["totals"]["tokens"] == 500
    assert round(first["totals"]["cost_usd"], 4) == 0.1

    older = client.get(f"/threads/hist-page/turns?limit=2&before={first['oldest_id']}").json()

    assert [t["prompt"] for t in older["turns"]] == ["p1", "p2"]
    assert older["has_more"] is True

    oldest = client.get(f"/threads/hist-page/turns?limit=2&before={older['oldest_id']}").json()

    assert [t["prompt"] for t in oldest["turns"]] == ["p0"]
    # Nothing above the first turn.
    assert oldest["has_more"] is False


def test_a_short_thread_keeps_its_tool_arguments(client):
    _seed_tool_turns(_DSN[0], "hist-small", turns=2, calls_per_turn=3)

    body = client.get("/threads/hist-small/turns").json()
    tool = body["turns"][0]["meta"]["blocks"][0]["tool"]

    assert body["tools_trimmed"] is False
    assert "noise" in tool["input"]
    assert "input_truncated" not in tool


def test_a_long_thread_ships_one_summary_per_run_of_calls(client):
    _seed_tool_turns(_DSN[0], "hist-big", turns=6, calls_per_turn=10)

    body = client.get("/threads/hist-big/turns").json()
    blocks = body["turns"][0]["meta"]["blocks"]

    assert body["tools_trimmed"] is True
    # Ten consecutive calls, then the answer: one summary and one text block.
    assert [block["kind"] for block in blocks] == ["tool_run", "text"]
    run = blocks[0]["run"]
    assert run["count"] == 10
    assert run["denied"] == 0 and run["pending"] == 0
    # The card still says what the last thing it ran was, and carries the ids
    # the rows are fetched by.
    assert run["last_label"].startswith("echo xxx")
    assert len(run["last_label"]) <= 200
    # The first run of the turn, numbered so the rows can be found without
    # shipping three thousand call ids.
    assert run["index"] == 0
    # The audit-derived copy of the same calls is not sent twice.
    assert body["turns"][0]["tools"] == []


def test_each_turn_numbers_its_runs_from_zero(client):
    # Over the threshold, which is what puts the summaries in the payload.
    _seed_tool_turns(_DSN[0], "hist-order", turns=6, calls_per_turn=8)

    body = client.get("/threads/hist-order/turns").json()

    # One run per turn here, and the numbering restarts per turn because the
    # rows are fetched one turn at a time.
    for turn in body["turns"]:
        assert turn["meta"]["blocks"][0]["run"]["index"] == 0
        assert turn["meta"]["blocks"][0]["run"]["count"] == 8


def test_turn_detail_returns_the_untrimmed_record(client):
    _seed_tool_turns(_DSN[0], "hist-detail", turns=6, calls_per_turn=10)
    listed = client.get("/threads/hist-detail/turns?limit=1").json()
    turn_id = listed["turns"][0]["id"]

    detail = client.get(f"/threads/hist-detail/turns/{turn_id}").json()["turn"]
    tool = detail["meta"]["blocks"][0]["tool"]

    assert tool["input"]["noise"] == "z" * 2000
    assert "input_truncated" not in tool
    assert client.get("/threads/hist-detail/turns/999999").status_code == 404
