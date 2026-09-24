"""Turns that outlive the connection that asked for them.

No websocket here: a runner is driven directly, which is the level the behavior
lives at — a browser is one thing that happens to be attached.
"""
import asyncio

import pytest

from apps.server.app.runners import (
    MAX_BUFFERED_FRAMES,
    RunnerRegistry,
    TurnRunner,
    background_turns_enabled,
    idle_seconds,
)
from packages.governance.session import GateRegistry


class FakeGraph:
    """Emits the frames a turn would, with a gate the test controls."""

    def __init__(self, frames: list[dict], *, hold: asyncio.Event | None = None):
        self.frames = frames
        self.hold = hold
        self.calls: list[str] = []

    async def aget_state(self, _config):
        class Snapshot:
            values: dict = {}

        return Snapshot()

    async def astream(self, state, config=None, stream_mode=None):
        self.calls.append(state["prompt"])
        for frame in self.frames:
            if self.hold is not None and frame.get("type") == "result":
                # Parks the turn just before it finishes, so a test can detach
                # and reattach while it is still running.
                await self.hold.wait()
            yield frame


class FakeAdapter:
    def __init__(self) -> None:
        self.terminated = 0

    async def terminate(self) -> None:
        self.terminated += 1


def _runner(graph, *, thread_id: str = "t1", adapters: dict | None = None) -> TurnRunner:
    gate_registry = GateRegistry()

    async def no_sink(_frame):
        return None

    session = gate_registry.register(thread_id, no_sink, workspace="/tmp")
    return TurnRunner(
        thread_id=thread_id,
        workspace_dir="/tmp",
        workspace_name=thread_id,
        gate_session=session,
        gate_registry=gate_registry,
        adapters=adapters if adapters is not None else {"claude": FakeAdapter()},
        config={"configurable": {"thread_id": thread_id}},
        graph=graph,
    )


TEXT = {"type": "text", "text": "hello"}
RESULT = {"type": "result", "text": "", "total_cost_usd": 0.0, "total_tokens": 0}


@pytest.mark.asyncio
async def test_a_turn_finishes_with_nobody_attached():
    """The point of the whole file: opening another workspace used to cancel the
    relay and kill both CLIs, so the work in flight was lost and the
    conversation came back empty."""
    graph = FakeGraph([TEXT, RESULT])
    runner = _runner(graph)

    async def sink(_frame):
        return None

    runner.attach(sink)
    await runner.submit("do the thing", "claude", True)
    runner.detach(sink)

    for _ in range(200):
        if graph.calls and not runner.busy:
            break
        await asyncio.sleep(0.01)

    assert graph.calls == ["do the thing"]
    await runner.close()


@pytest.mark.asyncio
async def test_frames_from_a_detached_turn_are_replayed_on_return():
    hold = asyncio.Event()
    graph = FakeGraph([TEXT, RESULT], hold=hold)
    runner = _runner(graph)
    await runner.submit("long job", "claude", True)

    # Nothing attached: the text still arrives, into the buffer.
    for _ in range(200):
        if any(frame.get("type") == "text" for frame in runner.buffer):
            break
        await asyncio.sleep(0.01)

    seen: list[dict] = []

    async def sink(frame):
        seen.append(frame)

    missed = runner.attach(sink)

    # The prompt comes back too: a client that never saw it would show the
    # streaming reply with nothing above it.
    assert missed[0] == {"type": "prompt", "text": "long job", "agent": "claude"}
    assert any(frame.get("type") == "text" for frame in missed)
    assert runner.busy is True

    hold.set()
    for _ in range(200):
        if not runner.busy:
            break
        await asyncio.sleep(0.01)

    # Live from here: the rest of the turn goes to the attached client.
    assert any(frame.get("type") == "result" for frame in seen)
    # And the buffer is empty again, because from now on the turn is in the
    # database and history is what shows it.
    assert runner.buffer == []
    await runner.close()


