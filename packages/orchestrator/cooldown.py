"""Rungs that just said no, remembered for a while.

The failover ladder asks each rung in turn and moves on when one refuses. What
it did not do is remember: a provider that answered "you have reached your
5-hour usage limit" was asked again by the next task a few minutes later, said
the same thing, and was asked again after that. Each of those refusals costs a
worker launch, a prompt, and — before the idle watchdog existed — up to half an
hour of the lead's time.

A refusal about *capacity* (a spent window, a missing credential) is about the
account, not the task, so it applies to every task until something changes. That
is what this records. Deliberately short-lived and per process: a window really
does reset, a key really does get fixed, and the way to find out is to try
again — just not immediately, and not once per task.
"""
import os
import time
from typing import Callable, Optional

DEFAULT_COOLDOWN_SECONDS = 900.0


def cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("ANA_RUNG_COOLDOWN_SECONDS",
                                             DEFAULT_COOLDOWN_SECONDS)))
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS


class Cooldowns:
    """Which rungs are resting, and why."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}
        self._reasons: dict[str, str] = {}

    def trip(self, label: str, reason: str = "", seconds: Optional[float] = None) -> None:
        if not label:
            return
        wait = cooldown_seconds() if seconds is None else seconds
        if wait <= 0:
            return
        self._until[label] = self._clock() + wait
        self._reasons[label] = reason

    def resting(self, label: str) -> bool:
        until = self._until.get(label)
        if until is None:
            return False
        if self._clock() >= until:
            # Expired: forgotten rather than kept, so a recovered window is
            # simply used again.
            self._until.pop(label, None)
            self._reasons.pop(label, None)
            return False
        return True

    def reason(self, label: str) -> str:
        return self._reasons.get(label, "")

    def clear(self, label: Optional[str] = None) -> None:
        if label is None:
            self._until.clear()
            self._reasons.clear()
            return
        self._until.pop(label, None)
        self._reasons.pop(label, None)


# One per server process. Workers are launched from several places (the plan
# fan-out, the lead's delegate tool) and a limit one of them hit is a limit for
# all of them.
_SHARED = Cooldowns()


def shared() -> Cooldowns:
    return _SHARED


def label_for(agent: str, model: Optional[str] = None) -> str:
    return f"{agent}/{model}" if model else agent


def usable(rungs: list, *, cooldowns: Optional[Cooldowns] = None) -> list:
    """The rungs worth trying, in order, with the resting ones dropped.

    Never returns nothing: if every rung is resting, the whole ladder is offered
    anyway. A cooldown is an optimization, not a reason to tell the user ANA has
    no workers left."""
    registry = cooldowns or _SHARED
    open_rungs = [
        rung for rung in rungs
        if not registry.resting(label_for(rung.agent, getattr(rung, "model", None)))
    ]
    return open_rungs or list(rungs)
