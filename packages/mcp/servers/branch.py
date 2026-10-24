"""ANA's own MCP server: the agent managing its project's feature branches.

A project holds feature branches, each a git worktree with its own lead agent
and its own conversation. Until now only a person could create or merge one
through the UI, so an agent that decided "this should be its own branch" could
only say so and wait.

Everything it touches belongs to the project the agent is working in: the gate
token identifies the conversation, and the server side resolves that to a
single project and refuses anything outside it (apps/server/app/gate.py). A
branch's own agent gets its parent project, so it can see its siblings and
finish a merge it was handed a conflict for.

Two things this deliberately does not do:

- **It never resolves a conflict for anyone.** A merge that conflicts leaves
  the markers in the branch's own checkout and says so; the agent working in
  that branch fixes them and merges again.
- **It never force-deletes on its own.** A branch holding commits the project
  does not have is the only copy of that work, so the refusal is what the
  agent gets, and forcing past it is a decision it has to make explicitly.

Written by hand for the same reason schedule.py is: the client side of stdio
MCP that matters here is three methods over newline-delimited JSON-RPC.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "ana-branch"
SERVER_VERSION = "1.0.0"

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("ANA_BRANCH_HTTP_TIMEOUT", 180))

LIST = "list_branches"
CREATE = "create_branch"
RENAME = "rename_branch"
MERGE = "merge_branch"
DELETE = "delete_branch"

TOOL_NAMES = (LIST, CREATE, RENAME, MERGE, DELETE)

TOOLS = [
    {
        "name": LIST,
        "description": (
            "This project and the feature branches under it. Each branch has "
            "its own checkout, its own lead agent and its own conversation. "
            "Call this before any other branch tool: the ids the others take "
            "come from here, and it also says whether this project can host "
            "branches at all."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": CREATE,
        "description": (
            "Start a feature branch in this project. Use it when a piece of "
            "work should proceed in parallel with what is happening on main — "
            "a separate checkout so two features never fight over the same "
            "file. The branch gets its own lead agent and its own "
            "conversation, so it does not continue this one: say to the user "
            "what the branch is for after creating it. Name it after the "
            "feature, in the user's language."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "What the branch is for, as a short name",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": RENAME,
        "description": (
            "Rename a branch's display name. The git branch and its checkout "
            "directory keep the name they were created with — moving those "
            "under a running agent is how a branch loses its work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "From " + LIST},
                "name": {"type": "string"},
            },
            "required": ["id", "name"],
        },
    },
    {
        "name": MERGE,
        "description": (
            "Merge a branch's work back into the project. Read the result "
            "rather than assuming it worked — most outcomes are not 'merged':\n"
            "- merged: the work is in the project now\n"
            "- empty: the branch has nothing the project does not have\n"
            "- dirty: the project has uncommitted changes. Retry with "
            "commit_project=true only if committing that work is what the "
            "user wants; it is their unfinished work in their repository\n"
            "- unresolved: this branch still has conflict markers from an "
            "earlier attempt. They must be fixed in the branch's own checkout "
            "first\n"
            "- conflict: aborted in the project, and the markers are now in "
            "the branch's checkout. The agent working in that branch resolves "
            "them and merges again\n"
            "- moved / missing / failed: say the reason to the user"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "From " + LIST},
                "commit_project": {
                    "type": "boolean",
                    "description": (
                        "Commit the project's own uncommitted work first. "
                        "Only after the user has asked for that."
                    ),
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": DELETE,
        "description": (
            "Remove a branch: its checkout, its git branch and its "
            "conversation. Refused while it holds commits the project does "
            "not have, because that branch is then the only copy of that "
            "work — merge it first, or ask the user before passing force."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "From " + LIST},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Delete even with unmerged commits, destroying them. "
                        "Only when the user has said so."
                    ),
                },
            },
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


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _merge_text(outcome: dict) -> str:
    """The merge result as something the agent can act on.

    Each status says what to do next, because "merge failed" on its own sends
    an agent looking for the wrong thing — most notably a conflict, whose
    markers are sitting in a checkout it has to be told about."""
    status = outcome.get("status", "")
    detail = (outcome.get("detail") or "").strip()
    paths = outcome.get("paths") or []
    listed = ", ".join(paths)
    if status == "merged":
        return "Merged into the project."
    if status == "empty":
        return "Nothing to merge: this branch has no work the project does not already have."
    if status == "dirty":
        return (
            "Refused: the project checkout has uncommitted changes, so merging "
            "would mix them into the merge commit. That work is the user's — "
            "ask them, and only then retry with commit_project=true.\n"
            f"{detail}"
        )
    if status == "unresolved":
        return (
            f"Refused: this branch still has an unresolved conflict in {listed}. "
            "Fix the conflict markers in this branch's own checkout, commit, "
            "then merge again. Nothing was merged."
        )
    if status == "conflict":
        return (
            f"Conflict in {listed}. The project's merge was rolled back, and the "
            "base branch has been merged into this branch's checkout instead, so "
            "the conflict markers are in those files now. Resolve them there, "
            f"commit, and merge again.\n{detail}"
        )
    if status == "moved":
        return f"Refused: {detail}"
    if status == "missing":
        return f"Refused: this branch's checkout is gone from disk ({detail})."
    if status == "failed":
        return f"git refused the merge and nothing was changed:\n{detail}"
    return f"{status}: {detail}"


def _call_tool(name: str, arguments: dict) -> dict:
    try:
        if name == LIST:
            body = _request("GET", "/gate/branches")
            project = body.get("project") or {}
            rows = body.get("branches") or []
            head = f"Project: {project.get('name')} ({project.get('id')})"
            if not body.get("can_branch", True):
                head += f"\nCannot create branches here: {body.get('reason')}"
            if not rows:
                return _text(head + "\nNo feature branches yet.")
            lines = [
                f"- {row['id']}: {row['name']} [{row['branch']} -> {row['base_branch']}]"
                + (" (archived)" if row.get("archived") else "")
                for row in rows
            ]
            return _text(head + "\n" + "\n".join(lines))

        if name == CREATE:
            label = str(arguments.get("name") or "").strip()
            if not label:
                return _error("name is required.")
            created = _request("POST", "/gate/branches", {"name": label})
            return _text(
                f"Created branch \"{created['name']}\" (id {created['id']}, git "
                f"{created['branch']} off {created['base_branch']}). It has its own "
                "lead agent and its own conversation, which this one does not "
                "continue — tell the user what it is for and that they can open it "
                "from the sidebar."
            )

        if name == RENAME:
            branch_id = str(arguments.get("id") or "").strip()
            label = str(arguments.get("name") or "").strip()
            if not branch_id or not label:
                return _error("id and name are required.")
            renamed = _request(
                "PATCH", f"/gate/branches/{urllib.parse.quote(branch_id)}",
                {"name": label},
            )
            return _text(
                f"Renamed to \"{renamed['name']}\". The git branch is still "
                f"{renamed['branch']}."
            )

        if name == MERGE:
            branch_id = str(arguments.get("id") or "").strip()
            if not branch_id:
                return _error("id is required.")
            outcome = _request(
                "POST", f"/gate/branches/{urllib.parse.quote(branch_id)}/merge",
                {"commit_project": bool(arguments.get("commit_project"))},
            )
            return _text(_merge_text(outcome))

        if name == DELETE:
            branch_id = str(arguments.get("id") or "").strip()
            if not branch_id:
                return _error("id is required.")
            query = "?force=true" if arguments.get("force") else ""
            _request(
                "DELETE", f"/gate/branches/{urllib.parse.quote(branch_id)}{query}"
            )
            return _text(f"Deleted {branch_id}.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # Passed through verbatim: a 409 is the unmerged-commits refusal and a
        # 422 is a name the project cannot take, and both are things the agent
        # can act on rather than retry blindly.
        return _error(f"refused ({exc.code}): {detail}")
    except Exception as exc:  # noqa: BLE001 - a failure is a result, not a crash
        return _error(f"could not reach ANA: {exc}")
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
        if name not in TOOL_NAMES:
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
