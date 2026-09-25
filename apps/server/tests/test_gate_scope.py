"""The gate enforcing a sub-agent's permission scope, end to end."""
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.audit import run_audit_migrations
from packages.governance.session import GateRegistry


@pytest_asyncio.fixture
async def prepared_pool(db_pool):
    await run_audit_migrations(db_pool)
    return db_pool


@pytest.fixture
def client(prepared_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def _register(client, thread_id: str, workspace: str):
    registry: GateRegistry = client.app.state.gate_registry
    registry.approval_timeout = 1.0

    async def send(payload: dict) -> None:
        return None

    session = registry.register(thread_id, send)
    session.current_agent = "codex"
    session.workspace = workspace
    return session


def _call(client, token: str, tool_name: str, tool_input: dict, **scope):
    return client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": token},
        json={
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cli_session_id": "s1",
            "role": "worker",
            "agent_id": "sub-1",
            **scope,
        },
    )


def test_a_read_scoped_subagent_cannot_write(client, tmp_path):
    session = _register(client, "scope-1", str(tmp_path))

    response = _call(
        client,
        session.token,
        "Write",
        {"file_path": f"{tmp_path}/a.py", "content": "x"},
        scope_mode="read",
    )

    body = response.json()
    assert body["decision"] == "deny"
    assert body["rule_id"] == "subagent-scope"


def test_a_read_scoped_subagent_can_still_read(client, tmp_path):
    session = _register(client, "scope-2", str(tmp_path))

    response = _call(
        client, session.token, "Read", {"file_path": f"{tmp_path}/a.py"}, scope_mode="read"
    )

    assert response.json()["decision"] == "allow"


def test_a_path_scoped_subagent_is_held_to_its_directory(client, tmp_path):
    session = _register(client, "scope-3", str(tmp_path))

    inside = _call(
        client, session.token, "Read", {"file_path": "docs/a.md"}, scope_paths="docs"
    )
    outside = _call(
        client, session.token, "Read", {"file_path": "src/a.py"}, scope_paths="docs"
    )

    assert inside.json()["decision"] == "allow"
    assert outside.json()["decision"] == "deny"
    assert outside.json()["rule_id"] == "subagent-scope"


def test_an_unscoped_subagent_is_unaffected(client, tmp_path):
    session = _register(client, "scope-4", str(tmp_path))

    response = _call(client, session.token, "Read", {"file_path": f"{tmp_path}/a.py"})

    assert response.json()["decision"] == "allow"


def test_a_scope_cannot_widen_what_the_rules_deny(client, tmp_path):
    """A scope narrows; it never grants. A write-scoped worker still cannot
    run a destructive command."""
    session = _register(client, "scope-5", str(tmp_path))

    response = _call(
        client,
        session.token,
        "Bash",
        {"command": "rm -rf /"},
        scope_mode="write",
    )

    assert response.json()["decision"] == "deny"
    assert response.json()["rule_id"] == "fs-recursive-force-delete"


def test_the_clis_own_subagent_tooling_can_be_disabled(client, tmp_path, monkeypatch):
    """The explicit off switch stops native delegation."""
    monkeypatch.setenv("ANA_NATIVE_SUBAGENTS", "off")
    session = _register(client, "native-1", str(tmp_path))

    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": "collaborationspawn_agent",
            "tool_input": {"message": "gAAAAABqs30NedF863Jha3vv1EWPL"},
            "role": "lead",
        },
    )

    body = response.json()
    assert body["decision"] == "deny"
    assert body["rule_id"] == "native-subagents-disabled"
    assert "ANA_NATIVE_SUBAGENTS=off" in body["reason"]


def test_a_sub_agent_cannot_spawn_when_disabled(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_NATIVE_SUBAGENTS", "off")
    session = _register(client, "native-2", str(tmp_path))

    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": "Task",
            "tool_input": {"prompt": "do a thing"},
            "role": "worker",
            "agent_id": "sub-1",
        },
    )

    assert response.json()["rule_id"] == "native-subagents-disabled"


def test_native_subagents_are_available_by_default(client, tmp_path, monkeypatch):
    monkeypatch.delenv("ANA_NATIVE_SUBAGENTS", raising=False)
    session = _register(client, "native-3", str(tmp_path))

    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": "collaborationspawn_agent",
            "tool_input": {"message": "hi"},
            "role": "lead",
        },
    )

    assert response.json()["decision"] == "allow"

    swarm = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "AgentSwarm", "tool_input": {"items": ["work"]}, "role": "lead"},
    )
    assert swarm.json()["decision"] == "allow"


def test_the_lead_still_gets_the_clock(client, tmp_path):
    session = _register(client, "native-4", str(tmp_path))

    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "clockcurr_time", "tool_input": {}, "role": "lead"},
    )

    assert response.json()["decision"] == "allow"
