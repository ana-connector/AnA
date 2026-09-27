"""ANA's own MCP server: one tool that asks the user a question and waits.

Why this exists at all: everything else in ANA flows one way — the user asks,
the agents work, the gate interrupts when a tool call needs a decision. The
agent itself had no way to reach the person who wants the work done, so a lead
facing a real fork in the requirements had to guess, and a guess that is wrong
is discovered at the end.

Why it is an MCP server rather than part of the gate: the gate is driven by the
CLIs' PreToolUse hook, which only ever fires *because* the agent did something.
A question the agent initiates needs a tool the agent can call, and a tool the
CLIs will call is an MCP tool.

Why the protocol is written out by hand: ANA has no MCP SDK dependency (the
other servers in config/mcp.yaml are third-party binaries run through `uvx`),
and the client side of stdio MCP that matters here is three methods —
`initialize`, `tools/list`, `tools/call` — over newline-delimited JSON-RPC.
Adding an SDK to the server image for that is a bigger change than this file.

The answer comes back through ANA's gate: POST /gate/ask with the same token
the hook already uses, which finds the WebSocket session for this conversation,
puts the question in front of the browser, and waits.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ana-ask"
SERVER_VERSION = "1.0.0"

TOOL_NAME = "ask_user"
# English, like every other text a model reads here; the questions themselves are
# read by a person, so the description says to write those in Korean.
TOOL_DESCRIPTION = (
    "Ask the user directly and wait for the answer. Use it only when the answer "
    "changes what gets built: a fork in the requirements, a choice that is hard "
    "to undo, a value only they know (target environment, priorities). Do not ask "
    "what could be found by reading the code — have a sub-agent check instead. "
    "Questions may have labelled choices with descriptions, allow several "
    "selections, offer a free-text answer, or be optional and skippable. "
    "Provide at least two choices or an input field for each question. "
    "Write the questions and options in Korean. If nobody "
    "answers, a timeout comes back as the result — then state your assumption and "
    "carry on."
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "description": "Questions to ask at once. The more you ask, the less likely you get an answer.",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "One question, in Korean"},
                    "options": {
                        "type": "array",
                        "items": {"oneOf": [
                            {"type": "string"},
                            {"type": "object", "properties": {
                                "value": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            }, "required": ["value", "label"]},
                        ]},
                        "description": "Fixed choices; use objects for labels and explanations",
                    },
                    "input": {"type": "object", "properties": {
                        "label": {"type": "string"},
                        "placeholder": {"type": "string"},
                    }, "required": ["label"]},
                    "required": {"type": "boolean", "description": "False permits skipping"},
                    "multi": {
                        "type": "boolean",
                        "description": "Whether several options may be picked at once",
                    },
                    "note": {
                        "type": "string",
                        "description": "Why you are asking, or what each option implies (Korean)",
                    },
                },
                "required": ["question"],
            },
        }
    },
    "required": ["questions"],
}

# How long to wait on the HTTP call. Deliberately longer than the gate's own
# question timeout, so the timeout that decides is ANA's (which returns a
# result) rather than this socket's (which would only look like a broken tool).
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ANA_ASK_HTTP_TIMEOUT", 1200))


def _gate_url() -> str:
    base = (os.environ.get("ANA_GATE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/gate/ask"


def _ask(questions: list) -> dict:
    """Puts the questions to ANA and returns its answer payload."""
    body = json.dumps(
        {
            "questions": questions,
            "role": os.environ.get("ANA_GATE_ROLE") or "lead",
            # Which sub-agent is asking, so ANA stops that agent's idle clock
            # while the question is on screen: an agent waiting for an answer is
            # not an agent that has stopped working.
            "agent_id": os.environ.get("ANA_GATE_AGENT_ID"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _gate_url(),
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-ana-gate-token": os.environ.get("ANA_GATE_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _answer_text(payload: dict, questions: list) -> str:
    """The tool result the agent reads.

    Questions and answers paired up in order, because an answer on its own
    ("두 번째") is not usable by a model that has since written other things."""
    if not payload.get("answered"):
        return payload.get("note") or "The user did not answer."
    lines = []
    for index, question in enumerate(questions):
        answers = payload.get("answers") or []
        given = answers[index] if index < len(answers) else None
        if isinstance(given, list):
            given = ", ".join(str(item) for item in given)
        lines.append(f"Q: {question.get('question', '')}\nA: {given or '(no answer)'}")
    return "\n\n".join(lines)


def _call_tool(arguments: dict) -> dict:
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not questions:
        return {
            "content": [{"type": "text", "text": "questions needs at least one entry."}],
            "isError": True,
        }
    if any(not isinstance(question, dict) or not (
        isinstance(question.get("options"), list) and len(question["options"]) >= 2
        or isinstance(question.get("input"), dict) and bool(question["input"].get("label"))
    ) for question in questions):
        return {
            "content": [{"type": "text", "text": "Each question needs at least two choices or a labelled input."}],
            "isError": True,
        }
    try:
        payload = _ask(questions)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {
            "content": [{"type": "text", "text": f"could not deliver the question: {exc.code} {detail}"}],
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 - a broken question must not look
        # like a crashed server: the agent gets a readable failure and decides.
        return {
            "content": [{"type": "text", "text": f"could not deliver the question: {exc}"}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": _answer_text(payload, questions)}]}


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
                # Echo the client's version when it named one: both CLIs are
                # newer than this file will be, and refusing an unknown version
                # would make the server vanish from a working setup.
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
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        }

    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": _call_tool(params.get("arguments") or {}),
        }

    if message_id is None:
        return None  # an unknown notification is not an error worth answering
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
            continue  # a client that sends garbage gets no answer, not a crash
        if not isinstance(message, dict):
            continue
        response = handle(message)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()


if __name__ == "__main__":
    serve()
