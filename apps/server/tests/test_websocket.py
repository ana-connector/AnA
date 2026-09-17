import asyncio
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


class OrderTrackingAdapter(AgentAdapter):
    """Delays its first call briefly so a second prompt can be enqueued
    while the first turn is still in flight, then records call order --
    proving relay_events()'s queue processes turns FIFO instead of
    interleaving concurrent messages (roadmap doc, '동시 메시지 처리: 큐잉').
    Prompts are RAG-augmented by call_lead_agent before reaching here, so
    assertions check the suffix rather than an exact match."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append(prompt)
        if len(self.calls) == 1:
            await asyncio.sleep(0.1)
        yield AdapterEvent(type="text", text="ok", session_id="sid-1")
        yield AdapterEvent(type="result", text="ok", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


class TerminateTrackingAdapter(AgentAdapter):
    def __init__(self):
        self.terminated = False

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text="ok", session_id="sid-1")
        yield AdapterEvent(type="result", text="ok", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def client_with_fake_adapter(db_pool, postgres_dsn, monkeypatch, tmp_path):
    # Depends on db_pool (not just postgres_dsn) so the turns table is
    # truncated before this fixture runs: the graph's call_lead_agent node
    # RAG-augments every prompt via search_similar_turns with no relevance
    # threshold, so leftover rows from earlier test files sharing the same
    # session-scoped Postgres container (test_memory_rag.py, test_graph.py)
    # would otherwise get prepended into "hello" and break exact-echo
    # assertions purely based on test execution order.
    #
    # build_transcript is patched too (not just build_adapter): WORKSPACE_DIR
    # is read once at import time, so monkeypatch.setenv("ANA_WORKSPACE_DIR",
    # ...) can never reach TranscriptLogger(WORKSPACE_DIR) if it were built
    # inline -- only patching the build_transcript() factory keeps these
    # tests from writing junk rows into the real workspace's
    # .ana_transcript.jsonl (this previously happened and had to be cleaned
    # up by hand).
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=FakeAdapter()
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
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


def test_websocket_allows_connection_with_allowed_origin(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect(
        "/ws/chat", headers={"origin": "http://localhost:3000"}
    ) as ws:
        first = ws.receive_json()
    assert first["type"] == "thread_id"


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


def test_websocket_relays_error_when_run_stream_raises(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=RaisingAdapter()
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                ws.send_text("hello")
                event = ws.receive_json()
    assert event["type"] == "error"
    assert "boom" in event["text"]


def test_websocket_closes_connection_after_relay_failure(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=RaisingAdapter()
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                ws.send_text("hello")
                ws.receive_json()  # error frame
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_text()
    assert exc_info.value.code == 1011


def test_websocket_sends_error_frame_when_build_adapter_raises(postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    mock_build_adapter = Mock(side_effect=RuntimeError("ANTHROPIC_API_KEY_SAVED is not set"))
    with patch("apps.server.app.websocket.build_adapter", mock_build_adapter):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                event = ws.receive_json()
                assert event["type"] == "error"
                assert "ANTHROPIC_API_KEY_SAVED" in event["text"]
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_text()
    assert exc_info.value.code == 1011


def test_websocket_processes_queued_prompts_in_order(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    adapter = OrderTrackingAdapter()
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=adapter
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id
                # Both sent back-to-back, before the first turn resolves
                # (OrderTrackingAdapter sleeps on its first call) -- proves
                # the queue serializes turns instead of interleaving them.
                ws.send_text("first-msg")
                ws.send_text("second-msg")
                for _ in range(4):  # 2 events per turn x 2 turns
                    ws.receive_json()

    assert len(adapter.calls) == 2
    assert adapter.calls[0].endswith("first-msg")
    assert adapter.calls[1].endswith("second-msg")


def test_websocket_terminates_adapter_on_disconnect(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    adapter = TerminateTrackingAdapter()
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=adapter
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat") as ws:
                ws.receive_json()  # thread_id

    assert adapter.terminated is True
