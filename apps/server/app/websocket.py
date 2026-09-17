import asyncio
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
    session = build_session()

    async def relay_events() -> None:
        async for event in session.run():
            await websocket.send_json(
                {
                    "type": event.type,
                    "text": event.text,
                    "session_id": event.session_id,
                    "is_error": event.is_error,
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
        await session.close()
