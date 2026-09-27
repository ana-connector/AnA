"""Last-sign-of-life clocks, one per running agent.

A sub-agent used to be bounded by a wall clock: 30 minutes from the moment it
started, whatever it was doing when the clock ran out. That cut working agents
off mid-task (a scaffolding worker was three commands from committing when its
budget expired) and it was also *too patient* about the opposite case — a CLI
that died quietly still held its slot for the rest of the half hour, because
"not finished" and "not doing anything" looked identical from outside.

What actually distinguishes the two is activity: a tool call, a line of
output, a token. This module records the last one per agent so a supervisor can
ask "has this agent done anything in the last N seconds?" instead of "how long
has it been alive?".

Two details matter as much as the clock itself:

- **Waiting on a human is not idleness.** An agent parked on an approval card
  is behaving correctly; the person is the slow part. `hold()` stops the clock
  for exactly as long as that lasts, so a thinking-time budget is never spent
  on someone reading their phone.
- **Holds nest.** One agent can have several requests outstanding, so holds are
  counted rather than flagged: the clock restarts when the last one is released.
"""
import asyncio
import contextlib
import os
import time
from typing import Any, Awaitable, Callable, Optional

DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0


def idle_timeout_seconds() -> float:
    """How long a sub-agent may do nothing before it is given up on. 0 (or a
    value that will not parse) turns the watchdog off, leaving only the
    optional wall clock."""
    try:
        return max(0.0, float(os.environ.get("ANA_SUBAGENT_IDLE_TIMEOUT_SECONDS",
                                             DEFAULT_IDLE_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_SECONDS


def wall_timeout_seconds() -> float:
    """An optional ceiling on a sub-agent's total runtime. Off by default: this
    is the 30-minute cut that used to kill working agents, kept only for
    callers that deliberately want a hard bound."""
    try:
        return max(0.0, float(os.environ.get("ANA_SUBAGENT_TIMEOUT_SECONDS", 0)))
    except ValueError:
        return 0.0


class IdleTimeout(Exception):
    """A watched agent stopped doing anything for longer than its budget."""

    def __init__(self, idle_for: float):
        self.idle_for = idle_for
        super().__init__(f"{idle_for:.0f}s 동안 아무 활동이 없었다")


class WallTimeout(Exception):
    """A watched agent outlived its (optional) total runtime ceiling."""

    def __init__(self, ran_for: float):
        self.ran_for = ran_for
        super().__init__(f"{ran_for:.0f}s 안에 끝나지 않음")


class ActivityTracker:
    """Per-agent idle clocks. Process-local and not thread-safe: it is touched
    from the gate's request handlers and the turn's watchdogs, both of which
    run on the server's event loop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[str, float] = {}
        # agent id -> how many un-released holds it has.
        self._holds: dict[str, int] = {}
        # agent id -> when its current hold started, so time spent waiting for
        # a human is never charged to the agent.
        self._held_since: dict[str, float] = {}

    def start(self, key: str) -> None:
        """Begin watching an agent. Separate from `touch` so a worker that has
        not produced anything yet still has a clock to be judged by."""
        self._last[key] = self._clock()
        self._holds.pop(key, None)
        self._held_since.pop(key, None)

    def touch(self, key: Optional[str]) -> None:
        """Record a sign of life. A key that was never started is started."""
        if not key:
            return
        self._last[key] = self._clock()

    def hold(self, key: Optional[str]) -> None:
        """Stop this agent's clock: it is waiting on something outside itself
        (an approval, a question) and that wait is not its fault."""
        if not key:
            return
        count = self._holds.get(key, 0)
        self._holds[key] = count + 1
        if count == 0:
            self._held_since[key] = self._clock()

    def release(self, key: Optional[str]) -> None:
        """The wait is over. The agent is credited with the whole wait: the
        clock restarts from now rather than from where it stopped."""
        if not key:
            return
        count = self._holds.get(key, 0)
        if count <= 1:
            self._holds.pop(key, None)
            self._held_since.pop(key, None)
            self._last[key] = self._clock()
            return
        self._holds[key] = count - 1

    def idle_seconds(self, key: Optional[str]) -> float:
        """How long since this agent last did anything. Zero while it is held,
        and zero for an agent nobody is watching — an unknown key must never
        read as "idle forever" and get something killed."""
        if not key:
            return 0.0
        if self._holds.get(key):
            return 0.0
        last = self._last.get(key)
        if last is None:
            return 0.0
        return max(0.0, self._clock() - last)

    def held(self, key: Optional[str]) -> bool:
        return bool(key) and bool(self._holds.get(key))

    def forget(self, key: Optional[str]) -> None:
        if not key:
            return
        self._last.pop(key, None)
        self._holds.pop(key, None)
        self._held_since.pop(key, None)


async def supervise(
    awaitable: Awaitable[Any],
    *,
    tracker: Optional[ActivityTracker] = None,
    key: Optional[str] = None,
    idle_timeout: float = 0.0,
    wall_timeout: float = 0.0,
    poll_seconds: float = 1.0,
) -> Any:
    """Awaits `awaitable`, cancelling it if it goes quiet (or, optionally, if it
    runs past a hard ceiling).

    Raises IdleTimeout or WallTimeout after cancelling the work, so the caller
    can report *why* the sub-agent was given up on — "went quiet for 5 minutes"
    and "hit a 30-minute cap while still working" are different failures and
    used to be reported as the same one.

    With no tracker, no key, or no budgets this is a plain await: a caller that
    wants neither bound does not get a watchdog it has to reason about."""
    task = (
        awaitable
        if isinstance(awaitable, asyncio.Task)
        else asyncio.ensure_future(awaitable)
    )
    watching_idle = idle_timeout > 0 and tracker is not None and bool(key)
    if not watching_idle and wall_timeout <= 0:
        return await task

    # A budget smaller than the polling interval would otherwise only be
    # noticed a whole interval late.
    budgets = [value for value in (idle_timeout if watching_idle else 0, wall_timeout) if value > 0]
    poll = max(0.05, min([poll_seconds, *budgets]))
    started = time.monotonic()
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll)
        if done:
            # Awaited rather than .result(): a cancelled turn has to propagate
            # CancelledError from here too.
            return await task
        if watching_idle:
            idle_for = tracker.idle_seconds(key)
            if idle_for > idle_timeout:
                await _cancel(task)
                raise IdleTimeout(idle_for)
        if wall_timeout > 0 and (time.monotonic() - started) > wall_timeout:
            await _cancel(task)
            raise WallTimeout(time.monotonic() - started)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    # Awaited, not fired and forgotten: the worker's `finally` is what
    # terminates its CLI subprocess, and returning before it has run leaves a
    # CLI working (and billing) for a task nobody is waiting on.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
