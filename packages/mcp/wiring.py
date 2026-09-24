"""One call each for the two adapter factories.

Everything the factories need to know about MCP lives here, so wiring stage 6
into apps/server/app/websocket.py is one line per adapter rather than a block
of registry handling in a file that is busy with other things.
"""
import os
from typing import Optional

from packages.mcp.config_writer import (
    codex_mcp_args,
    write_claude_mcp_config,
    write_kimi_mcp_config,
)
from packages.mcp.registry import (
    LEAD,
    WORKER,
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


def process_role(role: str):
    """Whose servers the CLI *process* is started with.

    Not the same question as "what may this agent call". A lead CLI process is
    also the process its native sub-agents run in (the CLI's own
    spawn_agent/Task tooling), and those sub-agents are workers — the gate
    sees their calls with an agent_id and judges them as such. Starting the
    lead process with no servers would leave every natively-spawned worker
    without git or postgres, which is exactly what happened the first time
    this ran for real.

    The lead itself is still kept away from them by the gate, which refuses it
    every tool call but delegation, the clock, and asking the user. Flags decide
    what exists; the gate decides who may use it.

    So a lead process gets both sets: the worker servers its native sub-agents
    need, and the servers scoped to the lead itself. Returning only WORKER
    here meant a lead-scoped server could be configured for nobody."""
    return (WORKER, LEAD) if role == LEAD else (role,)


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

    The path is None only when MCP is switched off entirely; otherwise there
    is always a config, even an empty one, because the flag is what keeps the
    CLI from loading servers ANA never granted. The names go to the skill
    catalog, which only offers skills whose servers are actually there."""
    if not mcp_enabled():
        return None, set()
    servers, _ = resolve_for_role(
        process_role(role),
        env=env,
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        check_command=check_command,
    )
    # Written even when the list is empty, and always passed: without
    # --strict-mcp-config the CLI adds whatever MCP servers the host user or
    # the account provides (a live run picked up an account-level server that
    # ANA never granted), and the role scoping would be advisory.
    path = write_claude_mcp_config(servers, state_dir or state_dir_default(), role)
    return str(path), {server.name for server in servers}


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
        process_role(role),
        env=env,
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        check_command=check_command,
    )
    names = {server.name for server in servers}
    if not servers:
        return [], names
    # Codex has no --strict-mcp-config, so its built-in app server still shows
    # up; the gate denies it as unregistered (verified in the audit log).
    return codex_mcp_args(servers, state_dir or state_dir_default()), names


def kimi_mcp(
    role: str,
    kimi_home: str,
    *,
    env: Optional[dict] = None,
    workspace_dir: str = "",
    state_dir: Optional[str] = None,
    check_command: bool = True,
) -> set[str]:
    """Writes this role's servers into the prepared Kimi home and returns
    their names. Always written — an empty `mcpServers` is how ANA says "no
    servers", and leaving the file from a previous session in place would say
    something else."""
    if not mcp_enabled():
        write_kimi_mcp_config([], state_dir or state_dir_default(), kimi_home)
        return set()
    servers, _ = resolve_for_role(
        process_role(role),
        env=env,
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        check_command=check_command,
    )
    write_kimi_mcp_config(servers, state_dir or state_dir_default(), kimi_home)
    return {server.name for server in servers}


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
