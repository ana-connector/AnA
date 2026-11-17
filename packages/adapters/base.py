from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class AdapterEvent:
    """One structured event out of an agent CLI turn."""

    type: str  # "text" | "reasoning" | "model" | "tool" | "subagent" | "result" | "usage" | "error"
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


# What a CLI's own tool name is called in ANA's records. The gate's rows are
# written with the Claude CLI's names, which the rule set, the audit log and
# the UI labels all already speak, so a CLI that calls the same thing `shell`
# or `bash` is recorded as `Bash`. Nothing here invents a name: a tool with no
# counterpart keeps its own, capitalized.
CLI_TOOL_NAMES = {
    "bash": "Bash",
    "shell": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "apply_patch": "Edit",
    "patch": "Edit",
    "delete": "Delete",
    "grep": "Grep",
    "glob": "Glob",
    "list": "LS",
    "ls": "LS",
    "task": "Task",
    "webfetch": "WebFetch",
    "webSearch": "WebSearch",
    "websearch": "WebSearch",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
}


def cli_tool_name(name: str) -> str:
    """The recorded name for a tool the CLI ran itself."""
    if not name:
        return "Tool"
    mapped = CLI_TOOL_NAMES.get(name) or CLI_TOOL_NAMES.get(name.lower())
    return mapped or (name[0].upper() + name[1:])


def cli_tool_record(agent: str, name: str, tool_input: dict, call_id: Optional[str]) -> dict:
    """One tool call a CLI ran on its own, in the shape the gate records.

    Cursor and OpenCode have no PreToolUse hook, so their calls never reach
    ANA's gate: nothing screened them, and nothing recorded them either --
    their file edits and shell commands were simply invisible, in the live
    transcript and in the saved one. These records restore the second half.

    `decided_by` says `cli:<agent>` rather than `rule`, because that is the
    truth: the CLI approved its own call. Reading one of these as "ANA allowed
    this" would be the one wrong conclusion to draw, and the dialog shows this
    field next to the verdict."""
    return {
        "tool_use_id": call_id or None,
        "tool_name": cli_tool_name(name),
        "input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
        # The call happened: the CLI ran it with ANA's blanket-approval flag
        # on. It is not a gate decision, which is what `decided_by` carries.
        "verdict": "allow",
        "decided_by": f"cli:{agent}",
        "rule_id": None,
        "category": None,
        "agent_id": None,
    }


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
