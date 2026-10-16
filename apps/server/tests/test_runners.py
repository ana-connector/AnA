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
async def test_cancel_auto_stops_routed_cli_and_delegated_worker():
    hold = asyncio.Event()
    lead = FakeAdapter()
    worker = FakeAdapter()
    runner = _runner(
        FakeGraph([TEXT, RESULT], hold=hold),
        adapters={"codex": lead, "worker/codex/one": worker},
    )
    cancelled = asyncio.Event()

    async def sink(frame):
        if frame.get("type") == "cancelled":
            cancelled.set()

    async def delegation():
        await asyncio.Event().wait()

    runner.attach(sink)
    await runner.submit("long job", "auto", False)
    for _ in range(200):
        if runner.current_turn_task is not None and runner.current_turn_task.done() is False:
            break
        await asyncio.sleep(0.01)
    delegated = asyncio.create_task(delegation())
    runner.gate_session.active_delegations.add(delegated)
    task = {"id": "native:routed:one", "agent": "codex", "status": "running"}
    runner.gate_session.native_agents["routed:one"] = task

    assert runner.cancel_current()
    await asyncio.wait_for(cancelled.wait(), timeout=2)

    assert delegated.cancelled()
    assert task["status"] == "error"
    assert task["error"] == "사용자가 중지함"
    assert lead.terminated == 1
    assert worker.terminated == 1
    await runner.close()


@pytest.mark.asyncio
async def test_cancel_resolves_pending_approval_as_deny():
    hold = asyncio.Event()
    runner = _runner(FakeGraph([TEXT, RESULT], hold=hold))
    seen = []

    async def sink(frame):
        seen.append(frame)

    runner.attach(sink)
    await runner.submit("slow turn", "claude", False)
    async with asyncio.timeout(2):
        while runner.current_turn_task is None or not runner.graph.calls:
            await asyncio.sleep(0.01)
    approval = asyncio.create_task(runner.gate_registry.request_approval(
        runner.gate_session, {"tool_name": "Bash", "command": "git push"}
    ))
    async with asyncio.timeout(2):
        while not any(frame.get("type") == "approval_request" for frame in seen):
            await asyncio.sleep(0.01)
    request_id = next(frame["request_id"] for frame in seen if frame["type"] == "approval_request")

    assert runner.cancel_current()
    answer = await asyncio.wait_for(approval, 2)
    assert answer.value == "deny"
    assert any(frame.get("type") == "request_resolved" and frame["request_id"] == request_id for frame in seen)
    assert not runner.gate_registry.resolve_approval(runner.gate_session, request_id, "allow")
    async with asyncio.timeout(2):
        while not any(frame["type"] == "cancelled" for frame in seen):
            await asyncio.sleep(0.01)
    await runner.close()


@pytest.mark.asyncio
async def test_idle_cancel_discards_queued_prompts():
    hold = asyncio.Event()
    graph = FakeGraph([RESULT], hold=hold)
    runner = _runner(graph)
    await runner.submit("first", "claude", False)
    async with asyncio.timeout(2):
        while graph.calls != ["first"]:
            await asyncio.sleep(0.01)

    # Hold the relay between turns, with the second prompt still in its queue.
    resume = asyncio.Event()

    class PausedQueue(asyncio.Queue):
        async def get(self):
            await resume.wait()
            return await super().get()

    runner.queue = PausedQueue()
    await runner.submit("queued second", "claude", False)
    hold.set()
    async with asyncio.timeout(2):
        while runner.current_turn_task is not None:
            await asyncio.sleep(0.01)

    assert runner.cancel_current() is False
    assert runner.queue.empty()
    resume.set()
    await asyncio.sleep(0)
    assert graph.calls == ["first"]
    assert not any(frame["type"] == "prompt" for frame in runner.buffer)
    await runner.close()


@pytest.mark.asyncio
async def test_a_prompt_sent_after_a_cancel_still_runs():
    """A stop must not cost the conversation its relay.

    Cancelling the turn in flight cancels a task the relay is awaiting, and
    the relay has to tell that apart from being cancelled itself — otherwise
    it ends, every later prompt is accepted and queued, and nothing ever
    answers again."""
    hold = asyncio.Event()
    graph = FakeGraph([TEXT, RESULT], hold=hold)
    runner = _runner(graph)
    seen: list[dict] = []

    async def sink(frame):
        seen.append(frame)

    runner.attach(sink)
    await runner.submit("slow", "claude", False)
    async with asyncio.timeout(2):
        while graph.calls != ["slow"]:
            await asyncio.sleep(0.01)
    assert runner.cancel_current()
    async with asyncio.timeout(2):
        while not any(frame["type"] == "cancelled" for frame in seen):
            await asyncio.sleep(0.01)

    hold.set()
    await runner.submit("after", "claude", False)
    async with asyncio.timeout(5):
        while not any(frame["type"] == "result" for frame in seen):
            await asyncio.sleep(0.01)
    assert graph.calls == ["slow", "after"]
    await runner.close()


