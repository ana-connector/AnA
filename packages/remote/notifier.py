"""What the gate registry talks to when it wants someone other than the browser.

Kept as a protocol with a do-nothing default so the registry has exactly one
code path: without a bot token configured, ANA behaves as it did before, and
nothing in the gate has to ask whether a remote channel exists.
"""
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class RemoteNotifier(Protocol):
    """A second place a pending request can be shown and answered.

    Every method is best-effort. The registry swallows whatever is raised here:
    a channel being unreachable must never change a verdict, because the
    failure mode of "approval denied because Discord was down" is an agent that
    stops working for a reason nobody can see."""

    async def approval_posted(self, request_id: str, session, request: dict) -> None: ...

    async def question_posted(self, request_id: str, session, request: dict) -> None: ...

    async def request_closed(self, request_id: str, session, outcome: str, by: str) -> None: ...

    async def turn_ended(self, session, text: str) -> None: ...


class NullNotifier:
    """The default: nobody else is listening."""

    async def approval_posted(self, request_id: str, session, request: dict) -> None:
        return None

    async def question_posted(self, request_id: str, session, request: dict) -> None:
        return None

    async def request_closed(self, request_id: str, session, outcome: str, by: str) -> None:
        return None

    async def turn_ended(self, session, text: str) -> None:
        return None
