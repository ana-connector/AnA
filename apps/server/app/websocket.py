import asyncio
import contextlib
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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


def build_adapter():
    mode = default_mode_from_settings()
    env = EnvBuilder(mode).build()
    return ClaudeCodeAdapter(
        workspace_dir=WORKSPACE_DIR,
        env=env,
        model=default_model_from_settings(),
        effort=default_effort_from_settings(),
    )


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

    thread_id = websocket.query_params.get("thread_id") or str(uuid.uuid4())
    await websocket.send_json(
        {"type": "thread_id", "text": thread_id, "session_id": None, "is_error": False}
    )

    adapter = build_adapter()
    transcript = TranscriptLogger(WORKSPACE_DIR)
    graph = websocket.app.state.graph
    config = {
        "configurable": {
            "thread_id": thread_id,
            "adapter": adapter,
            "pool": websocket.app.state.pool,
            "embedding_model": websocket.app.state.embedding_model,
            "transcript": transcript,
        }
    }

    # The queue lives here, in the websocket handler, not inside a
    # ChatSession object: graph.astream() is called once per turn (not once
    # forever like the old ChatSession.run() loop), so the queueing that
    # ChatSession used to provide internally is now explicit here —
    # relay_events() loops pulling one prompt at a time off `queue`, keeping
    # concurrent user messages processed in order instead of interrupting an
    # in-flight turn (see roadmap doc, "동시 메시지 처리: 큐잉").
    queue: "asyncio.Queue[str]" = asyncio.Queue()
    # Set when relay_events() hits an unrecoverable failure, so the receive
    # loop below (which owns the only calls to websocket.close()) can stop
    # and actually close the connection instead of leaving a dead socket
    # that silently swallows every later message the client sends.
    relay_failed = asyncio.Event()

    async def relay_events() -> None:
        try:
            while True:
                prompt = await queue.get()
                # Session id continuity across turns now comes from
                # graph.aget_state(config) (Postgres-backed via the
                # checkpointer) instead of an in-memory ChatSession
                # attribute — this is the whole point of this subproject.
                snapshot = await graph.aget_state(config)
                session_id = (snapshot.values or {}).get("session_id")
                async for event in graph.astream(
                    {"prompt": prompt, "session_id": session_id, "response_text": ""},
                    config=config,
                    stream_mode="custom",
                ):
                    await websocket.send_json(event)
        except Exception as exc:
            # Best-effort: the socket may already be closing/closed by the
            # time a mid-turn failure happens, so swallow send errors here —
            # the point is to try, not to guarantee delivery.
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
                )
            relay_failed.set()

    relay_task = asyncio.create_task(relay_events())
    try:
        while True:
            receive_task = asyncio.ensure_future(websocket.receive_text())
            failure_task = asyncio.ensure_future(relay_failed.wait())
            done, pending = await asyncio.wait(
                {receive_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if failure_task in done:
                # relay_events() already sent an error frame; now actually
                # close so the client sees the connection drop instead of
                # silently queuing messages nobody will ever answer.
                with contextlib.suppress(Exception):
                    await websocket.close(code=1011)
                break
            data = await receive_task
            await queue.put(data)
    except WebSocketDisconnect:
        pass
    finally:
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay_task
        await adapter.terminate()
