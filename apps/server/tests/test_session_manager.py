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


class ErrorOnlyAdapter(AgentAdapter):
    """Yields only an error event (no result) — simulates a non-zero CLI
    exit, per ClaudeCodeAdapter's contract when the subprocess fails."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(
            type="error", text="claude: command not found", session_id="sid-err", is_error=True
        )

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_failed_turn_still_reaches_transcript(tmp_path):
    """I1: a turn that ends in an error (no result event) must still be
    logged — that's exactly the failure case the transcript exists to cover
    (reconstructing context if the lead agent dies)."""
    adapter = ErrorOnlyAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.send("hello")

    run_iter = session.run()
    event = await run_iter.__anext__()
    assert event.type == "error"

    records = transcript.read_all()
    assert len(records) == 1
    assert records[0]["prompt"] == "hello"
    assert records[0]["response"] == "claude: command not found"


class FirstCallErrorsThenSucceedsAdapter(AgentAdapter):
    """First run_stream call fails via an error event carrying a session_id
    that was never actually created on disk (e.g. the CLI process died
    before reaching that point). Second call succeeds with a different,
    real session_id."""

    def __init__(self):
        self.calls: list[tuple[str, Optional[str]]] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append((prompt, session_id))
        if len(self.calls) == 1:
            yield AdapterEvent(
                type="error", text="boom", session_id="poisoned-sid", is_error=True
            )
        else:
            yield AdapterEvent(type="text", text="ok", session_id="real-sid")
            yield AdapterEvent(type="result", text="ok", session_id="real-sid", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_session_id_is_not_poisoned_by_a_failed_first_turn(tmp_path):
    """I2: only a successful "result" event may set self.session_id. If the
    very first turn fails before the CLI actually created a session on
    disk, later turns must not --resume a session_id that doesn't exist."""
    adapter = FirstCallErrorsThenSucceedsAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.send("first")
    await session.send("second")

    run_iter = session.run()
    events: list[AdapterEvent] = []
    for _ in range(3):  # 1 error event (turn 1) + 2 events (turn 2)
        events.append(await run_iter.__anext__())

    # The poisoned session_id from the failed first turn must never be
    # adopted, and the second call must not have been resumed against it.
    assert adapter.calls[0] == ("first", None)
    assert adapter.calls[1] == ("second", None)
    assert session.session_id == "real-sid"
