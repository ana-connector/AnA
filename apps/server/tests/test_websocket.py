from typing import AsyncIterator, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

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
