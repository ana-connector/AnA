import asyncio
from typing import AsyncIterator, Optional

import pytest

from apps.server.app.session_manager import ChatSession
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


class FakeAdapter(AgentAdapter):
    """Yields one canned text event + one result event per call, echoing
    the prompt back so tests can assert per-turn ordering."""

    def __init__(self):
        self.terminated = False
        self.calls: list[tuple[str, Optional[str]]] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append((prompt, session_id))
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.01)

    async def terminate(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_processes_queued_prompts_in_order(tmp_path):
    adapter = FakeAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.send("first")
    await session.send("second")

    collected: list[AdapterEvent] = []
    run_iter = session.run()
    for _ in range(4):  # 2 events per turn x 2 turns
        collected.append(await run_iter.__anext__())

    texts = [e.text for e in collected if e.type == "text"]
    assert texts == ["echo:first", "echo:second"]
    assert adapter.calls[0] == ("first", None)
    # second call resumes the session id learned from the first turn
    assert adapter.calls[1] == ("second", "sid-1")

    records = transcript.read_all()
    assert [r["prompt"] for r in records] == ["first", "second"]
    assert [r["response"] for r in records] == ["echo:first", "echo:second"]


@pytest.mark.asyncio
async def test_close_terminates_adapter(tmp_path):
    adapter = FakeAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.close()

    assert adapter.terminated is True
