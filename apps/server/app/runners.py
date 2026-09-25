"""Turns that outlive the browser tab that started them.

A turn used to belong to a WebSocket connection: the relay task, the gate token
and both CLI subprocesses were built per connection and torn down with it. So
switching workspaces — or closing the tab, or a dropped wifi connection — killed
the work in flight, and coming back showed an empty conversation. For an
orchestrator whose turns take minutes and fan out to sub-agents, that is the
wrong lifetime: the work belongs to the *conversation*, and a browser is one
thing that happens to be watching it.

A TurnRunner is that lifetime. It owns the queue, the relay task, the gate
session and the adapters for one thread, and a connection attaches to it and
detaches without stopping anything. While nobody is attached, frames are
buffered so a client that comes back mid-turn is shown what it missed rather
than a blank screen; the buffer is cleared when a turn ends, because from then
on the turn is in the database and history covers it.

Nothing here is a cache: a runner is closed when it has been idle and unwatched
for ANA_RUNNER_IDLE_SECONDS, which is what keeps an abandoned conversation from
holding a CLI subprocess and a gate token forever.
"""
import asyncio
import contextlib
import os
import time
from typing import Awaitable, Callable, Optional

from packages.governance.session import GateRegistry, GateSession
from packages.orchestrator.timeline import TurnTimeline

Sink = Callable[[dict], Awaitable[None]]

# Frames of the turn in flight, replayed to a client that attaches mid-turn.
# Bounded: a long turn with a chatty sub-agent fan-out can produce thousands,
# and the point is to show what is happening, not to reconstruct every delta.
MAX_BUFFERED_FRAMES = 800

# A frame of these types ends a turn, and with it the replay buffer: everything
# before is in the turns table by then, and history is what shows it.
TURN_END_FRAMES = {"result", "error", "cancelled"}


def background_turns_enabled() -> bool:
    """ANA_BACKGROUND_TURNS=off restores the old lifetime — a turn dies with the
    connection that asked for it. The same escape hatch ANA_GOVERNANCE=off and
    ANA_MCP=off give their subsystems."""
    return os.environ.get("ANA_BACKGROUND_TURNS", "on").lower() != "off"


def idle_seconds() -> float:
    """How long a runner with nobody attached and nothing running is kept.

    Long enough to cover reading another conversation and coming back; short
    enough that an abandoned thread does not hold a subprocess all day."""
    try:
        return max(0.0, float(os.environ.get("ANA_RUNNER_IDLE_SECONDS", 900)))
    except ValueError:
        return 900.0


