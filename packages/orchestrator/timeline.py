"""Ordered text and tool blocks for one lead turn's saved transcript."""


class TurnTimeline:
    def __init__(self) -> None:
        self.blocks: list[dict] = []

    def append_text(self, text: str) -> None:
        if not text:
            return
        if self.blocks and self.blocks[-1]["kind"] == "text":
            self.blocks[-1]["text"] += text
        else:
            self.blocks.append({"kind": "text", "text": text})

    def append_tool(self, tool: dict) -> None:
        call_id = tool.get("tool_use_id")
        if call_id:
            for block in self.blocks:
                if block["kind"] == "tool" and block["tool"].get("tool_use_id") == call_id:
                    block["tool"] = tool
                    return
        self.blocks.append({"kind": "tool", "tool": tool})
