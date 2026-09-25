"""Gate session registry and the approval broker.

A hook runs inside the CLI subprocess and has to prove which conversation it
belongs to, so each WebSocket connection registers a random token that maps
back to its thread and to the callbacks used to ask that browser for
approval. An unknown token is refused, which keeps a stray process on the
machine from driving approvals for a thread it has nothing to do with.

The registry is process-local by design: a token is only meaningful while the
connection that created it is open, and it must not outlive it.
"""
import asyncio
import contextlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from packages.governance.subagent_scope import Scope
from packages.orchestrator.timeline import TurnTimeline
from packages.remote.notifier import NullNotifier, RemoteNotifier

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
# A question waits far longer than an approval: an approval interrupts someone
# who is watching the agent work, while a question can arrive when they have
# walked away. Long enough to be worth asking, bounded so a turn cannot hang
# forever.
DEFAULT_QUESTION_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class Answer:
    """What came back, and who sent it.

    `by` is what the audit log records as `decided_by`: "user" for the browser,
    "discord:<user id>" for a phone. It matters because a button that runs a
    command the rule set stopped is worth attributing to a person."""

    value: Any
    by: str = ""


@dataclass
class _Pending:
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop

    def resolve(self, value: object) -> None:
        if self.future.done():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.future.set_result(value)
        else:
            self.loop.call_soon_threadsafe(self._set, value)

    def _set(self, value: object) -> None:
        if not self.future.done():
            self.future.set_result(value)

    def fail(self, error: BaseException) -> None:
        if self.future.done():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.future.set_exception(error)
        else:
            self.loop.call_soon_threadsafe(self._fail, error)

    def _fail(self, error: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(error)


@dataclass
class GateSession:
    token: str
    thread_id: str
    # Sends one approval request to the browser; awaited by request_approval.
    send: Callable[[dict], Awaitable[None]]
    # Set by the planner only while the lead executes a tool-using turn.
    lead_scope: Optional[Scope] = None
    workspace_prompt: str = ""
    # Which agent is running right now, so the audit row names the right CLI.
    # Updated per turn by the websocket handler.
    current_agent: Optional[str] = None
    # This conversation's own workspace directory. The gate refuses calls that
    # reach outside it (see packages/governance/workspace.py); None disables
    # that check, which is what a caller that never set a workspace gets.
    workspace: Optional[str] = None
    # Which model each CLI is actually running, learned from hook payloads.
    models: dict[str, str] = field(default_factory=dict)
    # request_id -> the future the gate request is waiting on, plus the loop
    # it belongs to. The loop is kept because the answer does not always
    # arrive on it: asyncio futures are not thread-safe, and resolving one
    # from another thread leaves the waiter parked until its timeout.
    pending: dict[str, "_Pending"] = field(default_factory=dict)
    timeline: Optional[TurnTimeline] = None
    native_agents: dict[str, dict] = field(default_factory=dict)
    # Called when a request this session raised is over, whoever ended it.
    # TurnRunner binds it to "drop the buffered card and tell the browser", so
    # a question answered from a phone stops being offered in the tab.
    on_resolved: Optional[Callable[[str, str], Awaitable[None]]] = None
    # The workspace's display name, for a channel that carries every
    # conversation and has to say which one this is.
    workspace_name: str = ""


class GateRegistry:
    """Tokens → open connections. One instance lives on app.state."""

    def __init__(
        self,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        question_timeout: float = DEFAULT_QUESTION_TIMEOUT_SECONDS,
        notifier: Optional[RemoteNotifier] = None,
    ):
        self._sessions: dict[str, GateSession] = {}
        # request_id -> the session waiting on it. The browser answers through
        # its own connection and never needs this; a Discord interaction only
        # carries what was packed into the button, so this is how an answer
        # from anywhere else finds the future it belongs to.
        self._requests: dict[str, GateSession] = {}
        self.approval_timeout = approval_timeout
        self.question_timeout = question_timeout
        self.notifier: RemoteNotifier = notifier or NullNotifier()

    def register(
        self,
        thread_id: str,
        send: Callable[[dict], Awaitable[None]],
        workspace: Optional[str] = None,
    ) -> GateSession:
        token = secrets.token_urlsafe(32)
        session = GateSession(
            token=token, thread_id=thread_id, send=send, workspace=workspace
        )
        self._sessions[token] = session
        return session

    def unregister(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session is None:
            return
        # Anything still waiting on this connection can never be answered now;
        # failing those futures makes the gate deny rather than hang until the
        # hook's own timeout.
        for request_id, pending in session.pending.items():
            self._requests.pop(request_id, None)
            pending.fail(ConnectionError("gate session closed"))

    def get(self, token: str) -> Optional[GateSession]:
        return self._sessions.get(token)

    async def _notify(self, what: str, *args) -> None:
        """Best-effort call into the remote channel.

        Swallowed on purpose: Discord being unreachable must not decide a
        verdict, and the browser is still being asked either way."""
        try:
            await getattr(self.notifier, what)(*args)
        except Exception:  # noqa: BLE001 - a remote channel is never load-bearing
            logger.warning("remote notifier %s failed", what, exc_info=True)

    async def notify_turn_ended(self, session: GateSession, text: str) -> None:
        """What the agent finally said, for a channel that is not watching the
        stream. Best-effort, like every other call into a remote channel."""
        await self._notify("turn_ended", session, text)

    async def _close_request(
        self, session: GateSession, request_id: str, outcome: str, by: str
    ) -> None:
        """One place where a finished request is taken down everywhere.

        Runs whatever ended it -- an answer from either side, a timeout, a
        closed connection -- so a card is never left offering a decision that
        has already been made."""
        session.pending.pop(request_id, None)
        self._requests.pop(request_id, None)
        await self._notify("request_closed", request_id, session, outcome, by)
        if session.on_resolved is not None:
            with contextlib.suppress(Exception):
                await session.on_resolved(request_id, by)

    async def request_approval(self, session: GateSession, request: dict) -> Answer:
        """Asks everyone who can answer, and waits for whoever gets there first.

        The value is "allow" or "deny"; a timeout, a closed connection or a
        failed send all deny."""
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        session.pending[request_id] = _Pending(future=future, loop=loop)
        self._requests[request_id] = session
        answer = Answer(value="deny", by="")
        try:
            await session.send({**request, "type": "approval_request", "request_id": request_id})
            await self._notify(
                "approval_posted", request_id, session, {**request, "request_id": request_id}
            )
            answer = await asyncio.wait_for(future, timeout=self.approval_timeout)
            return answer
        except asyncio.TimeoutError:
            answer = Answer(value="timeout", by="timeout")
            return answer
        except Exception:  # noqa: BLE001 - send failed or connection closed
            answer = Answer(value="deny", by="closed")
            return answer
        finally:
            outcome = "closed" if answer.by == "closed" else str(answer.value)
            await self._close_request(session, request_id, outcome, answer.by)

    async def request_answer(self, session: GateSession, request: dict) -> Answer:
        """Asks the browser a question and waits for the answer.

        The same machinery as request_approval — one pending future per
        request, resolved by the client frame that answers it — because it is
        the same problem: something inside a CLI subprocess needs a human, and
        the only way to that human is the connection that started the turn.

        The value is the answers, or None when nobody answered in time. None is
        a result, not a failure: the caller tells the agent to state its
        assumption and carry on rather than losing the turn."""
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        session.pending[request_id] = _Pending(future=future, loop=loop)
        self._requests[request_id] = session
        answer = Answer(value=None, by="")
        try:
            await session.send({**request, "type": "question", "request_id": request_id})
            await self._notify(
                "question_posted", request_id, session, {**request, "request_id": request_id}
            )
            result = await asyncio.wait_for(future, timeout=self.question_timeout)
            answer = result if isinstance(result.value, list) else Answer(value=None, by=result.by)
            return answer
        except asyncio.TimeoutError:
            answer = Answer(value=None, by="timeout")
            return answer
        except Exception:  # noqa: BLE001 - send failed or connection closed
            answer = Answer(value=None, by="closed")
            return answer
        finally:
            outcome = (
                "answered" if isinstance(answer.value, list)
                else ("closed" if answer.by == "closed" else "timeout")
            )
            await self._close_request(session, request_id, outcome, answer.by)

    def resolve_answer(
        self, session: GateSession, request_id: str, answers: list, by: str = "user"
    ) -> bool:
        """Called when the browser answers a question. False for an unknown or
        already-answered request, so a double submit is a no-op."""
        pending = session.pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.resolve(Answer(value=answers, by=by))
        return True

    def resolve_approval(
        self, session: GateSession, request_id: str, decision: str, by: str = "user"
    ) -> bool:
        """Called when the browser answers. Returns False for an unknown or
        already-answered request, so a duplicate click is a no-op."""
        pending = session.pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.resolve(Answer(value="allow" if decision == "allow" else "deny", by=by))
        return True

    # --- answers that arrive from somewhere other than the browser ------------

    def session_for_request(self, request_id: str) -> Optional[GateSession]:
        """Which conversation a pending request belongs to, by its id alone."""
        return self._requests.get(request_id)

    def resolve_remote_approval(self, request_id: str, decision: str, by: str) -> bool:
        """Answers an approval from a channel that has no gate token: a Discord
        button carries the request id and nothing else."""
        session = self._requests.get(request_id)
        if session is None:
            return False
        return self.resolve_approval(session, request_id, decision, by=by)

    def resolve_remote_answer(self, request_id: str, answers: list, by: str) -> bool:
        """The same for a questionnaire."""
        session = self._requests.get(request_id)
        if session is None:
            return False
        return self.resolve_answer(session, request_id, answers, by=by)
