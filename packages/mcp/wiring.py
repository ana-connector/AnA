"""One call each for the two adapter factories.

Everything the factories need to know about MCP lives here, so wiring stage 6
into apps/server/app/websocket.py is one line per adapter rather than a block
of registry handling in a file that is busy with other things.
"""
import os
from typing import Optional

from packages.mcp.config_writer import codex_mcp_args, write_claude_mcp_config
from packages.mcp.registry import (
    ResolvedServer,
    SkippedServer,
    load_registry,
    secrets_for,
)


def state_dir_default() -> str:
    return os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana"))


def mcp_enabled() -> bool:
    """ANA_MCP=off runs the CLIs with no MCP servers at all, the same escape
    hatch ANA_GOVERNANCE=off gives the gate. Read per call so flipping it
    needs no restart."""
    return os.environ.get("ANA_MCP", "on").lower() != "off"


def resolve_for_role(
    role: str,
    *,
    env: Optional[dict] = None,
    workspace_dir: str = "",
    state_dir: Optional[str] = None,
    check_command: bool = True,
) -> tuple[list[ResolvedServer], list[SkippedServer]]:
    """The servers this role gets, resolved against a live environment.

    The DSN comes from the *server's* environment, not the agent's: stage 2's
    DATABASE_URL is deliberately stripped from every agent subprocess
    (packages/adapters/env_builder.py), and the point of the Postgres MCP
    server is to answer questions about that database without giving it back."""
    registry = load_registry()
    if not mcp_enabled():
        return [], [SkippedServer(name, "ANA_MCP=off") for name in registry.names()]
    return registry.resolve(
        role,
        workspace_dir=workspace_dir,
        database_url=os.environ.get("DATABASE_URL", ""),
        state_dir=state_dir or state_dir_default(),
        secrets=secrets_for(registry.secret_names(), env),
        check_command=check_command,
    )


def claude_mcp(
    role: str,
    *,
    env: Optional[dict] = None,
    workspace_dir: str = "",
    state_dir: Optional[str] = None,
    check_command: bool = True,
) -> tuple[Optional[str], set[str]]:
    """`(--mcp-config path, server names)` for a Claude CLI run.

    The path is None when this role gets no servers: passing no flag leaves
    the CLI in its own default, which for a role with nothing scoped is the
    same result with one less moving part. The names go to the skill catalog,
    which only offers skills whose servers are actually there."""
    servers, _ = resolve_for_role(
        role,
        env=env,
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        check_command=check_command,
    )
    names = {server.name for server in servers}
    if not servers:
        return None, names
    path = write_claude_mcp_config(servers, state_dir or state_dir_default(), role)
    return str(path), names


def codex_mcp(
    role: str,
    *,
    env: Optional[dict] = None,
    workspace_dir: str = "",
    state_dir: Optional[str] = None,
    check_command: bool = True,
) -> tuple[list[str], set[str]]:
    """`(-c overrides, server names)` for a `codex exec` run. The overrides are
    empty when nothing is scoped for this role."""
    servers, _ = resolve_for_role(
        role,
        env=env,
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        check_command=check_command,
    )
    names = {server.name for server in servers}
    if not servers:
        return [], names
    return codex_mcp_args(servers, state_dir or state_dir_default()), names


def describe(
    role: str = "worker",
    *,
    env: Optional[dict] = None,
    workspace_dir: str = "",
    check_command: bool = True,
) -> dict:
    """What the settings page shows: which servers this role has, and the
    reason for each one it does not."""
    servers, skipped = resolve_for_role(
        role, env=env, workspace_dir=workspace_dir, check_command=check_command
    )
    return {
        "role": role,
        "enabled": mcp_enabled(),
        "active": [
            {"name": s.name, "description": s.description, "command": s.command}
            for s in servers
        ],
        "inactive": [{"name": s.name, "reason": s.reason} for s in skipped],
    }
