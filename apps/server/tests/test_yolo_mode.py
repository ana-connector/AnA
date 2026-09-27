"""YOLO mode: approvals are answered "allow" without a human, and nothing else is.

The floor stays where it was — `deny` rules, the secret scan, workspace and
sub-agent scope all return before the gate would ever ask anyone — so the only
thing this mode removes is the person.
"""
import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.audit import recent_events, run_audit_migrations
from packages.governance.session import GateRegistry
from packages.orchestrator.settings import (
    GOVERNANCE_MODES,
    NORMAL_MODE,
    YOLO_MODE,
    read_governance_mode,
    write_governance_mode,
    yolo,
)


@pytest_asyncio.fixture
async def prepared_pool(db_pool):
    await run_audit_migrations(db_pool)
    return db_pool


@pytest.fixture
def client(prepared_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def _register(client, thread_id: str, sent: list, timeout: float = 0.5):
    registry: GateRegistry = client.app.state.gate_registry
    registry.approval_timeout = timeout

    async def send(payload: dict) -> None:
        sent.append(payload)

    return registry.register(thread_id, send)


def _call(client, token: str, command: str, **extra):
    return client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": token},
        json={"tool_name": "Bash", "tool_input": {"command": command},
              "cli_session_id": "s1", **extra},
    ).json()


def _asked(sent: list) -> bool:
    return any(frame.get("type") == "approval_request" for frame in sent)


# --- the mode itself -------------------------------------------------------

