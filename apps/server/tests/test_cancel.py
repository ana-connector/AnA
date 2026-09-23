"""Cancelling an in-flight turn without dropping the connection."""
import asyncio
from typing import AsyncIterator, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from apps.server.app.websocket import _parse_client_frame
from packages.adapters.base import AdapterEvent, AgentAdapter


class SlowAdapter(AgentAdapter):
    """Streams one chunk, then stalls — long enough for a cancel frame to
    arrive mid-turn. Records whether terminate() was called, since cancelling
    the await alone would leave a real CLI subprocess running."""

    def __init__(self):
        self.terminated = False
        self.started = asyncio.Event()

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.started.set()
        yield AdapterEvent(type="text", text="working", session_id="sid-1")
        await asyncio.sleep(30)
        yield AdapterEvent(type="result", text="done", session_id="sid-1")

    async def terminate(self) -> None:
        self.terminated = True


class FastAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"ok:{prompt[-6:]}", session_id="sid-2")
        yield AdapterEvent(type="result", text="ok", session_id="sid-2")

    async def terminate(self) -> None:
        return None


def test_parse_client_frame_recognizes_cancel():
    frame = _parse_client_frame('{"type": "cancel"}')
    assert frame.kind == "cancel"


def test_parse_client_frame_kinds_for_prompts():
    assert _parse_client_frame('{"prompt": "hi", "agent": "codex"}').kind == "prompt"
    assert _parse_client_frame("hi").kind == "prompt"


@pytest.fixture
def slow_client(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    slow = SlowAdapter()
    with patch("apps.server.app.websocket.build_adapter", return_value=slow), patch(
        "apps.server.app.websocket.build_codex_adapter", return_value=FastAdapter()
    ), patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as client:
            yield client, slow


def test_cancel_stops_turn_and_terminates_cli(slow_client):
    client, slow = slow_client
    with client.websocket_connect("/ws/chat?thread_id=cancel-1") as ws:
        ws.receive_json()  # thread_id
        # plan_mode off: cancellation is about the turn, and planning would
        # add a CLI call this fake adapter would answer with its stall.
        ws.send_json({"prompt": "long job", "agent": "claude", "plan_mode": False})
        assert ws.receive_json()["type"] == "text"  # turn is under way
        ws.send_json({"type": "cancel"})
        event = ws.receive_json()

    assert event["type"] == "cancelled"
    assert slow.terminated is True, "the CLI subprocess must be killed, not just awaited"


def test_connection_survives_cancel_and_accepts_next_turn(slow_client):
    client, _ = slow_client
    with client.websocket_connect("/ws/chat?thread_id=cancel-2") as ws:
        ws.receive_json()  # thread_id
        # plan_mode off: cancellation is about the turn, and planning would
        # add a CLI call this fake adapter would answer with its stall.
        ws.send_json({"prompt": "long job", "agent": "claude", "plan_mode": False})
        ws.receive_json()  # text
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "cancelled"

        # Same socket, next turn — a cancel must not tear the connection down.
        ws.send_json({"prompt": "after cancel", "agent": "codex", "plan_mode": False})
        follow_up = ws.receive_json()

    assert follow_up["type"] == "text"
    assert follow_up["text"].startswith("ok:")


def test_cancel_with_nothing_running_still_answers(slow_client):
    client, _ = slow_client
    with client.websocket_connect("/ws/chat?thread_id=cancel-3") as ws:
        ws.receive_json()  # thread_id
        ws.send_json({"type": "cancel"})
        event = ws.receive_json()

    # A client whose UI thinks a turn is running needs an answer to reset it.
    assert event["type"] == "cancelled"


def test_cancelled_turn_is_still_recorded(slow_client, db_pool, postgres_dsn):
    """A cancelled turn's partial output stays in the transcript/turns table:
    the graph node logs whatever it accumulated before the CancelledError."""
    import psycopg

    client, _ = slow_client
    with client.websocket_connect("/ws/chat?thread_id=cancel-4") as ws:
        ws.receive_json()
        # plan_mode off: cancellation is about the turn, and planning would
        # add a CLI call this fake adapter would answer with its stall.
        ws.send_json({"prompt": "long job", "agent": "claude", "plan_mode": False})
        ws.receive_json()
        ws.send_json({"type": "cancel"})
        ws.receive_json()

    with psycopg.connect(postgres_dsn) as conn:
        rows = conn.execute(
            "SELECT response FROM turns WHERE thread_id = %s", ("cancel-4",)
        ).fetchall()

    assert rows, "the partial turn should have been stored"
    assert rows[0][0] == "working"
