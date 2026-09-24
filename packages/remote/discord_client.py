"""The half of the Discord bridge that touches the network.

No SDK, for the same reason `packages/mcp/servers/ask_user.py` writes MCP by
hand: `httpx` and `websockets` are already pinned, and the part of Discord that
matters here is four REST calls and a socket that has to identify, heartbeat
and resume. Adding a dependency with its own event loop conventions to the
server image is the larger change.

The gateway is dialled *out* from ANA. That is the whole reason this design
needs no public inbound, no tunnel and no authentication system: button clicks
arrive back down the socket ANA opened.
"""
import asyncio
import json
import logging
import random
from typing import Awaitable, Callable, Optional

import httpx
import websockets

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway opcodes.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Interactions are delivered to a bot whatever its intents are, and ANA reads
# no message content, so it asks for nothing.
INTENTS = 0


class DiscordAPI:
    """The REST calls the bridge makes: post a message, edit it, answer a click."""

    def __init__(self, token: str, client: Optional[httpx.AsyncClient] = None):
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=15.0)

    @property
    def _headers(self) -> dict:
        return {
            "authorization": f"Bot {self._token}",
            "content-type": "application/json",
            "user-agent": "ANA (https://github.com/, 1.0)",
        }

    async def _request(self, method: str, path: str, payload: Optional[dict] = None):
        for attempt in range(3):
            response = await self._client.request(
                method, f"{API_BASE}{path}", headers=self._headers, json=payload
            )
            if response.status_code == 429:
                # Discord's own back-pressure. Told, not guessed.
                retry_after = float((response.json() or {}).get("retry_after", 1.0))
                await asyncio.sleep(min(retry_after, 10.0))
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"discord rate limited: {method} {path}")

    async def create_message(self, channel_id: str, payload: dict) -> str:
        response = await self._request("POST", f"/channels/{channel_id}/messages", payload)
        return str((response.json() or {}).get("id") or "")

    async def edit_message(self, channel_id: str, message_id: str, payload: dict) -> None:
        await self._request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)

    async def respond(self, interaction_id: str, token: str, payload: dict) -> None:
        """An interaction has to be answered within three seconds, so this is
        deliberately the only thing between a click and a reply."""
        await self._request(
            "POST", f"/interactions/{interaction_id}/{token}/callback", payload
        )

    async def close(self) -> None:
        await self._client.aclose()


def identify_payload(token: str) -> dict:
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": INTENTS,
            "properties": {"os": "linux", "browser": "ana", "device": "ana"},
        },
    }


def resume_payload(token: str, session_id: str, sequence: Optional[int]) -> dict:
    return {"op": OP_RESUME, "d": {"token": token, "session_id": session_id, "seq": sequence}}


def heartbeat_payload(sequence: Optional[int]) -> dict:
    return {"op": OP_HEARTBEAT, "d": sequence}


class DiscordGateway:
    """One outbound socket, kept alive.

    Everything it learns -- the heartbeat interval, the session to resume, how
    far the event stream got -- comes from Discord; nothing here is on a timer
    of its own except the reconnect backoff."""

    def __init__(self, token: str, on_interaction: Callable[[dict], Awaitable[None]]):
        self._token = token
        self._on_interaction = on_interaction
        self._sequence: Optional[int] = None
        self._session_id: str = ""
        self._resume_url: str = ""
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect, and keep connecting. Returns only when stopped."""
        backoff = 1.0
        while not self._stop.is_set():
            resuming = bool(self._session_id and self._resume_url)
            url = self._resume_url if resuming else GATEWAY_URL
            try:
                async with websockets.connect(url, max_size=2**22) as socket:
                    await self._session(socket, resuming)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dropped socket is normal
                logger.warning("discord gateway disconnected", exc_info=True)
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 60.0)

    async def _session(self, socket, resuming: bool) -> None:
        hello = json.loads(await socket.recv())
        interval = float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0

        if resuming:
            await socket.send(json.dumps(resume_payload(self._token, self._session_id, self._sequence)))
        else:
            await socket.send(json.dumps(identify_payload(self._token)))

        heartbeat = asyncio.create_task(self._heartbeat(socket, interval))
        try:
            async for raw in socket:
                if self._stop.is_set():
                    break
                await self.handle_frame(json.loads(raw))
        finally:
            heartbeat.cancel()
            with_suppressed = asyncio.gather(heartbeat, return_exceptions=True)
            await with_suppressed

    async def _heartbeat(self, socket, interval: float) -> None:
        # The first beat is jittered, as Discord asks, so a fleet of bots does
        # not beat in lockstep.
        await asyncio.sleep(interval * random.random())
        while True:
            await socket.send(json.dumps(heartbeat_payload(self._sequence)))
            await asyncio.sleep(interval)

    async def handle_frame(self, frame: dict) -> None:
        """One gateway frame. Public because it is where the protocol is
        decided, and that is worth testing without a socket."""
        op = frame.get("op")
        if frame.get("s") is not None:
            self._sequence = frame["s"]

        if op == OP_INVALID_SESSION:
            # Cannot resume from here; the next connect starts fresh.
            self._session_id = ""
            self._resume_url = ""
            raise ConnectionError("discord invalidated the session")
        if op == OP_RECONNECT:
            raise ConnectionError("discord asked us to reconnect")
        if op != OP_DISPATCH:
            return

        event = frame.get("t")
        data = frame.get("d") or {}
        if event == "READY":
            self._session_id = str(data.get("session_id") or "")
            self._resume_url = (
                f"{data.get('resume_gateway_url') or ''}/?v=10&encoding=json"
                if data.get("resume_gateway_url")
                else ""
            )
            logger.info("discord gateway ready")
        elif event == "INTERACTION_CREATE":
            await self._on_interaction(data)
