"""The MCP server registry.

Reads config/mcp.yaml and answers one question: *which* MCP servers does an
agent with this role get, with every placeholder already substituted. Nothing
here spawns anything — the CLIs do that themselves once they are handed the
server list (see packages/mcp/config_writer.py).

Kept apart from packages/governance/rules.py on purpose: that file decides
whether a tool call may run, this one decides which tools exist at all. Stage
6's "Scope Partitioning" is this file plus the gate check in
packages/mcp/scope.py — the config is the fence, the gate is the guard.
"""
import os
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import yaml

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "mcp.yaml"

LEAD = "lead"
WORKER = "worker"

_SECRET_RE = re.compile(r"\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class MCPServer:
    """One entry of config/mcp.yaml, as written."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    roles: tuple[str, ...] = (WORKER,)
    enabled: bool = True
    requires_env: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ResolvedServer:
    """One entry with placeholders substituted, ready to be spawned."""

    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    description: str = ""

    @property
    def secret_env_names(self) -> tuple[str, ...]:
        """Env keys whose values came from a registered secret. Those never go
        into argv or into a world-readable file (see launcher.py)."""
        return tuple(self.env)


@dataclass(frozen=True)
class SkippedServer:
    name: str
    reason: str


class Registry:
    def __init__(self, servers: list[MCPServer]):
        self.servers = servers

    @classmethod
    def from_yaml(cls, path: os.PathLike | str) -> "Registry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        servers: list[MCPServer] = []
        for entry in raw.get("servers", []):
            name = entry.get("name")
            if not name:
                raise ValueError("every MCP server entry needs a name")
            command = entry.get("command")
            if not command:
                raise ValueError(f"MCP server {name!r} has no command")
            servers.append(
                MCPServer(
                    name=str(name),
                    command=str(command),
                    args=tuple(str(a) for a in entry.get("args", [])),
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    roles=tuple(str(r) for r in entry.get("roles", [WORKER])),
                    enabled=bool(entry.get("enabled", True)),
                    requires_env=tuple(str(s) for s in entry.get("requires_env", [])),
                    description=str(entry.get("description", "")),
                )
            )
        names = [server.name for server in servers]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate MCP server names: {', '.join(sorted(duplicates))}")
        return cls(servers)

    def names(self) -> tuple[str, ...]:
        return tuple(server.name for server in self.servers)

    def secret_names(self) -> tuple[str, ...]:
        """Every secret name the registry refers to, from `requires_env` and
        from `{secret:NAME}` placeholders. The caller looks these up and hands
        back only those values, so a placeholder typo resolves to nothing
        rather than to some unrelated environment variable."""
        names: list[str] = []
        for server in self.servers:
            names.extend(server.requires_env)
            for value in server.env.values():
                names.extend(_SECRET_RE.findall(value))
            for arg in server.args:
                names.extend(_SECRET_RE.findall(arg))
        return tuple(dict.fromkeys(names))

    def names_for(self, role: str) -> tuple[str, ...]:
        """Every server this role could get, ignoring availability. What the
        gate uses: a call to a server the role is not scoped for is a denial
        whether or not that server happens to be running."""
        return tuple(server.name for server in self.servers if role in server.roles)

    def resolve(
        self,
        role: str,
        *,
        workspace_dir: str = "",
        database_url: str = "",
        state_dir: str = "",
        secrets: Optional[dict[str, str]] = None,
        check_command: bool = True,
    ) -> tuple[list[ResolvedServer], list[SkippedServer]]:
        """The servers this role actually gets, and why the others were left out.

        Skips rather than raises throughout: an MCP server that cannot start is
        a missing capability, never a reason for the turn to fail."""
        secrets = secrets or {}
        resolved: list[ResolvedServer] = []
        skipped: list[SkippedServer] = []
        for server in self.servers:
            if not server.enabled:
                skipped.append(SkippedServer(server.name, "설정에서 비활성화됨"))
                continue
            if role not in server.roles:
                skipped.append(SkippedServer(server.name, f"{role} 역할 범위 밖"))
                continue
            missing = [name for name in server.requires_env if not secrets.get(name)]
            if missing:
                skipped.append(
                    SkippedServer(server.name, f"시크릿 미등록: {', '.join(missing)}")
                )
                continue
            if check_command and shutil.which(server.command) is None:
                skipped.append(
                    SkippedServer(server.name, f"실행 파일 없음: {server.command}")
                )
                continue
            substitute = _substituter(
                workspace=workspace_dir,
                database_url=database_url,
                state_dir=state_dir,
                secrets=secrets,
            )
            try:
                args = tuple(substitute(arg) for arg in server.args)
                env = {key: substitute(value) for key, value in server.env.items()}
            except KeyError as exc:  # unresolvable {secret:NAME} outside requires_env
                skipped.append(SkippedServer(server.name, f"치환 실패: {exc.args[0]}"))
                continue
            resolved.append(
                ResolvedServer(
                    name=server.name,
                    command=server.command,
                    args=args,
                    env=env,
                    description=server.description,
                )
            )
        return resolved, skipped


def _substituter(*, workspace: str, database_url: str, state_dir: str, secrets: dict[str, str]):
    """Placeholder substitution done by hand rather than with str.format():
    server arguments legitimately contain braces (JSON fragments, jq filters),
    and format() would choke on them or eat them."""
    literals = {
        "{workspace}": workspace,
        "{database_url}": database_url,
        "{state_dir}": state_dir,
    }

    def substitute(text: str) -> str:
        for placeholder, value in literals.items():
            text = text.replace(placeholder, value)

        def secret(match: re.Match) -> str:
            name = match.group(1)
            if name not in secrets:
                raise KeyError(name)
            return secrets[name]

        return _SECRET_RE.sub(secret, text)

    return substitute


@lru_cache(maxsize=4)
def _cached_registry(path: str, mtime: float) -> Registry:
    return Registry.from_yaml(path)


def load_registry(path: os.PathLike | str | None = None) -> Registry:
    """Loads the registry, re-reading it whenever the file changes — same
    contract as governance rules: adding a server is a file edit, not a
    restart. A missing or broken file yields an empty registry, since "no MCP
    servers" is a working system and a raised exception here would take every
    turn down."""
    resolved = str(path or os.environ.get("ANA_MCP_REGISTRY") or DEFAULT_REGISTRY_PATH)
    try:
        return _cached_registry(resolved, os.path.getmtime(resolved))
    except (OSError, ValueError, yaml.YAMLError):
        return Registry([])


def secrets_for(names: Iterable[str], env: Optional[dict[str, str]]) -> dict[str, str]:
    """The subset of an agent environment that MCP entries may reference.

    Registered secrets arrive in the adapter's environment already; this picks
    out the ones a server asked for by name instead of handing the whole
    environment to the substituter."""
    env = env or {}
    return {name: env[name] for name in names if name in env}