@pytest.mark.asyncio
async def test_the_buffer_is_bounded():
    graph = FakeGraph([{"type": "text", "text": str(i)} for i in range(MAX_BUFFERED_FRAMES + 50)])
    runner = _runner(graph)
    await runner.submit("chatty", "claude", True)

    for _ in range(400):
        if not runner.busy:
            break
        await asyncio.sleep(0.01)

    assert len(runner.buffer) <= MAX_BUFFERED_FRAMES
    await runner.close()


@pytest.mark.asyncio
async def test_a_send_failure_detaches_without_stopping_the_turn():
    """A socket that dies mid-send is a detach, not a failed turn."""
    graph = FakeGraph([TEXT, TEXT, RESULT])
    runner = _runner(graph)

    async def broken(_frame):
        raise ConnectionResetError("gone")

    runner.attach(broken)
    await runner.submit("keep going", "claude", True)

    for _ in range(200):
        if graph.calls and not runner.busy:
            break
        await asyncio.sleep(0.01)

    assert runner.sink is None
    assert graph.calls == ["keep going"]
    await runner.close()


@pytest.mark.asyncio
async def test_closing_a_runner_kills_its_clis_and_drops_its_token():
    adapter = FakeAdapter()
    graph = FakeGraph([TEXT, RESULT])
    runner = _runner(graph, adapters={"claude": adapter})
    token = runner.gate_session.token

    await runner.close()

    assert adapter.terminated == 1
    assert runner.gate_registry.get(token) is None
    # Idempotent: the sweeper and shutdown can both reach the same runner.
    await runner.close()
    assert adapter.terminated == 1


@pytest.mark.asyncio
async def test_an_idle_unwatched_runner_is_swept(monkeypatch):
    monkeypatch.setenv("ANA_RUNNER_IDLE_SECONDS", "0")
    graph = FakeGraph([TEXT, RESULT])
    runner = _runner(graph, thread_id="idle-1")
    adapter = list(runner.adapters.values())[0]
    registry = RunnerRegistry()
    registry.put(runner)

    # Nothing attached, nothing running: idle from the moment it was made.
    assert runner.idle_for() >= 0
    registry.start_sweeper(interval=0.01)
    for _ in range(300):
        if registry.get("idle-1") is None:
            break
        await asyncio.sleep(0.01)

    assert registry.get("idle-1") is None
    assert adapter.terminated == 1
    await registry.close_all()


@pytest.mark.asyncio
async def test_a_busy_or_watched_runner_is_not_idle():
    hold = asyncio.Event()
    runner = _runner(FakeGraph([TEXT, RESULT], hold=hold), thread_id="busy-1")

    async def sink(_frame):
        return None

    runner.attach(sink)
    assert runner.idle_for() == 0.0  # watched

    await runner.submit("work", "claude", True)
    runner.detach(sink)
    for _ in range(200):
        if runner.busy:
            break
        await asyncio.sleep(0.01)
    assert runner.idle_for() == 0.0  # detached, but still working

    hold.set()
    await runner.close()


@pytest.mark.asyncio
async def test_a_failed_relay_is_not_handed_out_again():
    """A runner whose relay died would accept prompts nobody answers, so the
    registry drops it and the next connection builds a fresh one."""
    runner = _runner(FakeGraph([TEXT, RESULT]), thread_id="dead-1")
    registry = RunnerRegistry()
    registry.put(runner)
    runner.failed.set()

    assert registry.get("dead-1") is None
    await runner.close()


def test_the_lifetime_can_be_switched_back(monkeypatch):
    assert background_turns_enabled() is True
    monkeypatch.setenv("ANA_BACKGROUND_TURNS", "off")
    assert background_turns_enabled() is False


def test_the_idle_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("ANA_RUNNER_IDLE_SECONDS", "30")
    assert idle_seconds() == 30
    monkeypatch.setenv("ANA_RUNNER_IDLE_SECONDS", "nonsense")
    assert idle_seconds() == 900
