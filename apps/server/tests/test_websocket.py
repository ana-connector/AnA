from typing import AsyncIterator, Optional
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.server.app.main import app
from apps.server.app.session_manager import ChatSession
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


class FakeAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


class RaisingAdapter(AgentAdapter):
    """run_stream raises immediately, before yielding anything — simulates
    FileNotFoundError / RuntimeError / etc from a broken subprocess call."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        raise RuntimeError("boom: adapter exploded")
        yield  # pragma: no cover - makes this an async generator

    async def terminate(self) -> None:
        return None


def test_websocket_echoes_prompt_back(tmp_path):
    fake_session = ChatSession(adapter=FakeAdapter(), transcript=TranscriptLogger(str(tmp_path)))

    with patch("apps.server.app.websocket.build_session", return_value=fake_session):
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text("hello")
            first = ws.receive_json()
            second = ws.receive_json()

    assert first == {
        "type": "text",
        "text": "echo:hello",
        "session_id": "sid-1",
        "is_error": False,
    }
    assert second["type"] == "result"
    assert second["text"] == "echo:hello"


def test_websocket_allows_connection_with_no_origin_header(tmp_path):
    """Non-browser clients (curl, test scripts) send no Origin header at
    all — the check must not reject them."""
    fake_session = ChatSession(adapter=FakeAdapter(), transcript=TranscriptLogger(str(tmp_path)))

    with patch("apps.server.app.websocket.build_session", return_value=fake_session) as mock_build:
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text("hi")
            ws.receive_json()

    mock_build.assert_called_once()


def test_websocket_allows_connection_with_allowed_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    fake_session = ChatSession(adapter=FakeAdapter(), transcript=TranscriptLogger(str(tmp_path)))

    with patch("apps.server.app.websocket.build_session", return_value=fake_session) as mock_build:
        client = TestClient(app)
        with client.websocket_connect(
            "/ws/chat", headers={"origin": "http://localhost:3000"}
        ) as ws:
            ws.send_text("hi")
            ws.receive_json()

    mock_build.assert_called_once()


def test_websocket_rejects_disallowed_origin(monkeypatch):
    monkeypatch.setenv("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    mock_build = Mock(side_effect=AssertionError("build_session should not be called"))

    with patch("apps.server.app.websocket.build_session", mock_build):
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws/chat", headers={"origin": "http://evil.example.com"}
            ) as ws:
                ws.receive_text()

    assert exc_info.value.code == 1008
    mock_build.assert_not_called()


def test_websocket_relays_error_when_run_stream_raises():
    with patch(
        "apps.server.app.websocket.build_session",
        return_value=ChatSession(adapter=RaisingAdapter(), transcript=Mock()),
    ):
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text("hello")
            event = ws.receive_json()

    assert event["type"] == "error"
    assert "boom" in event["text"]
    assert event["is_error"] is True


def test_websocket_closes_connection_after_relay_failure():
    """A mid-turn adapter failure must not leave a dead-but-open socket:
    after the error frame, the server must actually close the connection
    (code 1011) instead of silently queuing every later message the client
    sends with nobody left to answer them."""
    with patch(
        "apps.server.app.websocket.build_session",
        return_value=ChatSession(adapter=RaisingAdapter(), transcript=Mock()),
    ):
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_text("hello")
                event = ws.receive_json()
                assert event["type"] == "error"
                # Second receive must observe the server-initiated close
                # rather than hanging or returning another (nonexistent)
                # frame.
                ws.receive_text()

    assert exc_info.value.code == 1011


def test_websocket_sends_error_frame_when_build_session_raises():
    with patch(
        "apps.server.app.websocket.build_session",
        side_effect=RuntimeError("ANTHROPIC_API_KEY_SAVED is not set"),
    ):
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/chat") as ws:
                event = ws.receive_json()
                assert event["type"] == "error"
                assert "ANTHROPIC_API_KEY_SAVED" in event["text"]
                assert event["session_id"] is None
                assert event["is_error"] is True
                ws.receive_text()

    assert exc_info.value.code == 1011
