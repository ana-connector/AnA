"""The gate's MCP scope check, end to end through the endpoint."""
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.audit import recent_events, run_audit_migrations
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


def _register(client, thread_id: str) -> "GateRegistry":
    registry: GateRegistry = client.app.state.gate_registry
    registry.approval_timeout = 1.0

    async def send(payload: dict) -> None:
        return None

    return registry.register(thread_id, send)


def _call(client, token: str, tool_name: str, role: str = "worker"):
    return client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": token},
        json={
            "tool_name": tool_name,
            "tool_input": {"query": "select 1"},
            "cli_session_id": "s1",
            "role": role,
            # An agent_id is what marks a call as coming from a sub-agent
            # rather than the lead; without it the lead's no-tools rule fires
            # first and this test would pass for the wrong reason.
            "agent_id": "sub-1",
        },
    )


def test_scoped_server_is_allowed(client):
    session = _register(client, "mcp-gate-1")
    session.current_agent = "codex"

    response = _call(client, session.token, "mcp__git__git_log")

    assert response.status_code == 200
    assert response.json()["decision"] == "allow"


def test_unregistered_server_is_denied_and_audited(client, prepared_pool):
    session = _register(client, "mcp-gate-2")
    session.current_agent = "codex"

    response = _call(client, session.token, "mcp__slack__post_message")

    body = response.json()
    assert body["decision"] == "deny"
    assert body["rule_id"] == "mcp-scope"


async def test_denial_leaves_an_audit_row(client, prepared_pool):
    session = _register(client, "mcp-gate-3")
    session.current_agent = "codex"

    _call(client, session.token, "mcp__slack__post_message")

    events = await recent_events(prepared_pool, limit=10)
    assert any(
        event["tool_name"] == "mcp__slack__post_message" and event["verdict"] == "deny"
        for event in events
    )


def test_lead_still_gets_no_tools(client):
    session = _register(client, "mcp-gate-4")
    session.current_agent = "claude"

    response = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={
            "tool_name": "mcp__git__git_log",
            "tool_input": {},
            "cli_session_id": "s1",
            "role": "lead",
        },
    )

    assert response.json()["decision"] == "deny"
