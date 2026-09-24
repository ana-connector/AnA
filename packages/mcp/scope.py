"""Scope partitioning at the gate.

The registry already decides which servers a role is *given*; this decides
what happens when a call arrives for one it was not. Two ways that happens:
the CLI was started before the config changed, or a server reached the agent
some other way (a host-level MCP config, a resumed session). Either way the
answer is the same, and it is made in the same place every other tool call is
judged — see apps/server/app/gate.py.

Tool names differ between the CLIs: Claude reports `mcp__git__git_log`, while
hook payloads elsewhere in this system arrive with the punctuation stripped
(`collaborationspawn_agent`). Both forms are recognized rather than one
assumed.
"""
import re
from typing import Iterable, Optional

MCP_TOOL_RE = re.compile(r"^mcp__(?P<server>[^_]+(?:_[^_]+)*?)__(?P<tool>.+)$")


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def server_for_tool(tool_name: str, known_servers: Iterable[str]) -> Optional[str]:
    """The MCP server a tool call belongs to, or None for a native tool.

    A name that looks like an MCP tool but matches no registered server still
    returns a server name (whatever it claimed), so the gate can deny it
    instead of treating it as a native tool the rules already cover."""
    if not tool_name:
        return None
    match = MCP_TOOL_RE.match(tool_name)
    if match:
        return match.group("server")
    known = list(known_servers)
    # `<server>__<tool>`, without the mcp prefix. Verified for Claude
    # (mcp__git__git_log); Codex's exact hook spelling is unconfirmed, and a
    # form this check missed would fall through to the command rules as if it
    # were a native tool — a scope bypass rather than a visible failure.
    head, separator, rest = tool_name.partition("__")
    if separator and rest and head in known:
        return head
    normalized = _normalize(tool_name)
    if not normalized.startswith("mcp"):
        return None
    remainder = normalized[3:]
    for server in known:
        key = _normalize(server)
        if key and remainder.startswith(key) and len(remainder) > len(key):
            return server
    return None


def scope_denial(tool_name: str, role: str, allowed_servers: Iterable[str],
                 known_servers: Iterable[str]) -> Optional[str]:
    """Why this MCP call must not run, or None if it may.

    `allowed_servers` is what this role is scoped for; `known_servers` is
    every name the registry defines, used only to recognize the call."""
    known = list(known_servers)
    server = server_for_tool(tool_name, known)
    if server is None:
        return None
    allowed = set(allowed_servers)
    if server in allowed:
        return None
    if server not in known:
        return f"tools of unregistered MCP server '{server}' cannot be used"
    return f"MCP server '{server}' is not in scope for the {role} role"
