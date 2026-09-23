from packages.orchestrator.timeline import TurnTimeline


def test_text_tool_text_order_and_tool_decision_update():
    timeline = TurnTimeline()
    timeline.append_text("before ")
    timeline.append_text("tool")
    timeline.append_tool({"tool_use_id": "call-1", "verdict": "pending"})
    timeline.append_text("after")
    timeline.append_tool({"tool_use_id": "call-1", "verdict": "allow"})

    assert timeline.blocks == [
        {"kind": "text", "text": "before tool"},
        {"kind": "tool", "tool": {"tool_use_id": "call-1", "verdict": "allow"}},
        {"kind": "text", "text": "after"},
    ]
