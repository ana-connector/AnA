from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class AdapterEvent:
    """One structured event out of an agent CLI turn."""

    type: str  # "text" | "result" | "usage" | "error"
    text: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    is_error: bool = False
    raw: Optional[dict] = None


class AgentAdapter(ABC):
    """Common interface every CLI-backed agent adapter must implement."""

    @abstractmethod
    def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        """Run one turn, yielding AdapterEvents as the CLI streams output."""
        raise NotImplementedError

    @abstractmethod
    async def terminate(self) -> None:
        """Kill any in-flight subprocess for this adapter instance."""
        raise NotImplementedError
