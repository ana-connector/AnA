from typing import AsyncIterator, Optional
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.server.app.main import app
from packages.adapters.base import AdapterEvent, AgentAdapter


class FakeAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


class RaisingAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        raise RuntimeError("boom: adapter exploded")
        yield  # pragma: no cover

    async def terminate(self) -> None:
        return None


@pytest.fixture
def client_with_fake_adapter(db_pool, postgres_dsn, monkeypatch):
    # Depends on db_pool (not just postgres_dsn) so the turns table is
    # truncated before this fixture runs: the graph's call_lead_agent node
    # RAG-augments every prompt via search_similar_turns with no relevance
    # threshold, so leftover rows from earlier test files sharing the same
    # session-scoped Postgres container (test_memory_rag.py, test_graph.py)
    # would otherwise get prepended into "hello" and break exact-echo
    # assertions purely based on test execution order.
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=FakeAdapter()
    ):
        with TestClient(app) as client:
            yield client


def test_websocket_sends_thread_id_first_then_echoes_prompt(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat") as ws:
        first = ws.receive_json()
        assert first["type"] == "thread_id"
        assert len(first["text"]) > 0

        ws.send_text("hello")
        second = ws.receive_json()
        third = ws.receive_json()

    # "text" events carry only {type, text} (Task 4's call_lead_agent only
    # writes the full session_id/is_error payload for non-"text" events —
    # see graph.py's writer() calls and useChatSocket.ts's ChatEvent type).
    assert second == {"type": "text", "text": "echo:hello"}
    assert third["type"] == "result"


def test_websocket_reuses_supplied_thread_id(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat?thread_id=fixed-thread-abc") as ws:
        first = ws.receive_json()
    assert first == {"type": "thread_id", "text": "fixed-thread-abc", "session_id": None, "is_error": False}


def test_websocket_rejects_disallowed_origin(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    mock_build = Mock(side_effect=AssertionError("build_adapter should not be called"))
    with patch("apps.server.app.websocket.build_adapter", mock_build):
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(
                    "/ws/chat", headers={"origin": "http://evil.example.com"}
                ) as ws:
                    ws.receive_text()
    assert exc_info.value.code == 1008
    mock_build.assert_not_called()


def test_websocket_relays_error_when_run_stream_raises(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch("apps.server.app.websocket.build_adapter", return_value=RaisingAdapter()):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                ws.send_text("hello")
                event = ws.receive_json()
    assert event["type"] == "error"
    assert "boom" in event["text"]
