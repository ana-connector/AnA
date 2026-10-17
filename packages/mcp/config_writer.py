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
import hashlib
from pathlib import Path
from typing import Sequence

from packages.governance import atomic
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
    token = next((s.env.get("ANA_GATE_TOKEN") for s in servers if s.name == "ask"), None)
    suffix = f".{hashlib.sha256(token.encode()).hexdigest()}" if token else ""
    return atomic.write_text(
        directory / f"claude.{role}{suffix}.json", json.dumps(payload, indent=2)
    )


def write_kimi_mcp_config(
    servers: Sequence[ResolvedServer],
    state_dir: os.PathLike | str,
    kimi_home: os.PathLike | str,
) -> Path:
    """Writes `$KIMI_CODE_HOME/mcp.json`, which Kimi reads at session start.

    Same `mcpServers` shape as the Claude config file, so the launcher
    indirection carries over unchanged. Kimi has no --strict flag, but ANA's
    prepared home *is* the user level: nothing else is loaded from it, and a
    project-level `.kimi-code/mcp.json` inside the workspace still has to get
    past the gate's scope check."""
    launchers = write_launchers(servers, state_dir)
    payload = {
        "mcpServers": {
            server.name: {"command": str(launchers[server.name]), "args": []}
            for server in servers
        }
    }
    path = Path(kimi_home)
    path.mkdir(parents=True, exist_ok=True)
    return atomic.write_text(path / "mcp.json", json.dumps(payload, indent=2))


def write_cursor_mcp_config(
    servers: Sequence[ResolvedServer],
    state_dir: os.PathLike | str,
    cursor_home: os.PathLike | str,
) -> Path:
    """Writes `$HOME/.cursor/mcp.json` for a prepared Cursor Agent HOME.

    Same `mcpServers` shape as Claude/Kimi. Cursor has no --strict-mcp-config;
    the prepared HOME is the user level, so this file is the whole list
    (plus any project `.cursor/mcp.json` inside the workspace)."""
    launchers = write_launchers(servers, state_dir)
    payload = {
        "mcpServers": {
            server.name: {"command": str(launchers[server.name]), "args": []}
            for server in servers
        }
    }
    path = Path(cursor_home) / ".cursor"
    path.mkdir(parents=True, exist_ok=True)
    return atomic.write_text(path / "mcp.json", json.dumps(payload, indent=2))


def write_opencode_mcp_config(
    servers: Sequence[ResolvedServer],
    state_dir: os.PathLike | str,
    opencode_home: os.PathLike | str,
) -> Path:
    """Writes the overlay config OPENCODE_CONFIG points at, and returns it.

    OpenCode keeps MCP servers inside its own config file rather than in a file
    of their own, under an `mcp` key whose entries are
    `{"type": "local", "command": [...]}` — one list, not a `command`/`args`
    pair as in the Claude/Kimi/Cursor shape. The command is still the launcher
    script, so no secret reaches the file.

    This is an *additional* config (`OPENCODE_CONFIG=<path>`): the user's own
    config still loads from OPENCODE_CONFIG_DIR, which is where their
    providers, keys and plugins live. Written even when the list is empty, so
    that an earlier turn's servers are replaced rather than left behind."""
    launchers = write_launchers(servers, state_dir)
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            server.name: {
                "type": "local",
                "command": [str(launchers[server.name])],
                "enabled": True,
            }
            for server in servers
        },
    }
    path = Path(opencode_home) / ".config" / "opencode"
    path.mkdir(parents=True, exist_ok=True)
    return atomic.write_text(path / "ana.json", json.dumps(payload, indent=2))


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
