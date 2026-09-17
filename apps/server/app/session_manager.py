import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


@dataclass
class ChatSession:
    """One browser connection's lead-agent conversation. Owns the turn
    queue so concurrent user messages are processed in order instead of
    interrupting an in-flight turn (see roadmap doc, '동시 메시지 처리')."""

    adapter: AgentAdapter
    transcript: TranscriptLogger
    session_id: Optional[str] = None
    _queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)

    async def send(self, prompt: str) -> None:
        await self._queue.put(prompt)

    async def run(self) -> AsyncIterator[AdapterEvent]:
        """Consumes queued prompts forever, yielding every AdapterEvent
        from each turn. The caller (websocket handler) drives this loop."""
        while True:
            prompt = await self._queue.get()
            response_text = ""
            cost_usd = None
            async for event in self.adapter.run_stream(prompt, session_id=self.session_id):
                if event.session_id:
                    self.session_id = event.session_id
                if event.type == "text":
                    response_text += event.text
                if event.type == "result":
                    cost_usd = event.cost_usd
                    # Log the completed turn as soon as the result event
                    # arrives, rather than after the async-for loop exits:
                    # the latter needs one more __anext__() resume than a
                    # caller that stops pulling once the turn's events are
                    # exhausted would ever make.
                    self.transcript.append(
                        session_id=self.session_id or "",
                        prompt=prompt,
                        response=response_text,
                        cost_usd=cost_usd,
                    )
                yield event

    async def close(self) -> None:
        await self.adapter.terminate()
