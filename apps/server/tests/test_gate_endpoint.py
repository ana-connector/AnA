"""The gate endpoint: rules, audit ordering, approval and rejection."""
import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.audit import recent_events, run_audit_migrations
from packages.governance.session import GateRegistry
from packages.governance.subagent_scope import Scope


@pytest_asyncio.fixture
async def prepared_pool(db_pool):
    await run_audit_migrations(db_pool)
    return db_pool


@pytest.fixture
def client(prepared_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def _register(client, thread_id: str, sent: list, timeout: float = 1.0):
    """Registers a gate session whose 'browser' just records what it is sent."""
    registry: GateRegistry = client.app.state.gate_registry
    registry.approval_timeout = timeout

    async def send(payload: dict) -> None:
        sent.append(payload)

    return registry.register(thread_id, send)


def _await_approval(sent: list, timeout: float = 5.0) -> dict:
    """The gate also streams a live "tool" frame per decision, so the approval
    request has to be picked out by type rather than by arrival order."""
    import threading
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in sent:
            if frame.get("type") == "approval_request":
                return frame
        threading.Event().wait(0.02)
    raise AssertionError("the browser was never asked for approval")


def _call(client, token: str, command: str):
    return client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": token},
        json={"tool_name": "Bash", "tool_input": {"command": command}, "cli_session_id": "s1"},
    )


def test_unknown_token_is_refused(client):
    response = _call(client, "not-a-real-token", "echo hi")

    assert response.status_code == 403


def test_allowed_command_passes_and_is_audited(client, prepared_pool):
    session = _register(client, "gate-1", [])
    session.current_agent = "claude"

    response = _call(client, session.token, "echo hello")

    assert response.json()["decision"] == "allow"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-1"))
    assert [e["verdict"] for e in events] == ["allow"]
    assert events[0]["agent"] == "claude"


@pytest.mark.parametrize("command", ["pytest -q", "git add .", "git commit -m update"])
def test_lead_can_use_tools_without_a_planning_scope(client, command):
    session = _register(client, "lead-controls", [])
    session.current_agent = "codex"
    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "clockcurr_time", "tool_input": {}, "role": "lead"},
    )
    assert response.json()["decision"] == "allow"

    shell = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "Bash", "tool_input": {"command": command}, "role": "lead"},
    )
    assert shell.json()["decision"] == "allow"


def test_planner_grants_lead_a_temporary_read_scope(client):
    session = _register(client, "lead-execute", [])
    session.current_agent = "codex"
    session.lead_scope = Scope(mode="read")

    def call(command):
        return client.post(
            "/gate/tool-call",
            headers={"x-ana-gate-token": session.token},
            json={"tool_name": "Bash", "tool_input": {"command": command},
                  "role": "lead", "scope_mode": "write"},
        ).json()

    assert call("ls")["decision"] == "allow"
    assert call("touch changed.txt")["rule_id"] == "subagent-scope"
    session.lead_scope = None
    assert call("ls")["decision"] == "allow"


@pytest.mark.parametrize("tool_name", [
    "request_user_input_async",
    "functionsrequest_user_input_async",
    "mcp__ask__ask_user",
])
@pytest.mark.parametrize("role", ["lead", "worker"])
def test_every_agent_can_ask_the_user(client, role, tool_name):
    session = _register(client, f"ask-{role}-{tool_name}", [])
    session.current_agent = "codex"
    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": tool_name,
            "tool_input": {"questions": [{"title": "쉬는 날에는 무엇을 하세요?"}]},
            "role": role,
        },
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "allow"


def test_native_subagent_inherits_token_but_uses_worker_permissions(client):
    sent: list = []
    session = _register(client, "native-worker", sent)
    session.current_agent = "codex"
    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "role": "worker",
            "agent_id": "child-1",
            "agent_type": "worker",
        },
    )
    assert response.json()["decision"] == "allow"
    assert session.native_agents["child-1"]["tools"][0]["verdict"] == "allow"


