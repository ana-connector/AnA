"""Per-session workspaces and the gate's path confinement."""
import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.audit import run_audit_migrations
from packages.governance.secrets import run_secret_migrations
from packages.governance.workspace import (
    escaping_paths,
    extract_paths,
    is_within,
    session_workspace,
    workspace_root,
)


def test_each_thread_gets_its_own_directory(tmp_path):
    a = session_workspace(tmp_path, "thread-a")
    b = session_workspace(tmp_path, "thread-b")

    assert a != b
    assert a.is_dir() and b.is_dir()


def test_thread_id_cannot_escape_the_root(tmp_path):
    # thread_id arrives as a query parameter, so it is attacker-controlled.
    path = session_workspace(tmp_path, "../../etc")

    # One segment under the root, whatever the id contained.
    assert path.parent == tmp_path
    assert str(path).startswith(str(tmp_path))


def test_empty_thread_id_still_yields_a_directory(tmp_path):
    assert session_workspace(tmp_path, "").name == "default"


def test_extract_paths_from_commands_and_file_tools():
    assert "/etc/passwd" in extract_paths({"command": "cat /etc/passwd"})
    assert "/home/x/notes.md" in extract_paths({"file_path": "/home/x/notes.md"})


def test_is_within_handles_traversal(tmp_path):
    root = str(tmp_path / "ws")

    assert is_within(f"{root}/sub/file.txt", root) is True
    assert is_within(f"{root}/../other/file.txt", root) is False
    assert is_within("relative/path.txt", root) is True


def test_is_within_rejects_sibling_with_shared_prefix(tmp_path):
    root = str(tmp_path / "ws")

    # "/ws-other" must not count as inside "/ws".
    assert is_within(f"{root}-other/file.txt", root) is False


def test_escaping_paths_ignores_read_only_system_locations(tmp_path):
    root = str(tmp_path / "ws")

    # Flagging these would make every `ls /usr/bin` an approval prompt and
    # train the user to approve without reading.
    assert escaping_paths({"command": "ls /usr/bin && cat /proc/cpuinfo"}, root) == []


def test_escaping_paths_flags_paths_outside_the_workspace(tmp_path):
    root = str(tmp_path / "ws")

    escaping = escaping_paths({"command": "cp secret.txt /home/other/steal.txt"}, root)

    assert escaping == ["/home/other/steal.txt"]


def test_relative_paths_are_not_flagged(tmp_path):
    root = str(tmp_path / "ws")

    # They resolve inside the subprocess's working directory, which is the
    # session workspace.
    assert escaping_paths({"command": "cat notes/todo.md"}, root) == []


def test_workspace_root_falls_back_to_the_old_fixed_directory(monkeypatch):
    monkeypatch.delenv("ANA_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("ANA_WORKSPACE_DIR", "/tmp/legacy-workspace")

    assert workspace_root() == "/tmp/legacy-workspace"


@pytest.fixture
def client(db_pool, postgres_dsn, monkeypatch, tmp_path):
    import asyncio

    asyncio.run(run_audit_migrations(db_pool))
    asyncio.run(run_secret_migrations(db_pool))
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "sessions"))
    with TestClient(app) as test_client:
        yield test_client


def _register(client, thread_id, workspace):
    async def send(payload):
        client.app.state._sent = getattr(client.app.state, "_sent", [])
        client.app.state._sent.append(payload)

    return client.app.state.gate_registry.register(thread_id, send, workspace=workspace)


def _call(client, token, command):
    return client.post(
        "/gate/tool-call",
        headers={"x-ana-gate-token": token},
        json={"tool_name": "Bash", "tool_input": {"command": command}},
    )


def test_command_inside_the_workspace_is_allowed(client, tmp_path):
    workspace = str(tmp_path / "sessions" / "t1")
    session = _register(client, "ws-1", workspace)

    assert _call(client, session.token, f"echo hi > {workspace}/out.txt").json()[
        "decision"
    ] == "allow"


def test_command_reaching_outside_the_workspace_asks(client, tmp_path):
    workspace = str(tmp_path / "sessions" / "t2")
    session = _register(client, "ws-2", workspace)
    client.app.state.gate_registry.approval_timeout = 0.2

    body = _call(client, session.token, "cp notes.md /home/other/steal.md").json()

    # No answer inside the timeout, so it is denied — but the point is that an
    # otherwise-unremarkable command was escalated to a human.
    assert body["decision"] == "deny"
    assert body["rule_id"] == "workspace-escape"
    assert body["category"] == "session-isolation"


def test_a_denied_rule_still_denies_outright(client, tmp_path):
    workspace = str(tmp_path / "sessions" / "t3")
    session = _register(client, "ws-3", workspace)

    body = _call(client, session.token, "sudo cat /home/other/file").json()

    # Path confinement upgrades allow→ask; it never downgrades a denial.
    assert body["decision"] == "deny"
    assert body["rule_id"] == "privilege-escalation"


def test_websocket_gives_each_thread_a_separate_workspace(client, tmp_path):
    # Asserted on the directories rather than the registry: registration
    # happens just after the thread_id frame is sent, so reading the registry
    # right after receiving that frame races the handler.
    for thread_id in ("ws-iso-a", "ws-iso-b"):
        with client.websocket_connect(f"/ws/chat?thread_id={thread_id}") as ws:
            ws.receive_json()
            ws.send_json({"type": "cancel"})
            ws.receive_json()  # round-trip: setup has finished by now

    created = sorted(p.name for p in (tmp_path / "sessions").iterdir())

    assert created == ["ws-iso-a", "ws-iso-b"]


def test_thread_id_frame_carries_the_workspace(client, tmp_path):
    # Server-side isolation is invisible unless the UI is told about it.
    with client.websocket_connect("/ws/chat?thread_id=ws-frame-1") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "thread_id"
    assert frame["workspace"].endswith("/ws-frame-1")
    assert str(tmp_path / "sessions") in frame["workspace"]
