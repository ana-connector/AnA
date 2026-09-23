"""Wiring the gate into the adapters and the WebSocket protocol."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.server.app.main import app
from apps.server.app.websocket import _parse_client_frame, build_adapter, build_codex_adapter
from packages.adapters.claude_code import ClaudeCodeAdapter
from packages.adapters.codex import CodexAdapter
from packages.adapters.env_builder import with_gate
from packages.governance.cli_config import prepare_codex_home, write_claude_settings


def test_claude_argv_drops_bypass_when_the_gate_is_wired(tmp_path):
    adapter = ClaudeCodeAdapter(
        workspace_dir=str(tmp_path),
        env={},
        settings_path="/tmp/settings.json",
        permission_prompts="none",
    )

    args, _ = adapter._build_args("hi", None)

    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--settings") + 1] == "/tmp/settings.json"
    # Backstop: anything the hook does not allow is denied rather than left
    # waiting on a prompt nobody can answer in a -p run.
    assert args[args.index("--permission-prompts") + 1] == "none"


def test_claude_argv_omits_permission_prompts_when_unsupported(tmp_path):
    # The npm build of the same CLI version rejects the flag outright
    # ("error: unknown option"), so it is only passed when advertised.
    adapter = ClaudeCodeAdapter(
        workspace_dir=str(tmp_path), env={}, settings_path="/tmp/settings.json"
    )

    args, _ = adapter._build_args("hi", None)

    assert "--permission-prompts" not in args
    assert args[args.index("--settings") + 1] == "/tmp/settings.json"


def test_permission_prompt_support_probe(tmp_path):
    from packages.adapters.claude_code import supports_permission_prompts

    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\necho '  --permission-prompts <target>'\n")
    fake.chmod(0o755)
    assert supports_permission_prompts(str(fake)) is True

    old = tmp_path / "old-claude"
    old.write_text("#!/bin/sh\necho '  --permission-mode <mode>'\n")
    old.chmod(0o755)
    assert supports_permission_prompts(str(old)) is False

    # A probe that cannot run at all must not crash a turn.
    assert supports_permission_prompts(str(tmp_path / "missing")) is False


def test_claude_argv_keeps_bypass_without_the_gate(tmp_path):
    adapter = ClaudeCodeAdapter(workspace_dir=str(tmp_path), env={})

    args, _ = adapter._build_args("hi", None)

    assert "--dangerously-skip-permissions" in args


def test_codex_argv_drops_approval_bypass_when_hooks_are_on(tmp_path):
    adapter = CodexAdapter(
        workspace_dir=str(tmp_path), env={}, hooks_enabled=True, sandbox="workspace-write"
    )

    args = adapter._build_args("hi", None)

    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    # Hook trust prompts cannot be answered in automation; this flag skips
    # those, not the sandbox.
    assert "--dangerously-bypass-hook-trust" in args
    assert args[args.index("-s") + 1] == "workspace-write"


def test_codex_argv_keeps_bypass_without_hooks(tmp_path):
    adapter = CodexAdapter(workspace_dir=str(tmp_path), env={})

    args = adapter._build_args("hi", None)

    assert "--dangerously-bypass-approvals-and-sandbox" in args


def test_claude_settings_file_registers_the_hook(tmp_path):
    path = write_claude_settings(tmp_path, python_executable="/usr/bin/python3")

    settings = json.loads(Path(path).read_text())
    entry = settings["hooks"]["PreToolUse"][0]["hooks"][0]

    assert entry["type"] == "command"
    assert entry["command"].startswith("/usr/bin/python3 ")
    assert entry["command"].endswith("hook.py")


def test_codex_home_carries_hooks_and_links_credentials(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "x"}')
    (source / "config.toml").write_text("")

    home = prepare_codex_home(tmp_path / "ana_codex", source)

    hooks = json.loads((home / "hooks.json").read_text())
    assert hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("hook.py")
    # Credentials are linked, not copied: ANA keeps its own CODEX_HOME so it
    # never writes into the user's real ~/.codex.
    assert (home / "auth.json").is_symlink()
    assert json.loads((home / "auth.json").read_text())["token"] == "x"


def test_codex_home_preparation_is_idempotent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text("{}")

    prepare_codex_home(tmp_path / "ana_codex", source)
    prepare_codex_home(tmp_path / "ana_codex", source)  # must not raise on the existing link


def test_with_gate_injects_only_the_gate_coordinates():
    env = with_gate({"PATH": "/bin"}, "http://127.0.0.1:8000", "tok")

    assert env["ANA_GATE_URL"] == "http://127.0.0.1:8000"
    assert env["ANA_GATE_TOKEN"] == "tok"
    assert env["PATH"] == "/bin"


def test_build_adapter_wires_the_gate_when_a_token_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "pro")
    import apps.server.app.websocket as ws

    monkeypatch.setattr(ws, "ANA_STATE_DIR", str(tmp_path))
    adapter = build_adapter("token-123")

    assert adapter.settings_path is not None
    assert adapter.env["ANA_GATE_TOKEN"] == "token-123"


def test_build_adapter_leaves_the_gate_off_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "pro")
    monkeypatch.setenv("ANA_GOVERNANCE", "off")
    import apps.server.app.websocket as ws

    monkeypatch.setattr(ws, "ANA_STATE_DIR", str(tmp_path))
    adapter = build_adapter("token-123")

    # The escape hatch is explicit and opt-in; forgetting the variable keeps
    # the gate on.
    assert adapter.settings_path is None
    assert "ANA_GATE_TOKEN" not in adapter.env


def test_build_codex_adapter_wires_hooks(tmp_path, monkeypatch):
    source = tmp_path / "codex"
    source.mkdir()
    (source / "auth.json").write_text("{}")
    import apps.server.app.websocket as ws

    monkeypatch.setattr(ws, "ANA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ws, "CODEX_SOURCE_HOME", str(source))
    adapter = build_codex_adapter("token-456")

    assert adapter.hooks_enabled is True
    assert adapter.env["CODEX_HOME"].endswith("codex_home")
    assert adapter.env["ANA_GATE_TOKEN"] == "token-456"


def test_parse_client_frame_reads_approval_answers():
    frame = _parse_client_frame('{"type": "approval", "request_id": "r1", "decision": "allow"}')

    assert (frame.kind, frame.request_id, frame.decision) == ("approval", "r1", "allow")


def test_parse_client_frame_treats_unknown_decision_as_deny():
    # An approval frame is a security decision; anything that is not an
    # explicit "allow" must not be read as one.
    frame = _parse_client_frame('{"type": "approval", "request_id": "r1", "decision": "yes"}')

    assert frame.decision == "deny"


@pytest.fixture
def client(db_pool, postgres_dsn, monkeypatch, tmp_path):
    from packages.adapters.transcript import TranscriptLogger

    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "pro")
    with patch(
        "apps.server.app.websocket.build_transcript",
        return_value=TranscriptLogger(str(tmp_path)),
    ):
        with TestClient(app) as test_client:
            yield test_client


def _session_for(registry, thread_id):
    return [s for s in registry._sessions.values() if s.thread_id == thread_id]


def test_websocket_connection_registers_and_releases_a_gate_token(client):
    registry = client.app.state.gate_registry

    with client.websocket_connect("/ws/chat?thread_id=gate-ws-1") as ws:
        ws.receive_json()  # thread_id
        assert len(_session_for(registry, "gate-ws-1")) == 1

    # Closing the connection must invalidate the token: a hook that outlives
    # its conversation should not keep getting approvals. Teardown runs on the
    # app's own loop, so this polls rather than assuming it finished the
    # instant the client-side context manager exited.
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _session_for(registry, "gate-ws-1"):
        time.sleep(0.02)
    assert _session_for(registry, "gate-ws-1") == []


def test_websocket_ignores_an_approval_for_an_unknown_request(client):
    # A stale card in a reopened tab must not break the connection.
    with client.websocket_connect("/ws/chat?thread_id=gate-ws-2") as ws:
        ws.receive_json()  # thread_id
        ws.send_json({"type": "approval", "request_id": "no-such-request", "decision": "allow"})
        ws.send_json({"type": "cancel"})

        assert ws.receive_json()["type"] == "cancelled"


def test_codex_opens_sandbox_network_when_the_gate_screens_calls(tmp_path):
    adapter = CodexAdapter(
        workspace_dir=str(tmp_path), env={}, hooks_enabled=True, sandbox="workspace-write"
    )

    args = adapter._build_args("hi", None)

    # Otherwise an egress command the user just approved at the gate still
    # fails inside the sandbox, moving the real decision away from the gate.
    assert "sandbox_workspace_write.network_access=true" in args


def test_codex_leaves_sandbox_network_closed_without_the_gate(tmp_path):
    adapter = CodexAdapter(workspace_dir=str(tmp_path), env={}, sandbox="workspace-write")

    args = adapter._build_args("hi", None)

    assert "sandbox_workspace_write.network_access=true" not in args


def test_codex_without_a_sandbox_mode_still_runs_the_hook(tmp_path):
    # Inside Docker codex's own sandbox cannot start (bubblewrap needs
    # unprivileged user namespaces), so no sandbox mode is configured there.
    # The bypass flag turns off codex's sandbox and prompts, NOT its hooks —
    # the gate still screens every call.
    adapter = CodexAdapter(workspace_dir=str(tmp_path), env={}, hooks_enabled=True)

    args = adapter._build_args("hi", None)

    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--dangerously-bypass-hook-trust" in args


def test_plan_mode_reaches_the_graph(client):
    """A regression guard: the flag has to survive parse → queue → astream, and
    a missing key in the astream payload silently disables planning."""
    import apps.server.app.graph as graph_module

    seen: list = []
    original = graph_module.call_lead_agent

    async def spy(state, config):
        seen.append(state.get("plan_mode"))
        return await original(state, config)

    with patch.object(graph_module, "call_lead_agent", spy):
        # The graph is compiled at startup, so patch the node and rebuild it
        # for this client.
        client.app.state.graph = graph_module.build_graph(client.app.state.checkpointer)
        with client.websocket_connect("/ws/chat?thread_id=plan-flag") as ws:
            ws.receive_json()
            ws.send_json({"prompt": "hi", "agent": "claude", "plan_mode": True})
            while True:
                event = ws.receive_json()
                if event["type"] in ("result", "error"):
                    break

    assert seen == [True]
