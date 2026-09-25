"""ANA's own MCP server: the agent scheduling work for later.

The counterpart of `ask_user`. That one lets the agent reach the person; this
one lets it reach a clock — "check the deploy every morning", "run the test
suite nightly" — without a person having to translate that into a cron entry.

Everything it creates belongs to the conversation the agent is in: the gate
token identifies that conversation, and the server side refuses to schedule
anywhere else (apps/server/app/gate.py). An agent that could put work in
another workspace on a timer would have found a way out of its own sandbox.

Written by hand for the same reason ask_user is: the client side of stdio MCP
that matters here is three methods over newline-delimited JSON-RPC, and the
alternative is an SDK in the server image for that.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ana-schedule"
SERVER_VERSION = "1.0.0"

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ANA_SCHEDULE_HTTP_TIMEOUT", 30))

CREATE = "create_schedule"
LIST = "list_schedules"
DELETE = "delete_schedule"

TOOLS = [
    {
        "name": CREATE,
        "description": (
            "Schedule a prompt to run in this conversation on a repeating "
            "schedule. Use it when the user asks for something recurring "
            "(\"every morning\", \"each Monday\", \"nightly\") rather than "
            "promising to remember. The prompt runs exactly as if it had been "
            "typed here, so write it as an instruction, in Korean, that stands "
            "on its own months from now — no \"as we discussed\". Say the "
            "schedule back to the user in words after creating it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short label for the list, in Korean",
                },
                "cron": {
                    "type": "string",
                    "description": (
                        "Five-field cron in UTC: minute hour day month weekday. "
                        "\"30 9 * * *\" is 09:30 daily; \"0 8 * * 1-5\" weekdays "
                        "at 08:00. Ranges, lists and */step are supported."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "What to do, in Korean, as a standalone instruction",
                },
            },
            "required": ["name", "cron", "prompt"],
        },
    },
    {
        "name": LIST,
        "description": "The schedules this conversation already has.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": DELETE,
        "description": (
            "Remove one schedule by id. Ids come from " + LIST + "; confirm "
            "with the user before removing something you did not just create."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]


def _base_url() -> str:
    return (os.environ.get("ANA_GATE_URL") or "http://127.0.0.1:8000").rstrip("/")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{_base_url()}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers={
            "content-type": "application/json",
            "x-ana-gate-token": os.environ.get("ANA_GATE_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _call_tool(name: str, arguments: dict) -> dict:
    try:
        if name == CREATE:
            for field in ("name", "cron", "prompt"):
                if not str(arguments.get(field) or "").strip():
                    return _error(f"{field} is required.")
            created = _request(
                "POST",
                "/gate/schedules",
                {
                    "name": arguments["name"],
                    "cron": arguments["cron"],
                    "prompt": arguments["prompt"],
                },
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Scheduled \"{created['name']}\" ({created['cron_text']}, "
                            f"cron {created['cron']}). Next run: "
                            f"{created.get('next_run_at') or 'unknown'}. Id: {created['id']}."
                        ),
                    }
                ]
            }
        if name == LIST:
            rows = _request("GET", "/gate/schedules").get("schedules") or []
            if not rows:
                return {"content": [{"type": "text", "text": "No schedules in this workspace."}]}
            lines = [
                f"- {row['id']}: {row['name']} — {row['cron_text']} "
                f"({'on' if row['enabled'] else 'paused'}, by {row['created_by']}) "
                f"next {row.get('next_run_at') or '-'}"
                for row in rows
            ]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        if name == DELETE:
            schedule_id = str(arguments.get("id") or "").strip()
            if not schedule_id:
                return _error("id is required.")
            _request("DELETE", f"/gate/schedules/{schedule_id}")
            return {"content": [{"type": "text", "text": f"Deleted {schedule_id}."}]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        # A bad cron expression arrives here as a 400, and the agent can fix it
        # and try again — which is why the reason is passed through verbatim.
        return _error(f"could not reach the scheduler: {exc.code} {detail}")
    except Exception as exc:  # noqa: BLE001 - a failure is a result, not a crash
        return _error(f"could not reach the scheduler: {exc}")
    return _error(f"unknown tool: {name}")


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method = message.get("method")
    message_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in (CREATE, LIST, DELETE):
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": f"unknown tool: {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": _call_tool(name, params.get("arguments") or {}),
        }

    if message_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(stdin=None, stdout=None) -> None:
    """Reads messages until stdin closes. One JSON object per line."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = handle(message)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()


if __name__ == "__main__":
    serve()
