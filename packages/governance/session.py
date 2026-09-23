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
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from packages.orchestrator.timeline import TurnTimeline

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0


@dataclass
class GateSession:
    token: str
    thread_id: str
    # Sends one approval request to the browser; awaited by request_approval.
    send: Callable[[dict], Awaitable[None]]
    # Which agent is running right now, so the audit row names the right CLI.
    # Updated per turn by the websocket handler.
    current_agent: Optional[str] = None
    # This conversation's own workspace directory. The gate refuses calls that
    # reach outside it (see packages/governance/workspace.py); None disables
    # that check, which is what a caller that never set a workspace gets.
    workspace: Optional[str] = None
    # Which model each CLI is actually running, learned from hook payloads.
    models: dict[str, str] = field(default_factory=dict)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    timeline: Optional[TurnTimeline] = None
    native_agents: dict[str, dict] = field(default_factory=dict)


class GateRegistry:
    """Tokens → open connections. One instance lives on app.state."""

    def __init__(self, approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS):
        self._sessions: dict[str, GateSession] = {}
        self.approval_timeout = approval_timeout

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
        for future in session.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("gate session closed"))

    def get(self, token: str) -> Optional[GateSession]:
        return self._sessions.get(token)

    async def request_approval(self, session: GateSession, request: dict) -> str:
        """Asks the browser and waits. Returns "allow" or "deny"; a timeout,
        a closed connection or a failed send all deny."""
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        session.pending[request_id] = future
        try:
            await session.send({**request, "type": "approval_request", "request_id": request_id})
            return await asyncio.wait_for(future, timeout=self.approval_timeout)
        except asyncio.TimeoutError:
            return "timeout"
        except Exception:  # noqa: BLE001 - send failed or connection closed
            return "deny"
        finally:
            session.pending.pop(request_id, None)

    def resolve_approval(self, session: GateSession, request_id: str, decision: str) -> bool:
        """Called when the browser answers. Returns False for an unknown or
        already-answered request, so a duplicate click is a no-op."""
        future = session.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result("allow" if decision == "allow" else "deny")
        return True
