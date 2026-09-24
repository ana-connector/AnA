"""WebSocket agent-selection protocol
(docs/plans/2026-09-22-dual-cli-orchestration.md)."""
from typing import AsyncIterator, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from apps.server.app.websocket import _parse_client_frame
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.orchestrator.routing import AUTO


@pytest.fixture(autouse=True)
def without_planning(monkeypatch):
    """These tests are about the socket, not about planning.

    Every turn now asks the lead whether to delegate (there is no plan/direct
    switch any more), and that call would consume the scripted adapter these
    tests hand the graph. ANA_PLANNER=off is the same escape hatch operators
    get for debugging a problem that is clearly not about planning."""
    monkeypatch.setenv("ANA_PLANNER", "off")



class NamedAdapter(AgentAdapter):
    def __init__(self, name: str):
        self.name = name

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"{self.name}:{prompt}", session_id=f"{self.name}-sid")
        yield AdapterEvent(
            type="result", text=f"{self.name}:{prompt}", session_id=f"{self.name}-sid"
        )

    async def terminate(self) -> None:
        return None


def test_parse_client_frame_reads_json_prompt_and_agent():
    frame = _parse_client_frame('{"prompt": "hi", "agent": "codex"}')
    assert (frame.kind, frame.prompt, frame.agent) == ("prompt", "hi", "codex")


def test_parse_client_frame_accepts_bare_text_for_backward_compatibility():
    # The original protocol sent the prompt as a bare text frame; existing
    # clients must keep working. With no agent named, routing decides.
    frame = _parse_client_frame("hi")
    assert (frame.kind, frame.prompt, frame.agent) == ("prompt", "hi", AUTO)


def test_parse_client_frame_normalizes_unknown_agent():
    # Client-supplied and used to pick a subprocess, so it must never pass
    # through unvalidated: an unknown name falls back to routing, never to a
    # literal subprocess name.
    frame = _parse_client_frame('{"prompt": "hi", "agent": "rm -rf"}')
    assert (frame.prompt, frame.agent) == ("hi", AUTO)


def test_parse_client_frame_falls_back_when_prompt_missing():
    frame = _parse_client_frame('{"agent": "codex"}')
    assert (frame.prompt, frame.agent) == ('{"agent": "codex"}', "codex")


@pytest.fixture
def client_with_both_adapters(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with patch(
        "apps.server.app.websocket.build_adapter", return_value=NamedAdapter("claude")
    ), patch(
        "apps.server.app.websocket.build_codex_adapter", return_value=NamedAdapter("codex")
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


def test_websocket_routes_json_frame_to_requested_agent(client_with_both_adapters):
    with client_with_both_adapters.websocket_connect("/ws/chat?thread_id=ws-agents-1") as ws:
        ws.receive_json()  # thread_id
        # plan_mode off: this test is about routing a frame to an agent, not
        # about the planner (which is on by default and adds its own frames).
        ws.send_json({"prompt": "hello", "agent": "codex", "plan_mode": False})
        text_event = next_content(ws)
        result_event = next_content(ws)

    assert text_event == {"type": "text", "text": "codex:hello"}
    assert result_event["agent"] == "codex"


def test_websocket_bare_text_frame_routes_to_the_default_agent(client_with_both_adapters):
    with client_with_both_adapters.websocket_connect("/ws/chat?thread_id=ws-agents-2") as ws:
        ws.receive_json()  # thread_id
        ws.send_text("hello")
        route_event = ws.receive_json()
        # A bare text frame takes the planner default, so the plan frame comes
        # before the turn's own output.
        text_event = next_content(ws)

    # "hello" matches no routing rule and the thread has no history, so the
    # table's default answers it.
    assert route_event["type"] == "route"
    assert text_event["text"].startswith("claude:")
