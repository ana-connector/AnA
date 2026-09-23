"""Turning a resolved server list into what each CLI understands.

Claude takes a JSON file (`--mcp-config <path> --strict-mcp-config`); Codex
takes config overrides (`-c mcp_servers.<name>.command="..."`). Both point at
the launcher script rather than at the real command, so neither the config
file nor argv carries a secret (see packages/mcp/launcher.py).

`--strict-mcp-config` matters: without it the CLI *also* loads whatever MCP
servers the host user configured for themselves, and the role scoping this
module exists to enforce would be advisory.
"""
import json
import os
from pathlib import Path
from typing import Sequence

from packages.mcp.launcher import write_launchers
from packages.mcp.registry import ResolvedServer

CONFIG_DIRNAME = "mcp"


def write_claude_mcp_config(
    servers: Sequence[ResolvedServer], state_dir: os.PathLike | str, role: str
) -> Path:
    """Writes the `--mcp-config` file for one role and returns its path.

    Written even when the list is empty: an empty `mcpServers` object plus
    --strict-mcp-config is how ANA says "this role gets no MCP servers", which
    is a different statement from passing no flag at all."""
    launchers = write_launchers(servers, state_dir)
    payload = {
        "mcpServers": {
            server.name: {"command": str(launchers[server.name]), "args": []}
            for server in servers
        }
    }
    directory = Path(state_dir) / CONFIG_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"claude.{role}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def codex_mcp_args(
    servers: Sequence[ResolvedServer], state_dir: os.PathLike | str
) -> list[str]:
    """The `-c` overrides that give a `codex exec` run the same servers.

    Values are TOML, hence the quoting. An empty list means no overrides at
    all — Codex then falls back to CODEX_HOME's config.toml, which for ANA's
    prepared home defines no MCP servers."""
    launchers = write_launchers(servers, state_dir)
    args: list[str] = []
    for server in servers:
        command = json.dumps(str(launchers[server.name]))  # TOML string, escaped
        args += ["-c", f"mcp_servers.{server.name}.command={command}"]
        args += ["-c", f"mcp_servers.{server.name}.args=[]"]
    return args
