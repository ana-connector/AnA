"""MCP tool that lets a lead run one task through ANA's worker ladder."""
import json
import os
import sys
import urllib.request

TOOL_NAME = "run_task"
TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Run one delegated task with ANA's worker routing. Use this for tasks "
        "that need a worker-only MCP server, or whenever the worker model "
        "ladder should choose the sub-agent. Wait for the result and answer "
        "the user in Korean. Write a short Korean title and a self-contained "
        "English prompt."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short task title in Korean"},
            "prompt": {"type": "string", "description": "Self-contained worker instruction"},
        },
        "required": ["title", "prompt"],
    },
}


def _call(arguments: dict) -> dict:
    title = str(arguments.get("title") or "").strip()
    prompt = str(arguments.get("prompt") or "").strip()
    if not title or not prompt:
        return {"content": [{"type": "text", "text": "title and prompt are required"}], "isError": True}
    request = urllib.request.Request(
        (os.environ.get("ANA_GATE_URL") or "http://127.0.0.1:8000").rstrip("/")
        + "/gate/delegate",
        data=json.dumps({"title": title, "prompt": prompt}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-ana-gate-token": os.environ.get("ANA_GATE_TOKEN", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            result = json.load(response)
    except Exception as exc:  # The lead needs a tool result it can explain.
        return {"content": [{"type": "text", "text": f"worker request failed: {exc}"}], "isError": True}
    message = result.get("output") or result.get("error") or "Worker returned no output"
    return {"content": [{"type": "text", "text": message}], "isError": result.get("status") != "done"}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    message_id = message.get("id")
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": message_id, "result": {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ana-delegate", "version": "1.0.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL_NAME:
            return {"jsonrpc": "2.0", "id": message_id,
                    "error": {"code": -32602, "message": "unknown tool"}}
        return {"jsonrpc": "2.0", "id": message_id,
                "result": _call(params.get("arguments") or {})}
    return {"jsonrpc": "2.0", "id": message_id,
            "error": {"code": -32601, "message": "unknown method"}}


def main() -> None:
    for line in sys.stdin:
        try:
            result = handle(json.loads(line))
            if result is not None:
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:  # Keep the stdio server alive after bad input.
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32603, "message": str(exc)}}), flush=True)


if __name__ == "__main__":
    main()
