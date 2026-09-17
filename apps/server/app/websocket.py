import asyncio
import contextlib
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from apps.server.app.session_manager import ChatSession
from packages.adapters.claude_code import ClaudeCodeAdapter
from packages.adapters.env_builder import (
    EnvBuilder,
    default_effort_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
)
from packages.adapters.transcript import TranscriptLogger

router = APIRouter()

WORKSPACE_DIR = os.environ.get("ANA_WORKSPACE_DIR", "/home/hello/workspace")


def _allowed_origins() -> set[str]:
    """Reads ANA_ALLOWED_ORIGINS fresh on every call (rather than caching at
    import time) so tests can monkeypatch it per-test and so a running
    server picks up an updated env var without a code change."""
    raw = os.environ.get("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    return {origin.strip() for origin in raw.split(",") if origin.strip()}


def build_session() -> ChatSession:
    mode = default_mode_from_settings()
    env = EnvBuilder(mode).build()
    adapter = ClaudeCodeAdapter(
        workspace_dir=WORKSPACE_DIR,
        env=env,
        model=default_model_from_settings(),
        effort=default_effort_from_settings(),
    )
    transcript = TranscriptLogger(WORKSPACE_DIR)
    return ChatSession(adapter=adapter, transcript=transcript)


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    # C1: browsers do not apply same-origin policy to WebSocket handshakes,
    # so any page the user has open in another tab could otherwise drive the
    # lead agent (which runs with --dangerously-skip-permissions). The
    # Origin header is browser-enforced, so its *absence* just means a
    # non-browser client (curl, test scripts) and is not itself suspicious —
    # only a *present but disallowed* Origin is rejected.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in _allowed_origins():
        await websocket.close(code=1008)
        return

    try:
        session = build_session()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
            )
        await websocket.close(code=1011)
        return

    async def relay_events() -> None:
        try:
            async for event in session.run():
                await websocket.send_json(
                    {
                        "type": event.type,
                        "text": event.text,
                        "session_id": event.session_id,
                        "is_error": event.is_error,
                    }
                )
        except Exception as exc:
            # Best-effort: the socket may already be closing/closed by the
            # time a mid-turn failure happens, so swallow send errors here —
            # the point is to try, not to guarantee delivery.
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {
                        "type": "error",
                        "text": str(exc),
                        "session_id": session.session_id,
                        "is_error": True,
                    }
                )

    relay_task = asyncio.create_task(relay_events())
    try:
        while True:
            data = await websocket.receive_text()
            await session.send(data)
    except WebSocketDisconnect:
        pass
    finally:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
        await session.close()
