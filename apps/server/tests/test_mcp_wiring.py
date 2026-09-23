"""From registry to CLI: launcher scripts, config files, and the argv both
adapters end up building."""
import json
import os
import stat
import textwrap

import pytest

from packages.adapters.claude_code import ClaudeCodeAdapter
from packages.adapters.codex import CodexAdapter
from packages.mcp.config_writer import codex_mcp_args, write_claude_mcp_config
from packages.mcp.launcher import write_launcher
from packages.mcp.registry import ResolvedServer
from packages.mcp.wiring import claude_mcp, codex_mcp

SAMPLE = textwrap.dedent(
    """
    servers:
      - name: git
        command: /bin/sh
        args: ["-c", "echo {workspace}"]
        roles: [worker]
      - name: github
        command: /bin/sh
        env:
          TOKEN: "{secret:GITHUB_PERSONAL_ACCESS_TOKEN}"
        requires_env: [GITHUB_PERSONAL_ACCESS_TOKEN]
        roles: [worker]
    """
)


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setenv("ANA_MCP_REGISTRY", str(path))
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ANA_MCP", raising=False)
    return path


def test_launcher_keeps_the_secret_out_of_the_command_line(tmp_path):
    server = ResolvedServer(
        name="github", command="/bin/echo", args=("hi",), env={"TOKEN": "ghp_secret"}
    )
    script = write_launcher(server, tmp_path)

    assert "ghp_secret" not in script.read_text(encoding="utf-8")
    env_file = script.parent / "github.env"
    assert "ghp_secret" in env_file.read_text(encoding="utf-8")
    # Owner-only: the env file is the one place the value sits on disk.
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert os.access(script, os.X_OK)


def test_launcher_quotes_hostile_values(tmp_path):
    server = ResolvedServer(
        name="odd", command="/bin/echo", args=(), env={"V": "a'b\nrm -rf /"}
    )
    script = write_launcher(server, tmp_path)
    env_file = (script.parent / "odd.env").read_text(encoding="utf-8")
    # The value is one shell word, so the newline cannot start a command.
    assert env_file.startswith("export V='a'\"'\"'b\nrm -rf /'")


def test_claude_config_points_at_launchers(tmp_path):
    server = ResolvedServer(name="git", command="/bin/echo", args=("x",), env={})
    path = write_claude_mcp_config([server], tmp_path, "worker")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["git"]["command"] == str(tmp_path / "mcp" / "git")


def test_codex_overrides_are_toml_quoted(tmp_path):
    server = ResolvedServer(name="git", command="/bin/echo", args=(), env={})
    args = codex_mcp_args([server], tmp_path)
    assert args[0] == "-c"
    assert args[1] == f'mcp_servers.git.command="{tmp_path / "mcp" / "git"}"'


def test_claude_adapter_passes_strict_mcp_config(registry_path):
    path, servers = claude_mcp("worker", env={}, workspace_dir="/w")
    assert servers == {"git"}  # github needs a secret nobody registered
    adapter = ClaudeCodeAdapter(
        workspace_dir="/w", env={}, mcp_config_path=path
    )
    args, _ = adapter._build_args("hi", None)
    assert "--mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == path
    # Without --strict-mcp-config the CLI would also load the host user's own
    # servers, and the role scoping would be advisory.
    assert "--strict-mcp-config" in args


def test_codex_adapter_passes_the_overrides_before_the_prompt(registry_path):
    args, servers = codex_mcp("worker", env={}, workspace_dir="/w")
    assert servers == {"git"}
    adapter = CodexAdapter(workspace_dir="/w", env={}, mcp_args=args)
    argv = adapter._build_args("hi", None)
    assert argv[-1] == "hi"
    assert "mcp_servers.git.args=[]" in argv


def test_lead_process_carries_the_worker_servers(registry_path):
    """The lead's CLI process is also the process its native sub-agents run
    in, and those are workers. The lead itself is still refused every tool
    call by the gate — flags decide what exists, the gate decides who uses
    it."""
    path, servers = claude_mcp("lead", env={}, workspace_dir="/w")
    assert servers == {"git"}
    assert path is not None
    args, codex_servers = codex_mcp("lead", env={}, workspace_dir="/w")
    assert codex_servers == {"git"}
    assert args


def test_a_role_with_nothing_scoped_still_gets_a_strict_empty_config(registry_path):
    """Without the flag the CLI loads whatever MCP servers the host user or
    the account provides — a live run picked up an account-level server ANA
    never granted."""
    path, servers = claude_mcp("reviewer", env={}, workspace_dir="/w")
    assert servers == set()
    assert json.loads(open(path, encoding="utf-8").read()) == {"mcpServers": {}}
    args, _ = ClaudeCodeAdapter(
        workspace_dir="/w", env={}, mcp_config_path=path
    )._build_args("hi", None)
    assert "--strict-mcp-config" in args


def test_adapters_pass_no_mcp_flags_when_nothing_is_scoped():
    claude_args, _ = ClaudeCodeAdapter(workspace_dir="/w", env={})._build_args("hi", None)
    assert "--mcp-config" not in claude_args
    assert "--strict-mcp-config" not in claude_args
    codex_args = CodexAdapter(workspace_dir="/w", env={})._build_args("hi", None)
    assert not any(arg.startswith("mcp_servers.") for arg in codex_args)


def test_ana_mcp_off_disables_everything(registry_path, monkeypatch):
    monkeypatch.setenv("ANA_MCP", "off")
    assert claude_mcp("worker", env={}, workspace_dir="/w") == (None, set())


def test_registered_secret_activates_the_server(registry_path):
    _, servers = claude_mcp(
        "worker", env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_x"}, workspace_dir="/w"
    )
    assert servers == {"git", "github"}