def test_native_subagent_lifecycle_is_streamed(client):
    sent: list = []
    session = _register(client, "native-events", sent)
    session.current_agent = "codex"
    for event, output in (("SubagentStart", ""), ("SubagentStop", "done")):
        response = client.post(
            "/gate/subagent",
            headers={"x-ana-gate-token": session.token},
            json={"event": event, "agent_id": "child-1", "agent_type": "worker", "output": output},
        )
        assert response.status_code == 200
    assert [frame["task"]["status"] for frame in sent] == ["running", "done"]
    assert sent[-1]["task"]["output"] == "done"


def test_native_subagent_card_uses_the_agent_call_description(client):
    sent: list = []
    session = _register(client, "native-title", sent)
    session.current_agent = "claude"
    call = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "Agent", "tool_input": {
            "description": "GitHub 저장소 목록 조회", "prompt": "List repositories",
            "subagent_type": "general-purpose"}, "role": "lead"},
    )
    assert call.json()["decision"] == "allow"
    started = client.post(
        "/gate/subagent",
        headers={"x-ana-gate-token": session.token},
        json={"event": "SubagentStart", "agent_id": "child-1",
              "agent_type": "general-purpose"},
    )
    assert started.json() == {"ok": True}
    assert sent[-1]["task"]["title"] == "GitHub 저장소 목록 조회"


def test_delegate_endpoint_runs_the_bound_worker(client):
    session = _register(client, "routed-worker", [])
    called = []

    async def delegate(title, prompt, task_id):
        called.append((title, prompt, task_id))
        return {"status": "done", "output": "저장소 3개", "agent": "codex"}

    session.delegate_worker = delegate
    response = client.post(
        "/gate/delegate", headers={"x-ana-gate-token": session.token},
        json={"title": "저장소 조회", "prompt": "List repositories"},
    )
    assert response.json() == {"status": "done", "output": "저장소 3개", "agent": "codex"}
    assert called == [("저장소 조회", "List repositories", "")]

    polled = client.post(
        "/gate/delegate", headers={"x-ana-gate-token": session.token},
        json={"task_id": "existing-task"},
    )
    assert polled.status_code == 200
    assert called[-1] == ("", "", "existing-task")


def test_lead_can_call_delegate_mcp_but_worker_cannot(client):
    session = _register(client, "delegate-scope", [])
    headers = {"x-ana-gate-token": session.token}
    body = {"tool_name": "mcp__delegate__run_task",
            "tool_input": {"title": "저장소 조회", "prompt": "List repositories"}}
    lead = client.post("/gate/tool-call", headers=headers, json={**body, "role": "lead"})
    worker = client.post("/gate/tool-call", headers=headers, json={**body, "role": "worker"})
    assert lead.json()["decision"] == "allow"
    assert worker.json()["rule_id"] == "mcp-scope"


def test_denied_command_is_blocked_with_its_rule(client, prepared_pool):
    session = _register(client, "gate-2", [])

    response = _call(client, session.token, "rm -rf /home/hello/workspace")
    body = response.json()

    assert body["decision"] == "deny"
    assert body["rule_id"] == "fs-recursive-force-delete"
    assert body["category"] == "filesystem-destruction"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-2"))
    assert events[0]["decided_by"] == "rule"
    assert events[0]["verdict"] == "deny"


def test_risky_command_asks_and_can_be_approved(client, prepared_pool):
    sent: list = []
    session = _register(client, "gate-3", sent, timeout=10)
    registry = client.app.state.gate_registry

    # The approval answer has to arrive while the request is in flight, so the
    # POST runs on a worker thread and the answer is resolved from here.
    import threading

    result = {}

    def do_call():
        result["response"] = _call(client, session.token, "git push origin main")

    thread = threading.Thread(target=do_call)
    thread.start()
    request = _await_approval(sent)

    assert request["rule_id"] == "git-push"
    # The live "tool" frame goes out too, but the approval is its own frame.
    assert any(f["type"] == "tool" for f in sent)

    registry.resolve_approval(session, request["request_id"], "allow")
    thread.join(timeout=10)

    assert result["response"].json()["decision"] == "allow"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-3"))
    # Asked first (pending), then the user's answer — both recorded.
    assert [e["verdict"] for e in events] == ["allow", "pending"]
    assert events[0]["decided_by"] == "user"


