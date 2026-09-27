"""The lead's routed delegation MCP contract."""
from packages.mcp.servers.delegate import TOOL_NAME, handle


def test_lists_one_routed_worker_tool():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = response["result"]["tools"][0]
    assert tool["name"] == TOOL_NAME
    assert tool["inputSchema"]["required"] == []
    assert "task_id" in tool["inputSchema"]["properties"]
    assert "worker routing" in tool["description"]


def test_rejects_missing_task_details_before_http_call():
    response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": TOOL_NAME, "arguments": {"title": ""}}})
    assert response["result"]["isError"] is True


def test_running_result_tells_lead_to_poll_same_task(monkeypatch):
    import io
    import json
    from packages.mcp.servers import delegate

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(delegate.urllib.request, "urlopen", lambda *args, **kwargs:
                        Response(json.dumps({"status": "running", "task_id": "one"}).encode()))
    result = delegate._call({"title": "작업", "prompt": "do it"})
    assert result["isError"] is False
    assert "task_id=one" in result["content"][0]["text"]