def test_the_mode_defaults_to_normal(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ANA_GOVERNANCE_MODE", raising=False)

    assert read_governance_mode() == NORMAL_MODE
    assert yolo() is False


def test_the_stored_mode_outlives_a_restart_and_beats_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANA_GOVERNANCE_MODE", "normal")

    write_governance_mode(YOLO_MODE)

    assert read_governance_mode() == YOLO_MODE
    assert yolo() is True


def test_the_environment_sets_the_starting_position(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANA_GOVERNANCE_MODE", "YOLO")  # case is not the point

    assert read_governance_mode() == YOLO_MODE


def test_an_invented_mode_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        write_governance_mode("maybe")
    # And an invented value in the environment is ignored rather than obeyed.
    monkeypatch.setenv("ANA_GOVERNANCE_MODE", "maybe")
    assert read_governance_mode() == NORMAL_MODE


def test_the_modes_are_exactly_two():
    assert GOVERNANCE_MODES == (NORMAL_MODE, YOLO_MODE)


# --- what the gate does with it -------------------------------------------

def test_without_yolo_a_git_push_still_waits_for_a_human(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    sent: list = []
    session = _register(client, "yolo-off", sent)
    session.current_agent = "claude"

    # Nobody answers, so the approval times out and the call is denied.
    assert _call(client, session.token, "git push")["decision"] == "deny"
    assert _asked(sent)


def test_with_yolo_the_same_push_runs_and_nobody_is_asked(
    client, prepared_pool, tmp_path, monkeypatch
):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    sent: list = []
    session = _register(client, "yolo-on", sent)
    session.current_agent = "claude"

    decision = _call(client, session.token, "git push")

    assert decision["decision"] == "allow"
    assert decision["rule_id"] == "git-push"  # which rule was overridden, kept
    assert not _asked(sent)
    # The audit row must not claim a person approved something nobody saw.
    events = asyncio.run(recent_events(prepared_pool, thread_id="yolo-on"))
    allowed = [e for e in events if e["verdict"] == "allow"]
    assert allowed and allowed[0]["decided_by"] == "yolo"
    assert "YOLO" in allowed[0]["reason"]


def test_yolo_does_not_touch_a_deny_rule(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    session = _register(client, "yolo-deny", [])
    session.current_agent = "claude"

    for command, rule in (
        ("rm -rf /etc", "fs-recursive-force-delete"),
        ("sudo -i", "privilege-escalation"),
        ("psql -c 'drop table turns'", "db-drop"),
        ("shutdown now", "system-power-control"),
    ):
        decision = _call(client, session.token, command)
        assert decision["decision"] == "deny", command
        assert decision["rule_id"] == rule


def test_yolo_does_not_open_the_credential_files(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    session = _register(client, "yolo-secrets", [])
    session.current_agent = "claude"

    for command in (
        "cat .env",
        "cat apps/server/.env.production",
        "cat ~/.ssh/id_rsa",
        "head ~/.aws/credentials",
        "cat ~/.claude/.credentials.json",
        "cat ~/.npmrc",
        "cat server.pem",
        "cat private.key",
        "git diff .env",
    ):
        decision = _call(client, session.token, command)
        assert decision["decision"] == "deny", command
        assert decision["category"] == "credential-exfiltration", command

    for tool_input in (
        {"file_path": "/app/.env"},
        {"path": "/home/x/.ssh/id_ed25519"},
        {"file_path": "/app/secrets/.secrets"},
    ):
        decision = client.post(
            "/gate/tool-call",
            headers={"x-ana-gate-token": session.token},
            json={"tool_name": "Read", "tool_input": tool_input},
        ).json()
        assert decision["decision"] == "deny", tool_input
        assert decision["rule_id"] == "secret-file-read"


def test_the_example_env_file_is_still_readable(client, tmp_path, monkeypatch):
    """It exists to be read, it carries placeholders, and `cp .env.example .env`
    is this repository's own documented setup step."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    session = _register(client, "yolo-example", [])
    session.current_agent = "claude"

    for command in ("cat .env.example", "cat .env.sample", "cp .env.example .env"):
        assert _call(client, session.token, command)["decision"] == "allow", command

    read = client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": session.token},
        json={"tool_name": "Read", "tool_input": {"file_path": "/app/.env.example"}},
    ).json()
    assert read["decision"] == "allow"


def test_yolo_does_not_widen_a_sub_agents_scope(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    session = _register(client, "yolo-scope", [])
    session.current_agent = "claude"

    decision = _call(
        client, session.token, "touch changed.txt", role="worker", scope_mode="read"
    )

    assert decision["decision"] == "deny"
    assert decision["rule_id"] == "subagent-scope"


def test_yolo_still_blocks_a_command_carrying_a_secret_value(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    client.put("/secrets", json={"name": "GITHUB_TOKEN", "value": "ghp_leakme_1234567"})
    session = _register(client, "yolo-leak", [])
    session.current_agent = "claude"
    try:
        decision = _call(client, session.token, "echo ghp_leakme_1234567")
    finally:
        client.delete("/secrets/global/GITHUB_TOKEN")

    assert decision["decision"] == "deny"
    assert decision["rule_id"] == "secret-egress"
    assert "GITHUB_TOKEN" in decision["reason"]


def test_turning_it_off_takes_effect_on_the_next_call(client, tmp_path, monkeypatch):
    """Read per tool call, not per connection: a mode like this is only safe to
    offer if switching it back is immediate."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    write_governance_mode(YOLO_MODE)
    sent: list = []
    session = _register(client, "yolo-toggle", sent)
    session.current_agent = "claude"

    assert _call(client, session.token, "git push")["decision"] == "allow"
    assert not _asked(sent)

    write_governance_mode(NORMAL_MODE)

    assert _call(client, session.token, "git push")["decision"] == "deny"
    assert _asked(sent)


# --- the settings endpoint -------------------------------------------------

def test_the_settings_endpoint_reports_and_stores_the_mode(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))

    before = client.get("/settings").json()
    assert before["governance_mode"] == NORMAL_MODE
    assert before["governance_modes"] == list(GOVERNANCE_MODES)

    stored = client.put("/settings", json={"governance_mode": "yolo"}).json()
    assert stored["governance_mode"] == YOLO_MODE
    assert client.get("/settings").json()["governance_mode"] == YOLO_MODE

    # And an invented mode is a 422, not a silently stored value.
    assert client.put("/settings", json={"governance_mode": "sure"}).status_code == 422
    assert client.get("/settings").json()["governance_mode"] == YOLO_MODE


def test_an_unwritable_state_directory_reads_as_normal_not_as_an_error(monkeypatch):
    """The mode is read on every tool call, so a state directory that cannot be
    created must mean "normal", not a failed tool call."""
    monkeypatch.setenv("ANA_STATE_DIR", "/proc/definitely-not-writable/.ana")
    monkeypatch.delenv("ANA_GOVERNANCE_MODE", raising=False)

    assert read_governance_mode() == NORMAL_MODE
