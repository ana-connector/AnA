# ANA — Per-workspace containers

Date: 2026-09-23
Status: implementing

## Goal

Give each workspace its own container, with its own published ports, and run
that workspace's CLIs inside it. This is the isolation half of roadmap stage 3
that was deferred, now scoped to the unit that actually has an identity: a
workspace, not a chat session.

## Design

### Settings live on the workspace

`workspaces` gains `container_enabled`, `container_image`, `container_ports`
(`[{"host": 5173, "container": 5173}]`) and `container_name`. Editing them is a
`PATCH /workspaces/{id}`; nothing starts until the workspace is used.

### Lifecycle

`packages/orchestrator/containers.py` drives the `docker` CLI:

- `ensure_running(spec)` → starts `ana-ws-<id>` if absent, reusing it if it is
  already up. The container runs `sleep infinity`; the CLIs are started inside
  it per turn with `docker exec`, which matches ANA's one-subprocess-per-turn
  model rather than keeping an agent process alive.
- The workspace directory is bind-mounted at the same path inside, so every
  path the agent, the gate and the audit log talk about means one thing.
- `~/.claude`, `~/.claude.json` and `~/.codex` are mounted read-write, exactly
  as the server container does, so the CLIs are already logged in.
- Ports are published from the spec, which is the point: a dev server started
  by the agent inside the workspace is reachable from the browser.
- `stop(id)` removes it. A stale container from an older spec (different image
  or ports) is replaced rather than reused, since its published ports would be
  wrong.

### Running the CLI inside

Both adapters take a `command_prefix`, prepended to argv. For a containerized
workspace that is `["docker", "exec", "-i", "-w", <path>, "-e", …, <name>]`.
The gate URL becomes reachable from inside the container, so the hook can still
call ANA: the container joins the compose network and uses
`ANA_GATE_URL=http://server:8000`.

### Enabling it at all

ANA needs the Docker socket to do any of this, which is host-root equivalent.
So it is **off unless two things are true**: `ANA_WORKSPACE_CONTAINERS=on` in
the server environment, and the socket is actually mounted. The UI shows the
switch as unavailable otherwise rather than failing at turn time.

## What the build turned up

Four things that only show up against a real daemon:

- **Debian 13 splits the Docker client out of `docker.io`**, which ships the
  daemon and `docker-init` but no `/usr/bin/docker`. The image installs
  `docker-cli`.
- **The socket is root:docker on the host** and the server runs as uid 1000, so
  the container needs the socket's group (`group_add: ["${DOCKER_GID}"]`).
- **Bind mounts are resolved on the host**, not inside the server container:
  `~/.claude` expanded to `/home/ana/.claude`, which does not exist on the
  host, so Docker created an empty directory and every CLI came up "Not logged
  in". The host paths are passed in through the environment now, and they are
  part of the container fingerprint so a container from before the fix is
  replaced instead of reused.
- **The gate URL differs by vantage point**: the server's own is
  `127.0.0.1:8000`, which inside another container is that container. Workspace
  containers get the service name instead, or the hook fails closed with
  "connection refused" on the first tool call.

Verified live: a turn whose `hostname` is the workspace container, its tool
call screened by the gate, and a server started inside answering on the
published host port.

## Verification

- `pytest`: spec/argv construction, reuse vs replace on a changed spec, the
  adapters' `command_prefix`, settings round-trip through the API, and the
  feature staying off without the flag (a fake `docker` binary records argv, so
  no real containers are started in tests).
- Live: a workspace with a published port, an agent starting a server inside
  it, and the port answering from the host.
