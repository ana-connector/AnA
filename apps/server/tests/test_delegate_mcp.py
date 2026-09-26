"""The lead's routed delegation MCP contract."""
from packages.mcp.servers.delegate import TOOL_NAME, handle


def test_lists_one_routed_worker_tool():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = response["result"]["tools"][0]
    assert tool["name"] == TOOL_NAME
    assert tool["inputSchema"]["required"] == ["title", "prompt"]
    assert "worker routing" in tool["description"]


def test_rejects_missing_task_details_before_http_call():
    response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": TOOL_NAME, "arguments": {"title": ""}}})
    assert response["result"]["isError"] is True
