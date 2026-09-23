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
    # Token counts as the CLI reported them, normalized only in key names
    # each CLI already uses ("input_tokens"/"output_tokens"). Kept separate
    # from cost_usd because the two CLIs report different things: the Claude
    # CLI gives a dollar figure, the Codex CLI gives tokens only.
    usage: Optional[dict] = None
    # The model the CLI actually ran ("claude-fable-5-1", "gpt-6-astra", ...).
    # Shown in the UI instead of the CLI's name: which product wrapped the call
    # matters less than which model answered.
    model: Optional[str] = None


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