def test_risky_command_can_be_denied_by_the_user(client, prepared_pool):
    import threading

    sent: list = []
    session = _register(client, "gate-4", sent, timeout=10)
    registry = client.app.state.gate_registry
    result = {}

    def do_call():
        result["response"] = _call(client, session.token, "git push origin main")

    thread = threading.Thread(target=do_call)
    thread.start()
    registry.resolve_approval(session, _await_approval(sent)["request_id"], "deny")
    thread.join(timeout=10)

    assert result["response"].json()["decision"] == "deny"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-4"))
    assert events[0]["decided_by"] == "user"
    assert events[0]["verdict"] == "deny"


def test_an_approval_from_discord_says_so_in_the_log(client, prepared_pool):
    """A phone can answer the same request as the browser, and the audit row
    has to say which one did -- a button that runs a command the rule set
    stopped is worth attributing to a person."""
    import threading

    sent: list = []
    session = _register(client, "gate-discord", sent, timeout=10)
    registry = client.app.state.gate_registry
    result = {}

    def do_call():
        result["response"] = _call(client, session.token, "git push origin main")

    thread = threading.Thread(target=do_call)
    thread.start()
    request = _await_approval(sent)

    # No gate token: the request id from the button's custom id is all a
    # Discord interaction carries.
    assert registry.resolve_remote_approval(request["request_id"], "allow", "discord:42") is True
    thread.join(timeout=10)

    assert result["response"].json()["decision"] == "allow"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-discord"))
    assert events[0]["verdict"] == "allow"
    assert events[0]["decided_by"] == "discord:42"


def test_unanswered_request_times_out_as_denied(client, prepared_pool):
    sent: list = []
    session = _register(client, "gate-5", sent, timeout=0.3)

    response = _call(client, session.token, "git push origin main")

    assert response.json()["decision"] == "deny"
    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-5"))
    assert events[0]["decided_by"] == "timeout"


def test_closed_session_invalidates_the_token(client):
    session = _register(client, "gate-6", [])
    client.app.state.gate_registry.unregister(session.token)

    assert _call(client, session.token, "echo hi").status_code == 403


def test_audit_endpoint_lists_events(client):
    session = _register(client, "gate-7", [])
    _call(client, session.token, "echo hi")

    response = client.get("/audit?thread_id=gate-7")

    assert response.status_code == 200
    assert response.json()["events"][0]["tool_name"] == "Bash"


def test_pending_and_final_rows_share_the_tool_use_id(client, prepared_pool):
    """An approved call writes two rows (asked, then decided). They carry the
    same tool_use_id so a reader can show one call instead of two."""
    import asyncio
    import threading

    sent: list = []
    session = _register(client, "gate-8", sent, timeout=10)
    registry = client.app.state.gate_registry
    result = {}

    def do_call():
        result["response"] = client.post(
            "/gate/tool-call",
            headers={"x-ana-gate-token": session.token},
            json={
                "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"},
                "tool_use_id": "toolu_shared",
            },
        )

    thread = threading.Thread(target=do_call)
    thread.start()
    registry.resolve_approval(session, _await_approval(sent)["request_id"], "allow")
    thread.join(timeout=10)

    events = asyncio.run(recent_events(prepared_pool, thread_id="gate-8"))

    assert [e["verdict"] for e in events] == ["allow", "pending"]
    assert {e["tool_use_id"] for e in events} == {"toolu_shared"}
    # The live frames carry it too, so the chat list updates its entry.
    assert {f["tool"]["tool_use_id"] for f in sent if f["type"] == "tool"} == {"toolu_shared"}
