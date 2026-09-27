"""Ordered text and tool blocks for one turn's saved transcript.

The same shape carries a lead turn and a sub-agent's own record: text the agent
wrote, tool calls the gate screened, and the notices ANA itself inserted, in the
order they happened. Sub-agents use the functions directly (their blocks live on
the task card the browser already receives); the lead turn uses the class, which
is handed around as `configurable["timeline"]`.
"""


def append_text(blocks: list[dict], text: str) -> None:
    """Appends narration, merging into the previous text block: a streamed reply
    arrives in dozens of fragments and each one is not a paragraph."""
    if not text:
        return
    if blocks and blocks[-1]["kind"] == "text":
        blocks[-1]["text"] += text
    else:
        blocks.append({"kind": "text", "text": text})


def append_tool(blocks: list[dict], tool: dict) -> None:
    """Appends a tool call, or updates the one already recorded for this id: a
    call that needed approval is reported twice (pending, then the decision) and
    must not appear as two commands."""
    call_id = tool.get("tool_use_id")
    if call_id:
        for block in blocks:
            if block["kind"] == "tool" and block["tool"].get("tool_use_id") == call_id:
                block["tool"] = tool
                return
    blocks.append({"kind": "tool", "tool": tool})


def append_notice(blocks: list[dict], text: str) -> None:
    """Appends something ANA said about the turn rather than something an agent
    said in it — a failover, a handoff to another CLI.

    Recorded in the transcript instead of only being sent to the open tab, which
    is why the divider used to vanish the moment you opened another conversation
    and came back: it existed nowhere but in that tab's memory."""
    if not text:
        return
    if blocks and blocks[-1]["kind"] == "notice" and blocks[-1]["text"] == text:
        return  # a resend of the same notice is not a second switch
    blocks.append({"kind": "notice", "text": text})


class TurnTimeline:
    def __init__(self) -> None:
        self.blocks: list[dict] = []

    def append_text(self, text: str) -> None:
        append_text(self.blocks, text)

    def append_tool(self, tool: dict) -> None:
        append_tool(self.blocks, tool)

    def append_notice(self, text: str) -> None:
        append_notice(self.blocks, text)