class TurnRunner:
    """One conversation's worker: a queue, a relay, and everything a turn needs.

    Created on the first connection for a thread and reused by every later one.
    `attach` points it at a browser; `detach` takes the browser away without
    stopping the work."""

    def __init__(
        self,
        *,
        thread_id: str,
        workspace_dir: str,
        workspace_name: str,
        gate_session: GateSession,
        gate_registry: GateRegistry,
        adapters: dict,
        config: dict,
        graph,
        container: bool = False,
        container_error: str = "",
    ) -> None:
        self.thread_id = thread_id
        self.workspace_dir = workspace_dir
        self.workspace_name = workspace_name
        self.gate_session = gate_session
        self.gate_registry = gate_registry
        self.adapters = adapters
        self.config = config
        self.graph = graph
        self.container = container
        self.container_error = container_error

        self.queue: "asyncio.Queue[tuple[str, str, bool]]" = asyncio.Queue()
        self.buffer: list[dict] = []
        self.sink: Optional[Sink] = None
        self.cancel_requested = asyncio.Event()
        self.failed = asyncio.Event()
        self.current_turn_task: Optional[asyncio.Task] = None
        self.current_turn_agent: Optional[str] = None
        self.closed = False
        self._idle_since: Optional[float] = time.monotonic()
        # The gate reaches the browser through the runner, not through a socket:
        # an approval asked while nobody is attached is buffered and shown when
        # someone comes back, and answering it resolves the same pending future.
        gate_session.send = self.emit
        # A request can now be answered from somewhere other than this browser
        # (packages/remote). Whichever side answers first, the card has to come
        # off the other one.
        gate_session.on_resolved = self.request_resolved
        gate_session.workspace_name = workspace_name
        self.relay_task = asyncio.create_task(self._relay(), name=f"runner:{thread_id}")

    # --- what the browser sees -------------------------------------------

    @property
    def busy(self) -> bool:
        """Whether a turn is streaming or queued right now."""
        return (
            self.current_turn_task is not None and not self.current_turn_task.done()
        ) or not self.queue.empty()

    async def emit(self, frame: dict) -> None:
        """Sends a frame to whoever is watching, and remembers it either way."""
        self.buffer.append(frame)
        if len(self.buffer) > MAX_BUFFERED_FRAMES:
            del self.buffer[: len(self.buffer) - MAX_BUFFERED_FRAMES]
        if frame.get("type") in TURN_END_FRAMES:
            # The turn is in the database now; history is what shows it, so the
            # buffer starts empty for the next one.
            self.buffer.clear()
            # The same moment is the only one worth reporting to a phone: what
            # the agent finally said, once.
            if frame.get("type") == "result":
                await self.gate_registry.notify_turn_ended(
                    self.gate_session, frame.get("text") or ""
                )
        sink = self.sink
        if sink is None:
            return
        try:
            await sink(frame)
        except Exception:  # noqa: BLE001 - the socket went away mid-send; the
            # turn keeps running, and the next client to attach gets the rest.
            if self.sink is sink:
                self.sink = None

    def attach(self, sink: Sink) -> list[dict]:
        """Points this runner at a connection and returns what it missed.

        Last attach wins: two tabs on one conversation would otherwise race to
        answer its approvals, and the one that answered second would look
        ignored."""
        self.sink = sink
        self._idle_since = None
        return list(self.buffer)

    async def request_resolved(self, request_id: str, by: str) -> None:
        """A pending request is over, whoever ended it.

        Drops the buffered card so a reconnect does not bring it back, and
        tells the browser so a card someone answered from a phone stops asking
        for a decision that is already made."""
        self.discard_request(request_id)
        await self.emit(
            {
                "type": "request_resolved",
                "text": "",
                "session_id": None,
                "is_error": False,
                "request_id": request_id,
                "by": by,
            }
        )

    def discard_request(self, request_id: str) -> None:
        """An answered card must not return when the browser reconnects."""
        self.buffer = [
            frame for frame in self.buffer
            if not (frame.get("type") in ("approval_request", "question")
                    and frame.get("request_id") == request_id)
        ]

    def detach(self, sink: Sink) -> None:
        """The connection is gone. Nothing stops."""
        if self.sink is sink:
            self.sink = None
        if not self.busy:
            self._idle_since = time.monotonic()

    def idle_for(self) -> float:
        """Seconds with nobody attached and nothing to do, or 0 while in use."""
        if self.sink is not None or self.busy:
            return 0.0
        if self._idle_since is None:
            self._idle_since = time.monotonic()
        return time.monotonic() - self._idle_since

    # --- turns ------------------------------------------------------------

    async def submit(self, prompt: str, agent: str, plan_mode: bool) -> None:
        """Queues a turn. Queued rather than run inline, so concurrent messages
        are processed in order instead of interrupting the turn in flight."""
        # Recorded, not sent: the client that typed it already shows it, and a
        # client that attaches later needs it to have something to put the
        # streaming reply under.
        # started_at travels with it so a client that was not here when the
        # prompt was sent still shows the time it was sent, not the time it
        # happened to reconnect.
        self.buffer.append(
            {"type": "prompt", "text": prompt, "agent": agent, "started_at": time.time()}
        )
        self._idle_since = None
        await self.queue.put((prompt, agent, plan_mode))

    def cancel_current(self) -> bool:
        """Stops the turn in flight, leaving the runner and its queue alone."""
        task = self.current_turn_task
        if task is None or task.done():
            return False
        self.cancel_requested.set()
        task.cancel()
        return True

    async def _run_turn(self, prompt: str, agent: str, plan_mode: bool) -> None:
        # Session id continuity across turns comes from the checkpointed graph
        # state, not from anything held here — which is also why a turn can
        # outlive the connection that asked for it.
        timeline = TurnTimeline()
        self.gate_session.timeline = timeline
        self.gate_session.native_agents.clear()
        self.config["configurable"]["timeline"] = timeline
        self.config["configurable"]["native_agents"] = self.gate_session.native_agents
        try:
            snapshot = await self.graph.aget_state(self.config)
            values = snapshot.values or {}
            async for event in self.graph.astream(
                {
                    "prompt": prompt,
                    "agent": agent,
                    "plan_mode": plan_mode,
                    "session_id": values.get("session_id"),
                    "session_ids": values.get("session_ids") or {},
                    "last_agent": values.get("last_agent"),
                    "response_text": "",
                },
                config=self.config,
                stream_mode="custom",
            ):
                await self.emit(event)
        finally:
            self.gate_session.timeline = None
            self.config["configurable"].pop("timeline", None)
            self.config["configurable"].pop("native_agents", None)

    async def _relay(self) -> None:
        try:
            while True:
                prompt, agent, plan_mode = await self.queue.get()
                # Each turn is its own task so a cancel can stop that one turn
                # without killing the relay.
                self.current_turn_task = asyncio.create_task(
                    self._run_turn(prompt, agent, plan_mode)
                )
                self.current_turn_agent = agent
                # So an audit row names the CLI that actually made the call.
                self.gate_session.current_agent = agent
                try:
                    await self.current_turn_task
                except asyncio.CancelledError:
                    if not self.cancel_requested.is_set():
                        raise  # the runner itself is being closed
                    self.cancel_requested.clear()
                    await self.emit(
                        {
                            "type": "cancelled",
                            "text": "",
                            "session_id": None,
                            "is_error": False,
                            "agent": agent,
                        }
                    )
                finally:
                    if self.current_turn_task is not None and not self.current_turn_task.done():
                        self.current_turn_task.cancel()
                    self.current_turn_task = None
                    self.current_turn_agent = None
                    if not self.busy and self.sink is None:
                        self._idle_since = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one turn's failure must not
            # leave the conversation without a relay; the frame says what broke
            # and the runner is closed by whoever owns it.
            with contextlib.suppress(Exception):
                await self.emit(
                    {
                        "type": "error",
                        "text": str(exc),
                        "session_id": None,
                        "is_error": True,
                    }
                )
            self.failed.set()

    # --- teardown ---------------------------------------------------------

    async def close(self) -> None:
        """Stops the relay, kills every CLI subprocess, drops the gate token."""
        if self.closed:
            return
        self.closed = True
        self.sink = None
        self.relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.relay_task
        for adapter in list(self.adapters.values()):
            with contextlib.suppress(Exception):
                await adapter.terminate()
        self.gate_registry.unregister(self.gate_session.token)


class RunnerRegistry:
    """The live runners, one per thread, plus the sweep that retires idle ones."""

    def __init__(self) -> None:
        self._runners: dict[str, TurnRunner] = {}
        self._sweeper: Optional[asyncio.Task] = None

    def get(self, thread_id: str) -> Optional[TurnRunner]:
        runner = self._runners.get(thread_id)
        if runner is not None and (runner.closed or runner.failed.is_set()):
            # A runner whose relay died is not reusable: dropping it here means
            # the next connection builds a fresh one instead of attaching to
            # something that will never answer.
            self._runners.pop(thread_id, None)
            return None
        return runner

    def put(self, runner: TurnRunner) -> None:
        self._runners[runner.thread_id] = runner

    async def drop(self, thread_id: str) -> None:
        runner = self._runners.pop(thread_id, None)
        if runner is not None:
            await runner.close()

    async def close_all(self) -> None:
        for thread_id in list(self._runners):
            await self.drop(thread_id)
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    def start_sweeper(self, interval: float = 60.0) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep(interval), name="runner-sweeper")

    async def _sweep(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            limit = idle_seconds()
            for thread_id, runner in list(self._runners.items()):
                if runner.idle_for() >= limit:
                    await self.drop(thread_id)