@pytest.mark.asyncio
async def test_cancel_active_turn_keeps_queued_prompt():
    hold = asyncio.Event()
    graph = FakeGraph([RESULT], hold=hold)
    runner = _runner(graph)
    replay_at_cancel = []

    async def sink(frame):
        if frame["type"] == "cancelled":
            replay_at_cancel.extend(runner.buffer)

    runner.attach(sink)
    await runner.submit("first", "claude", False)
    async with asyncio.timeout(2):
        while graph.calls != ["first"]:
            await asyncio.sleep(0.01)
    await runner.submit("second", "claude", False)
    assert runner.cancel_current()
    hold.set()
    async with asyncio.timeout(2):
        while graph.calls != ["first", "second"] or runner.busy:
            await asyncio.sleep(0.01)
    assert graph.calls == ["first", "second"]
    assert [(frame["type"], frame["text"]) for frame in replay_at_cancel] == [
        ("prompt", "second")
    ]
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
    # started_at rides along so a client that reconnects shows when the prompt
    # was sent rather than when it arrived.
    assert {k: v for k, v in missed[0].items() if k != "started_at"} == {
        "type": "prompt", "text": "long job", "agent": "claude",
    }
    assert isinstance(missed[0]["started_at"], float)
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
async def test_answered_requests_are_not_replayed():
    runner = _runner(FakeGraph([]))
    await runner.emit({"type": "approval_request", "request_id": "a1"})
    await runner.emit({"type": "question", "request_id": "q1"})
    await runner.emit({"type": "approval_request", "request_id": "a2"})
    runner.discard_request("a1")
    runner.discard_request("q1")
    assert [frame["request_id"] for frame in runner.buffer] == ["a2"]
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

    assert not runner.sinks
    assert graph.calls == ["keep going"]
    await runner.close()


@pytest.mark.asyncio
async def test_questions_reach_every_open_tab_and_clear_after_remote_answer():
    runner = _runner(FakeGraph([]))
    first: list[dict] = []
    second: list[dict] = []

    async def first_tab(frame):
        first.append(frame)

    async def second_tab(frame):
        second.append(frame)

    runner.attach(first_tab)
    runner.attach(second_tab)
    question = {"type": "question", "request_id": "q1", "questions": [{"question": "Which?"}]}
    await runner.emit(question)
    assert first == [question]
    assert second == [question]

    await runner.request_resolved("q1", "discord:42")
    assert first[-1]["type"] == "request_resolved"
    assert second[-1]["type"] == "request_resolved"
    assert all(frame.get("request_id") != "q1" or frame["type"] != "question"
               for frame in runner.buffer)

    runner.detach(second_tab)
    await runner.emit({"type": "question", "request_id": "q2"})
    assert first[-1]["request_id"] == "q2"
    assert second[-1]["request_id"] == "q1"
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
async def test_a_close_cancelled_mid_teardown_still_drops_the_token():
    """Server shutdown or a connection torn down mid-cleanup cancels the close
    while it is waiting on a CLI to exit. The token used to be revoked only
    after that wait, so it outlived the runner."""
    stuck = asyncio.Event()

    class SlowToDie(FakeAdapter):
        async def terminate(self) -> None:
            stuck.set()
            await asyncio.Event().wait()  # never exits on its own

    runner = _runner(FakeGraph([TEXT, RESULT]), adapters={"claude": SlowToDie()})
    token = runner.gate_session.token

    closing = asyncio.create_task(runner.close())
    await stuck.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert runner.gate_registry.get(token) is None


@pytest.mark.asyncio
async def test_closing_while_a_turn_swallows_its_cancel_still_finishes():
    """close() cancels the relay while it awaits a turn, and asyncio passes that
    cancellation to the turn. A turn that catches it while finishing (storing its
    row, say) returns normally — and the relay, never told, went back to waiting
    on the queue. close() waited on the relay, so server shutdown hung."""
    finishing = asyncio.Event()

    class SwallowsCancel(FakeGraph):
        async def astream(self, state, config=None, stream_mode=None):
            self.calls.append(state["prompt"])
            yield TEXT
            try:
                finishing.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # finishing up despite the cancel, as a finally block would
            yield RESULT

    runner = _runner(SwallowsCancel([]))
    token = runner.gate_session.token
    await runner.submit("ending turn", "claude", False)
    await asyncio.wait_for(finishing.wait(), 2)

    # Timed by hand rather than with wait_for: close() suppresses the
    # CancelledError of the relay it awaits, which also swallows wait_for's own
    # timeout, so a hung close looked like a slow but successful one.
    closing = asyncio.create_task(runner.close())
    done, _ = await asyncio.wait({closing}, timeout=1)

    assert closing in done, "close() did not finish: the relay lost its cancellation"
    assert runner.relay_task.done()
    assert runner.gate_registry.get(token) is None


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


@pytest.mark.asyncio
async def test_unexpected_turn_failure_does_not_strand_next_prompt():
    class FailsOnce(FakeGraph):
        async def astream(self, state, config=None, stream_mode=None):
            self.calls.append(state["prompt"])
            if len(self.calls) == 1:
                raise RuntimeError("first turn broke")
            yield RESULT

    graph = FailsOnce([])
    runner = _runner(graph)
    seen: list[dict] = []

    async def sink(frame):
        seen.append(frame)

    runner.attach(sink)
    try:
        await runner.submit("first", "claude", False)
        await runner.submit("second", "claude", False)
        for _ in range(200):
            if len(graph.calls) == 2 and any(frame["type"] == "result" for frame in seen):
                break
            await asyncio.sleep(0.01)
        assert graph.calls == ["first", "second"]
        assert [frame["type"] for frame in seen if frame["type"] in {"error", "result"}] == [
            "error", "result"
        ]
        assert not runner.failed.is_set()
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_retrying_a_confirmed_prompt_id_does_not_run_it_twice():
    graph = FakeGraph([RESULT])
    runner = _runner(graph)
    try:
        assert await runner.submit("once", "claude", False, "browser-1")
        assert not await runner.submit("once", "claude", False, "browser-1")
        for _ in range(200):
            if graph.calls and not runner.busy:
                break
            await asyncio.sleep(0.01)
        assert graph.calls == ["once"]
    finally:
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
