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


# The lead's wait on a worker is no longer a deadline. ANA decides when a
# worker is finished or gone — it watches the worker's activity and answers as
# soon as either happens (packages/orchestrator/activity.py) — so this only has
# to outlast a long, healthy task. The old value here was 1800: a worker three
# commands from committing was reported to the lead as a timeout at the
# half-hour mark, and the lead then redid its work by hand.
_DEFAULT_TIMEOUT_SECONDS = 86400.0


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("ANA_DELEGATE_HTTP_TIMEOUT",
                                             _DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


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
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            result = json.load(response)
    except Exception as exc:  # The lead needs a tool result it can explain.
        return {"content": [{"type": "text", "text": f"worker request failed: {exc}"}], "isError": True}
    # An error carries the worker's partial output when it left any: a worker
    # ANA gave up on has usually done real work, and "failed" on its own is what
    # sent the lead off to repeat it.
    if result.get("status") == "done":
        message = result.get("output") or "Worker returned no output"
    else:
        message = "\n\n".join(
            part for part in (result.get("error"), result.get("output")) if part
        ) or "Worker returned no output"
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
