import asyncio
from typing import AsyncIterator, Optional
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.server.app.main import app
from packages.adapters.base import AdapterEvent, AgentAdapter


@pytest.fixture(autouse=True)
def without_planning(monkeypatch):
    """These tests are about the socket, not about planning.

    Every turn now asks the lead whether to delegate (there is no plan/direct
    switch any more), and that call would consume the scripted adapter these
    tests hand the graph. ANA_PLANNER=off is the same escape hatch operators
    get for debugging a problem that is clearly not about planning."""
    monkeypatch.setenv("ANA_PLANNER", "off")



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


# Frames the server sends around the content: which phase the turn is in, which
# CLI was routed to, which model answered. Positional reads skip them, so a new
# one does not break tests that are about the content.
NOISE_FRAMES = {"status", "route", "model", "plan"}


def next_content(ws, skip=NOISE_FRAMES):
    frame = ws.receive_json()
    while frame["type"] in skip:
        frame = ws.receive_json()
    return frame


def test_websocket_sends_thread_id_first_then_echoes_prompt(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat") as ws:
        first = ws.receive_json()
        assert first["type"] == "thread_id"
        assert len(first["text"]) > 0

        ws.send_text("hello")
        # A bare text frame has no agent, so routing runs and announces its
        # choice before the turn streams, and the turn reports its phase.
        second = ws.receive_json()
        assert second["type"] == "route"
        second = next_content(ws)
        third = next_content(ws)

    # "text" events carry only {type, text} (Task 4's call_lead_agent only
    # writes the full session_id/is_error payload for non-"text" events —
    # see graph.py's writer() calls and useChatSocket.ts's ChatEvent type).
    assert second == {"type": "text", "text": "echo:hello"}
    assert third["type"] == "result"


def test_websocket_reuses_supplied_thread_id(client_with_fake_adapter):
    with client_with_fake_adapter.websocket_connect("/ws/chat?thread_id=fixed-thread-abc") as ws:
        first = ws.receive_json()
    assert first["type"] == "thread_id"
    assert first["text"] == "fixed-thread-abc"
    assert first["session_id"] is None
    assert first["is_error"] is False
    # The frame also carries this thread's own workspace directory (see
    # test_workspace_isolation.py); asserted there rather than pinned here.
    assert first["workspace"].endswith("fixed-thread-abc")


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
                event = next_content(ws)
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
                assert next_content(ws)["type"] == "error"
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
                # Read until both turns have ended rather than counting frames:
                # a turn's frame count is not fixed (routing, usage and model
                # frames come and go), and stopping early raced the second turn.
                results = 0
                while results < 2:
                    if ws.receive_json()["type"] == "result":
                        results += 1

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


class SlowThenDoneAdapter(AgentAdapter):
    """Streams a first chunk, waits for a release, then finishes — so a test can
    disconnect while the turn is genuinely still running."""

    released = asyncio.Event()

    def __init__(self):
        self.calls: list[str] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append(prompt)
        yield AdapterEvent(type="text", text="working…", session_id="sid-bg")
        await SlowThenDoneAdapter.released.wait()
        yield AdapterEvent(type="text", text="done", session_id="sid-bg")
        yield AdapterEvent(type="result", text="working…done", session_id="sid-bg", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


def test_a_turn_survives_the_browser_leaving_and_shows_what_it_missed(
    db_pool, postgres_dsn, monkeypatch, tmp_path
):
    """Leaving a workspace mid-turn used to cancel the relay and kill the CLI, so
    the request was simply gone when you came back. Now the turn keeps running
    and the next connection is handed what it missed."""
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    adapter = SlowThenDoneAdapter()
    SlowThenDoneAdapter.released = asyncio.Event()
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=adapter
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat?thread_id=bg-1") as ws:
                ws.receive_json()  # thread_id
                ws.send_json({"prompt": "long job", "agent": "claude", "plan_mode": False})
                assert next_content(ws)["text"] == "working…"
                # The browser goes to another workspace.

            with client.websocket_connect("/ws/chat?thread_id=bg-1") as ws:
                opened = ws.receive_json()
                # The conversation says it is mid-turn before anything renders.
                assert opened["running"] is True
                replay = ws.receive_json()
                assert replay["type"] == "replay"
                kinds = [frame["type"] for frame in replay["frames"]]
                assert kinds[0] == "prompt"  # the request it never saw
                assert "text" in kinds

                # The same turn, still the same CLI process, finishes here.
                SlowThenDoneAdapter.released.set()
                frame = next_content(ws)
                while frame["type"] == "text" and frame["text"] != "done":
                    frame = next_content(ws)
                assert frame["text"] == "done"

    # One CLI call for one prompt: the turn was never restarted.
    assert adapter.calls == ["long job"]


def test_background_turns_off_restores_the_old_lifetime(
    db_pool, postgres_dsn, monkeypatch, tmp_path
):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_BACKGROUND_TURNS", "off")
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=FakeAdapter()
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/chat?thread_id=bg-off") as ws:
                ws.receive_json()
            # The runner went with the connection, so the next one starts fresh
            # and has nothing to replay.
            with client.websocket_connect("/ws/chat?thread_id=bg-off") as ws:
                opened = ws.receive_json()
                assert opened["running"] is False
