"""Per-workspace containers.

A workspace can run its CLIs inside its own container with its own published
ports, so a dev server the agent starts is reachable from the browser and the
work stays inside one box per project.

Driven through the `docker` CLI rather than a library: ANA already shells out
to two CLIs, the surface used here is four commands, and it keeps the
dependency list unchanged.

This needs the Docker socket, which is host-root equivalent — so it is off
unless ANA_WORKSPACE_CONTAINERS=on *and* the socket is actually reachable.
"""
import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

CONTAINER_PREFIX = "ana-ws-"
DEFAULT_IMAGE = "ana-server:latest"


@dataclass(frozen=True)
class PortMapping:
    host: int
    container: int

    def as_dict(self) -> dict:
        return {"host": self.host, "container": self.container}


@dataclass(frozen=True)
class ContainerSpec:
    workspace_id: str
    workspace_path: str
    image: str = DEFAULT_IMAGE
    ports: tuple[PortMapping, ...] = ()
    # Mounts that make the CLIs already-logged-in inside the container.
    #
    # These are **host** paths, not paths inside the server container: the
    # daemon resolves a bind mount on the host, so "~/.claude" expanded inside
    # the server (/home/ana/.claude) would silently create an empty directory
    # on the host and the CLI comes up "Not logged in".
    auth_mounts: tuple[str, ...] = field(default=())
    network: Optional[str] = None
    gate_url: Optional[str] = None
    # ANA's own state (the CLI settings file carrying the PreToolUse hook, and
    # the prepared CODEX_HOME) lives inside the server container, so the
    # workspace container gets it through the same named volume rather than a
    # host path that does not exist.
    state_volume: Optional[str] = None
    state_path: str = "/home/ana/.ana"

    @property
    def name(self) -> str:
        return f"{CONTAINER_PREFIX}{self.workspace_id}"

    def fingerprint(self) -> str:
        """What a running container has to match to be reused. A changed image
        or port set means the running one publishes the wrong things, so it is
        replaced rather than kept."""
        return json.dumps(
            {
                "image": self.image,
                "ports": [p.as_dict() for p in self.ports],
                "path": self.workspace_path,
                # Mounts count too: a container started before the credential
                # paths were fixed looks identical otherwise, and keeps coming
                # up "Not logged in".
                "mounts": list(self.auth_mounts),
                "state": self.state_volume,
            },
            sort_keys=True,
        )


def default_auth_mounts() -> tuple[str, ...]:
    """Host paths for the CLI credentials, as configured for this deployment.

    Read from the environment because only the host knows where its own home
    is; compose passes them in (`${HOME}/.claude`, …)."""
    pairs = (
        (os.environ.get("ANA_HOST_CLAUDE_DIR"), "/home/ana/.claude"),
        (os.environ.get("ANA_HOST_CLAUDE_JSON"), "/home/ana/.claude.json"),
        (os.environ.get("ANA_HOST_CODEX_DIR"), "/home/ana/.codex"),
        (os.environ.get("ANA_HOST_KIMI_DIR"), "/home/ana/.kimi-code"),
    )
    return tuple(f"{host}:{inside}" for host, inside in pairs if host)


def containers_enabled() -> bool:
    """Both conditions, not just the flag: without the socket the flag would
    only move the failure to the first turn."""
    if os.environ.get("ANA_WORKSPACE_CONTAINERS", "off").lower() != "on":
        return False
    if shutil.which("docker") is None:
        return False
    return os.path.exists(os.environ.get("DOCKER_HOST_SOCKET", "/var/run/docker.sock"))


def parse_ports(raw: Optional[list]) -> tuple[PortMapping, ...]:
    """Port mappings as stored on the workspace. Values come from the UI, so
    anything unparseable is dropped rather than passed to `docker run`."""
    mappings: list[PortMapping] = []
    for entry in raw or []:
        try:
            host = int(entry["host"])
            container = int(entry.get("container", entry["host"]))
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= host <= 65535 and 1 <= container <= 65535:
            mappings.append(PortMapping(host=host, container=container))
    return tuple(mappings)


def run_args(spec: ContainerSpec) -> list[str]:
    """argv for starting the workspace's container."""
    args = [
        "docker", "run", "-d",
        "--name", spec.name,
        "--label", f"ana.workspace={spec.workspace_id}",
        "--label", f"ana.fingerprint={spec.fingerprint()}",
        # Same path inside as outside: every path the agent, the gate and the
        # audit log mention then means one thing.
        "-v", f"{spec.workspace_path}:{spec.workspace_path}",
        "-w", spec.workspace_path,
    ]
    if spec.state_volume:
        args += ["-v", f"{spec.state_volume}:{spec.state_path}"]
    for mount in spec.auth_mounts:
        host_path, _, container_path = mount.rpartition(":")
        if host_path and container_path:
            args += ["-v", f"{host_path}:{container_path}"]
    for port in spec.ports:
        args += ["-p", f"{port.host}:{port.container}"]
    if spec.network:
        args += ["--network", spec.network]
    if spec.gate_url:
        args += ["-e", f"ANA_GATE_URL={spec.gate_url}"]
    # The container is a place to exec into, not a process to supervise: ANA
    # still runs one CLI subprocess per turn.
    args += [spec.image, "sleep", "infinity"]
    return args


def exec_prefix(spec: ContainerSpec, env: Optional[dict] = None) -> list[str]:
    """argv prefix that runs a command inside the workspace's container.

    Env vars are passed explicitly because `docker exec` does not inherit the
    caller's environment — and the gate token lives there."""
    prefix = ["docker", "exec", "-i", "-w", spec.workspace_path]
    for key, value in (env or {}).items():
        prefix += ["-e", f"{key}={value}"]
    prefix.append(spec.name)
    return prefix


async def _docker(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def inspect(name: str) -> Optional[dict]:
    code, out, _ = await _docker("docker", "inspect", name)
    if code != 0 or not out:
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed[0] if parsed else None


async def stop(name: str) -> None:
    await _docker("docker", "rm", "-f", name)


async def ensure_running(spec: ContainerSpec) -> str:
    """Starts the workspace's container if needed and returns its name.

    A container from an older spec is replaced: its published ports and image
    are part of what the workspace asked for, and `docker run` cannot change
    them in place."""
    existing = await inspect(spec.name)
    if existing is not None:
        labels = (existing.get("Config") or {}).get("Labels") or {}
        running = (existing.get("State") or {}).get("Running", False)
        if labels.get("ana.fingerprint") == spec.fingerprint():
            if running:
                return spec.name
            code, _, err = await _docker("docker", "start", spec.name)
            if code == 0:
                return spec.name
            raise RuntimeError(f"could not start {spec.name}: {err}")
        await stop(spec.name)

    code, _, err = await _docker(*run_args(spec))
    if code != 0:
        raise RuntimeError(f"could not start {spec.name}: {err}")
    return spec.name


async def stop_workspace(workspace_id: str) -> bool:
    """Removes the container belonging to one workspace, if there is one.

    Called when a workspace is deleted: the container is named after the
    workspace, so leaving it behind means a container (and its published
    ports) outliving the thing that asked for it, and the next workspace that
    happens to reuse the id inherits it. Returns whether a container was
    found."""
    if shutil.which("docker") is None:
        return False
    name = f"{CONTAINER_PREFIX}{workspace_id}"
    if await inspect(name) is None:
        return False
    await stop(name)
    return True
