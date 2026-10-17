import asyncio
import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from packages.adapters.claude_code import ClaudeCodeAdapter, supports_permission_prompts
from packages.adapters.codex import CodexAdapter
from packages.adapters.cursor import CursorAdapter, default_executable as cursor_executable
from packages.adapters.opencode import (
    OpenCodeAdapter,
    default_executable as opencode_executable,
)
from packages.adapters.kimi import KimiAdapter, default_executable as kimi_executable
from packages.adapters.env_builder import (
    EnvBuilder,
    build_codex_env,
    build_cursor_env,
    build_opencode_env,
    build_kimi_env,
    default_codex_effort_from_settings,
    default_codex_model_from_settings,
    default_codex_sandbox_from_settings,
    default_cursor_model_from_settings,
    default_opencode_effort_from_settings,
    default_opencode_model_from_settings,
    default_effort_from_settings,
    default_kimi_effort_from_settings,
    default_kimi_model_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
    with_gate,
)
from packages.adapters.transcript import TranscriptLogger
from packages.governance.cli_config import (
    prepare_codex_home,
    prepare_cursor_home,
    prepare_kimi_home,
    prepare_opencode_home,
    write_claude_settings,
)
from packages.governance.secrets import load_secrets
from packages.governance.subagent_scope import Scope, with_instructions
from packages.mcp.wiring import (
    claude_mcp,
    codex_mcp,
    cursor_mcp,
    kimi_mcp,
    opencode_mcp,
)
from packages.skills.catalog import with_catalog
from packages.governance.workspace import session_workspace, workspace_root
from packages.orchestrator.containers import (
    ContainerSpec,
    containers_enabled,
    default_auth_mounts,
    ensure_running,
    exec_prefix,
    parse_ports,
)
from apps.server.app.runners import (
    RunnerRegistry,
    TurnRunner,
    background_turns_enabled,
)
from packages.orchestrator.workspaces import ensure_workspace
from packages.observability.metrics import active_sessions
from packages.orchestrator.handoff import AGENTS
from packages.orchestrator.routing import AUTO
from apps.server.app.delegation import DelegationManager, run_routed_task
from packages.orchestrator.settings import (
    apply_workspace_prompt,
    commit_co_author_trailer,
    enabled_agents,
    read_auth_mode,
    system_prompt_for,
)
from packages.orchestrator.timeline import TurnTimeline

router = APIRouter()

WORKSPACE_DIR = os.environ.get("ANA_WORKSPACE_DIR", "/home/hello/workspace")


def allowed_origins() -> set[str]:
    """Reads ANA_ALLOWED_ORIGINS fresh on every call (rather than caching at
    import time) so tests can monkeypatch it per-test and so a running
    server picks up an updated env var without a code change."""
    raw = os.environ.get("ANA_ALLOWED_ORIGINS", "http://localhost:3000")
    return {origin.strip() for origin in raw.split(",") if origin.strip()}


ANA_STATE_DIR = os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana"))
GATE_URL = os.environ.get("ANA_GATE_URL", "http://127.0.0.1:8000")
CODEX_SOURCE_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
# The user's own Kimi data directory, which ANA copies its config from and
# links credentials to (see prepare_kimi_home).
KIMI_SOURCE_HOME = os.environ.get("KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code"))
# Cursor Agent login session directory (`auth.json`), symlinked into the
# prepared HOME (see prepare_cursor_home).
CURSOR_SOURCE_CONFIG = os.environ.get(
    "CURSOR_CONFIG_DIR", os.path.expanduser("~/.config/cursor")
)
# OpenCode splits the two: credentials and sessions live in the data directory,
# while providers, keys and plugins live in the config directory. ANA symlinks
# the first into its prepared HOME (see prepare_opencode_home) and points the
# CLI back at the second, which it reads as it is.
OPENCODE_SOURCE_DATA = os.environ.get(
    "OPENCODE_DATA_DIR", os.path.expanduser("~/.local/share/opencode")
)
OPENCODE_SOURCE_CONFIG = os.environ.get(
    "OPENCODE_CONFIG_DIR", os.path.expanduser("~/.config/opencode")
)
# Image and network a workspace container gets when it does not name its own.
# The server image already carries both CLIs, so it is the sane default.
CONTAINER_IMAGE_DEFAULT = os.environ.get("ANA_WORKSPACE_IMAGE", "ana-server:latest")
CONTAINER_NETWORK = os.environ.get("ANA_WORKSPACE_NETWORK") or None
# Named volume holding ANA_STATE_DIR, shared with workspace containers so the
# CLI settings file (with the gate hook) and the prepared CODEX_HOME are the
# same files inside and outside.
CONTAINER_STATE_VOLUME = os.environ.get("ANA_STATE_VOLUME") or None
# The gate URL *as seen from inside a workspace container*. The server's own
# ANA_GATE_URL points at itself (127.0.0.1), which inside another container is
# that container — the hook then fails closed with "connection refused".
CONTAINER_GATE_URL = os.environ.get("ANA_CONTAINER_GATE_URL", "http://server:8000")


def planning_enabled() -> bool:
    """Use the extra planning call only when explicitly enabled.

    Ordinary turns run directly with tools. Planning remains available for
    requests that benefit from ANA's isolated worker worktrees.
    """
    return os.environ.get("ANA_PLANNER", "off").lower() == "on"


def gate_enabled() -> bool:
    """ANA_GOVERNANCE=off runs the CLIs the old way (blanket approval), for
    debugging a problem that is clearly not about the gate. Anything else —
    including the variable being unset — keeps the gate on, so forgetting to
    set it never silently disables governance."""
    return os.environ.get("ANA_GOVERNANCE", "on").lower() != "off"


def build_adapter(
    gate_token: Optional[str] = None,
    auth_mode: Optional[str] = None,
    extra_env: Optional[dict] = None,
    workspace_dir: Optional[str] = None,
    command_prefix: Optional[list] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    role: str = "lead",
    scope: Optional[Scope] = None,
):
    # "pro" unless the caller asks for the key path explicitly (that caller is
    # the `tapie` agent). The stored setting is still honoured so an existing
    # installation that ran on the key keeps working.
    mode = auth_mode or read_auth_mode()
    # Registered secrets are part of the environment the builder reads, not
    # something layered on afterwards: TAPIE's key is a secret like any other,
    # and a builder that only looked at os.environ refused to build the agent
    # whose key had just been entered in the secrets page.
    env = EnvBuilder(mode).build({**os.environ, **(extra_env or {})})
    # They stay as environment variables so the agent can use a credential
    # without it ever appearing in a chat message — and so the gate's leak
    # scan has a value to recognize on the way out.
    env.update(extra_env or {})
    # The gate refuses tool calls from the lead: the lead plans and answers,
    # sub-agents do the work (see apps/server/app/gate.py).
    env["ANA_GATE_ROLE"] = role
    # Empty for an unscoped agent, so the lead's environment is unchanged.
    env.update((scope or Scope()).to_env())
    settings_path = None
    permission_prompts = None
    if gate_token is not None and gate_enabled():
        env = with_gate(env, GATE_URL, gate_token)
        settings_path = str(
            write_claude_settings(
                ANA_STATE_DIR,
                python_executable=sys.executable,
                # The same trailer the role instructions ask every CLI for.
                commit_attribution=commit_co_author_trailer(),
            )
        )
        # Only when this build has the flag; passing it to a build without it
        # aborts the run outright.
        permission_prompts = "none" if supports_permission_prompts() else None
    # MCP servers this role is scoped for, and the skill catalog that names
    # them (roadmap stage 6). The availability probe runs on this host, so it
    # is skipped when the CLI actually runs inside a workspace container —
    # there the container's PATH is what counts, not the server's.
    mcp_config_path, mcp_servers = claude_mcp(
        role,
        env=env,
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        check_command=not command_prefix,
    )
    return ClaudeCodeAdapter(
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        env=env,
        model=model or default_model_from_settings(),
        # The ladder's effort wins when it names one; the environment default
        # is what applies to a rung that does not.
        effort=effort or default_effort_from_settings(),
        settings_path=settings_path,
        permission_prompts=permission_prompts,
        command_prefix=command_prefix,
        system_prompt=with_instructions(
            with_catalog(system_prompt_for("claude", role), role, mcp_servers), scope
        ),
        mcp_config_path=mcp_config_path,
    )


def build_codex_adapter(
    gate_token: Optional[str] = None,
    extra_env: Optional[dict] = None,
    workspace_dir: Optional[str] = None,
    command_prefix: Optional[list] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    role: str = "lead",
    scope: Optional[Scope] = None,
) -> CodexAdapter:
    """Factory mirroring build_adapter(), for the `codex` CLI. Constructing
    it spawns nothing -- the subprocess only starts on run_stream() -- so
    both adapters are built up front per connection and the one the client
    picks per message is used."""
    env = build_codex_env()
    env.update(extra_env or {})
    env["ANA_GATE_ROLE"] = role
    env.update((scope or Scope()).to_env())
    sandbox = default_codex_sandbox_from_settings()
    hooks_enabled = False
    if gate_token is not None and gate_enabled():
        codex_home = prepare_codex_home(
            os.path.join(ANA_STATE_DIR, "codex_home"),
            CODEX_SOURCE_HOME,
            python_executable=sys.executable,
        )
        env = with_gate(env, GATE_URL, gate_token)
        env["CODEX_HOME"] = str(codex_home)
        hooks_enabled = True
    mcp_args, mcp_servers = codex_mcp(
        role,
        env=env,
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        check_command=not command_prefix,
    )
    return CodexAdapter(
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        env=env,
        model=model or default_codex_model_from_settings(),
        effort=effort or default_codex_effort_from_settings(),
        sandbox=sandbox,
        hooks_enabled=hooks_enabled,
        command_prefix=command_prefix,
        system_prompt=with_instructions(
            with_catalog(system_prompt_for("codex", role), role, mcp_servers), scope
        ),
        mcp_args=mcp_args,
    )


def _drop_finished_workers(adapters: dict) -> None:
    """Forgets worker adapters whose subprocess is over.

    They are tracked so a disconnect can terminate them, but a long
    conversation that plans on every turn would otherwise keep every worker it
    ever built — each holding a copy of the environment, including the
    registered secrets — for as long as the socket is open. Only workers are
    dropped: the lead pair is reused turn after turn."""
    for key, adapter in list(adapters.items()):
        if not key.startswith("worker/"):
            continue
        process = getattr(adapter, "_process", None)
        # `None` means "has not started yet", not "is done": workers are built
        # a moment before they run, and dropping one here would make a
        # disconnect during the fan-out leave its subprocess alive.
        if process is not None and process.returncode is not None:
            adapters.pop(key, None)


def build_kimi_adapter(
    gate_token: Optional[str] = None,
    extra_env: Optional[dict] = None,
    workspace_dir: Optional[str] = None,
    command_prefix: Optional[list] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    role: str = "lead",
    scope: Optional[Scope] = None,
) -> KimiAdapter:
    """Factory mirroring the other two, for the `kimi` CLI.

    Kimi keeps everything in a data directory rather than in flags: hooks and
    the effort level live in its `config.toml`, MCP servers in its `mcp.json`.
    ANA therefore prepares a home per effort level — the level cannot be
    passed per run, so two concurrent sub-agents on different levels would
    otherwise rewrite one file under each other."""
    env = build_kimi_env()
    env.update(extra_env or {})
    env["ANA_GATE_ROLE"] = role
    env.update((scope or Scope()).to_env())
    resolved_effort = effort or default_kimi_effort_from_settings()
    home = prepare_kimi_home(
        os.path.join(ANA_STATE_DIR, "kimi_home", resolved_effort or "default"),
        KIMI_SOURCE_HOME,
        python_executable=sys.executable,
        effort=resolved_effort,
    )
    env["KIMI_CODE_HOME"] = str(home)
    if gate_token is not None and gate_enabled():
        env = with_gate(env, GATE_URL, gate_token)
    else:
        # Without a gate the hook would deny every call fail-closed, which for
        # a deliberately ungoverned run is not what the operator asked for.
        home = prepare_kimi_home(
            os.path.join(ANA_STATE_DIR, "kimi_home", "ungated"),
            KIMI_SOURCE_HOME,
            python_executable=sys.executable,
            effort=resolved_effort,
        )
        _strip_hooks(home)
        env["KIMI_CODE_HOME"] = str(home)
    mcp_servers = kimi_mcp(
        role,
        str(home),
        env=env,
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        check_command=not command_prefix,
    )
    return KimiAdapter(
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        env=env,
        executable=kimi_executable(),
        model=model or default_kimi_model_from_settings(),
        command_prefix=command_prefix,
        system_prompt=with_instructions(
            with_catalog(system_prompt_for("kimi", role), role, mcp_servers), scope
        ),
    )


def _strip_hooks(home) -> None:
    """Removes ANA's hook entries from a prepared Kimi config."""
    config = os.path.join(str(home), "config.toml")
    try:
        with open(config, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return
    head = text.split("[[hooks]]")[0]
    with open(config, "w", encoding="utf-8") as handle:
        handle.write(head)


def build_cursor_adapter(
    gate_token: Optional[str] = None,
    extra_env: Optional[dict] = None,
    workspace_dir: Optional[str] = None,
    command_prefix: Optional[list] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    role: str = "lead",
    scope: Optional[Scope] = None,
) -> CursorAdapter:
    """Factory mirroring the others, for the Cursor Agent (`agent`) CLI.

    Auth is the host login under `~/.config/cursor/auth.json`. ANA prepares a
    HOME that symlinks that file and writes MCP servers into `.cursor/mcp.json`.
    Cursor has no PreToolUse hook, so the gate token is still passed for MCP
    tools (ask/delegate) that call back into ANA, while shell/file tools are
    auto-approved via `--force`."""
    env = build_cursor_env()
    env.update(extra_env or {})
    env["ANA_GATE_ROLE"] = role
    env.update((scope or Scope()).to_env())
    home = prepare_cursor_home(
        os.path.join(ANA_STATE_DIR, "cursor_home"),
        CURSOR_SOURCE_CONFIG,
    )
    env["HOME"] = str(home)
    # Keep XDG paths inside the prepared home so auth/config resolution does
    # not escape back to the real user home when HOME alone is not enough.
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    if gate_token is not None and gate_enabled():
        env = with_gate(env, GATE_URL, gate_token)
    mcp_servers = cursor_mcp(
        role,
        str(home),
        env=env,
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        check_command=not command_prefix,
    )
    adapter = CursorAdapter(
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        env=env,
        executable=cursor_executable(),
        model=model or default_cursor_model_from_settings(),
        command_prefix=command_prefix,
        system_prompt=with_instructions(
            with_catalog(system_prompt_for("cursor", role), role, mcp_servers), scope
        ),
    )
    adapter.effort = effort
    return adapter


def build_opencode_adapter(
    gate_token: Optional[str] = None,
    extra_env: Optional[dict] = None,
    workspace_dir: Optional[str] = None,
    command_prefix: Optional[list] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    role: str = "lead",
    scope: Optional[Scope] = None,
) -> OpenCodeAdapter:
    """Factory mirroring the others, for the OpenCode CLI.

    Auth is the host login in OpenCode's data directory. ANA prepares a HOME
    that symlinks it and keeps its own session database there, points
    OPENCODE_CONFIG_DIR back at the user's real config (that is where the
    providers, their keys and any plugins are configured) and OPENCODE_CONFIG
    at the overlay carrying ANA's MCP servers. OpenCode has no PreToolUse hook,
    so the gate token is still passed for MCP tools (ask/delegate) that call
    back into ANA, while shell/file tools are auto-approved via `--auto`."""
    env = build_opencode_env()
    env.update(extra_env or {})
    env["ANA_GATE_ROLE"] = role
    env.update((scope or Scope()).to_env())
    home = prepare_opencode_home(
        os.path.join(ANA_STATE_DIR, "opencode_home"),
        OPENCODE_SOURCE_DATA,
    )
    env["HOME"] = str(home)
    # XDG_DATA_HOME is the one that matters — credentials and the session
    # database are under it — but both are set so nothing resolves back to the
    # real user home when HOME alone is not enough.
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["OPENCODE_CONFIG_DIR"] = OPENCODE_SOURCE_CONFIG
    env["OPENCODE_CONFIG"] = str(home / ".config" / "opencode" / "ana.json")
    if gate_token is not None and gate_enabled():
        env = with_gate(env, GATE_URL, gate_token)
    mcp_servers = opencode_mcp(
        role,
        str(home),
        env=env,
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        check_command=not command_prefix,
    )
    return OpenCodeAdapter(
        workspace_dir=workspace_dir or WORKSPACE_DIR,
        env=env,
        executable=opencode_executable(),
        model=model or default_opencode_model_from_settings(),
        effort=effort or default_opencode_effort_from_settings(),
        command_prefix=command_prefix,
        system_prompt=with_instructions(
            with_catalog(system_prompt_for("opencode", role), role, mcp_servers), scope
        ),
    )


def build_transcript() -> TranscriptLogger:
    """Factory mirroring build_adapter(), rather than constructing
    TranscriptLogger(WORKSPACE_DIR) inline in chat_ws(): WORKSPACE_DIR is
    read once at import time, so a test's monkeypatch.setenv("ANA_WORKSPACE_DIR",
    ...) can never reach an inline construction — only patching this factory
    lets tests redirect transcript writes to a tmp_path instead of polluting
    the real workspace's .ana_transcript.jsonl."""
    return TranscriptLogger(WORKSPACE_DIR)


@dataclass
class ClientFrame:
    """One parsed client message. `kind` is "prompt" for a turn request and
    "cancel" for a request to stop the turn currently in flight."""

    kind: str
    prompt: str = ""
    agent: str = AUTO
    # Whether the lead is asked to judge the turn (direct answer vs. a plan of
    # sub-agent tasks). Not a client choice any more: the default follows
    # ANA_PLANNER, so a bare text frame behaves like a JSON one.
    plan_mode: bool = field(default_factory=planning_enabled)
    # Set on kind == "approval": the gate request being answered.
    request_id: str = ""
    client_id: str = ""
    decision: str = "deny"
    # Set on kind == "answer": what the user replied to the agent's question,
    # one entry per question asked.
    answers: list = field(default_factory=list)


def _parse_client_frame(data: str) -> ClientFrame:
    """Parses one client frame.

    The protocol is JSON — {"prompt": ..., "agent": "claude"|"codex"|"kimi"|"tapie"|"cursor"|"opencode"} for a
    turn, {"type": "cancel"} to stop the in-flight one, {"type": "approval",
    "request_id": ..., "decision": "allow"|"deny"} to answer the governance
    gate — but a bare text frame is still accepted as a prompt, so clients
    written against the original single-agent protocol keep working.

    The agent name is only ever accepted when it names a CLI that exists;
    anything else becomes "auto" and the routing rules decide, because this
    field ends up selecting a subprocess to run."""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return ClientFrame(kind="prompt", prompt=data)
    if not isinstance(payload, dict):
        # A JSON scalar frame (e.g. a bare quoted string) is still a prompt,
        # not a malformed request.
        return ClientFrame(kind="prompt", prompt=data)
    if payload.get("type") == "cancel":
        return ClientFrame(kind="cancel")
    if payload.get("type") == "approval":
        return ClientFrame(
            kind="approval",
            request_id=str(payload.get("request_id") or ""),
            decision="allow" if payload.get("decision") == "allow" else "deny",
        )
    if payload.get("type") == "answer":
        answers = payload.get("answers")
        return ClientFrame(
            kind="answer",
            request_id=str(payload.get("request_id") or ""),
            answers=answers if isinstance(answers, list) else [],
        )
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = data
    return ClientFrame(
        kind="prompt",
        prompt=prompt,
        client_id=(payload.get("client_id") if isinstance(payload.get("client_id"), str)
                   and 0 < len(payload["client_id"]) <= 128 else ""),
        # "auto" (or anything unknown) hands the choice to the routing rules;
        # a concrete agent from the client still wins.
        agent=payload.get("agent") if payload.get("agent") in AGENTS else AUTO,
        # Still parsed so an older client's frame is not rejected, but the
        # value no longer decides anything: the lead judges every turn.
        plan_mode=planning_enabled(),
    )


@dataclass
class ConversationSetup:
    """Everything a conversation needs before a runner can be built for it."""

    thread_id: str
    workspace_dir: str
    workspace_name: str
    secret_env: dict
    registered_secrets: list
    container_prefix: Optional[list] = None
    container_spec: object = None
    container_error: str = ""


async def prepare_conversation(app, requested_id: str) -> ConversationSetup:
    """Resolves the workspace, its secrets and its container.

    Factored out of the WebSocket handler because a conversation is no longer
    opened only by a browser: the scheduler starts turns on one nobody is
    watching (packages/orchestrator/schedules.py)."""
    registered_secrets: list = []
    try:
        registered_secrets = await load_secrets(
            app.state.pool, app.state.secret_cipher
        )
        secret_env = {secret.name: secret.value for secret in registered_secrets}
    except Exception:  # noqa: BLE001 - a secret store problem must not take
        # the chat down; the agent simply runs without injected credentials.
        secret_env = {}

    # The workspace record is the source of truth for where this conversation
    # works: it may point at an adopted project directory rather than one under
    # the root. An id nobody registered is adopted rather than rejected.
    container_prefix: Optional[list] = None
    container_spec = None
    container_error = ""
    try:
        workspace = await ensure_workspace(app.state.pool, requested_id)
        thread_id = workspace.id
        workspace_dir = workspace.path
        workspace_name = workspace.name

        if workspace.container_enabled and containers_enabled():
            spec = ContainerSpec(
                workspace_id=workspace.id,
                workspace_path=workspace.path,
                image=workspace.container_image or CONTAINER_IMAGE_DEFAULT,
                ports=parse_ports(list(workspace.container_ports)),
                network=CONTAINER_NETWORK,
                gate_url=CONTAINER_GATE_URL,
                state_volume=CONTAINER_STATE_VOLUME,
                state_path=ANA_STATE_DIR,
                auth_mounts=default_auth_mounts(),
            )
            try:
                await ensure_running(spec)
                # Every CLI for this workspace now runs inside its container.
                # `docker exec` inherits nothing, so everything the CLI needs is
                # passed explicitly — including CODEX_HOME: the Codex CLI keeps
                # its session files there, and a different home inside the
                # container means `codex exec resume` cannot find the rollout it
                # wrote last turn ("no rollout found for thread id …").
                container_spec = spec
                container_prefix = exec_prefix(
                    spec,
                    env={
                        "ANA_GATE_URL": CONTAINER_GATE_URL,
                        "ANA_GATE_TOKEN": "__token__",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - a container that will not
                # start is reported to the user, not a reason to lose the chat:
                # the turn simply runs on the server as before.
                container_error = str(exc)
    except Exception:  # noqa: BLE001 - a registry problem must not take chat
        # down; fall back to the directory-per-thread behavior.
        thread_id = requested_id
        workspace_name = requested_id
        try:
            workspace_dir = str(session_workspace(workspace_root(), thread_id))
        except OSError:
            workspace_dir = WORKSPACE_DIR


    return ConversationSetup(
        thread_id=thread_id,
        workspace_dir=workspace_dir,
        workspace_name=workspace_name,
        secret_env=secret_env,
        registered_secrets=registered_secrets,
        container_prefix=container_prefix,
        container_spec=container_spec,
        container_error=container_error,
    )


async def ensure_runner(app, requested_id: str) -> TurnRunner:
    """The runner for this conversation, built if nobody has one open.

    The registry is the same one the browser uses, so a scheduled turn and a
    typed one share the conversation, its CLI sessions and its gate token."""
    registry: RunnerRegistry = app.state.runners
    registry.start_sweeper()
    existing = registry.get(requested_id)
    if existing is not None:
        return existing
    setup = await prepare_conversation(app, requested_id)
    existing = registry.get(setup.thread_id)
    if existing is not None:
        return existing
    runner = await _build_runner(
        app,
        thread_id=setup.thread_id,
        workspace_dir=setup.workspace_dir,
        workspace_name=setup.workspace_name,
        secret_env=setup.secret_env,
        registered_secrets=setup.registered_secrets,
        container_prefix=setup.container_prefix,
        container_spec=setup.container_spec,
        container_error=setup.container_error,
    )
    registry.put(runner)
    return runner


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    # C1: browsers do not apply same-origin policy to WebSocket handshakes,
    # so any page the user has open in another tab could otherwise drive the
    # lead agent (which runs with --dangerously-skip-permissions). The
    # Origin header is browser-enforced, so its *absence* just means a
    # non-browser client (curl, test scripts) and is not itself suspicious —
    # only a *present but disallowed* Origin is rejected.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in allowed_origins():
        await websocket.close(code=1008)
        return

    # A workspace owns exactly one conversation, so its id *is* the thread id.
    # `thread_id` stays accepted as the older spelling of the same parameter.
    requested_id = (
        websocket.query_params.get("workspace")
        or websocket.query_params.get("thread_id")
        or str(uuid.uuid4())
    )

    # Loaded before the first frame goes out, deliberately. Everything after
    # the thread_id frame must reach the receive loop without awaiting, or a
    # client that disconnects the instant it gets that frame leaves this
    # coroutine parked mid-setup with a subprocess and a gate token that only
    # get cleaned up whenever the task is finally torn down.
    setup = await prepare_conversation(websocket.app, requested_id)
    thread_id = setup.thread_id
    workspace_dir = setup.workspace_dir
    workspace_name = setup.workspace_name
    secret_env = setup.secret_env
    registered_secrets = setup.registered_secrets
    container_prefix = setup.container_prefix
    container_spec = setup.container_spec
    container_error = setup.container_error

    app = websocket.app
    registry: RunnerRegistry = app.state.runners
    registry.start_sweeper()
    active_sessions.inc()

    runner = registry.get(thread_id) if background_turns_enabled() else None

    # Sent before anything can fail: a client that learns the conversation could
    # not be started still needs to know which conversation that was.
    await websocket.send_json(
        {
            "type": "thread_id",
            "text": thread_id,
            "session_id": None,
            "is_error": False,
            # So the UI can show which directory this conversation works in —
            # the isolation is invisible otherwise.
            "workspace": workspace_dir,
            "workspace_id": thread_id,
            "workspace_name": workspace_name,
            # So the UI can say the turn is running inside the workspace's
            # container — or why it is not.
            "container": bool(container_prefix),
            "container_error": container_error,
            # Whether this conversation is mid-turn right now. A client that
            # left and came back has to know before it renders anything, or it
            # shows an idle composer over a turn that is still running.
            "running": runner is not None and runner.busy,
            # The server's clock, so the browser can read every timestamp the
            # server sends without trusting the two to agree. Sent fresh on
            # every connection, which is what makes a replayed status frame
            # resolve to the right elapsed time.
            "server_now": time.time(),
        }
    )

    if runner is None:
        try:
            runner = await _build_runner(
                app,
                thread_id=thread_id,
                workspace_dir=workspace_dir,
                workspace_name=workspace_name,
                secret_env=secret_env,
                registered_secrets=registered_secrets,
                container_prefix=container_prefix,
                container_spec=container_spec,
                container_error=container_error,
            )
        except Exception as exc:  # noqa: BLE001 - build_adapter() raises on a
            # real misconfiguration (no API key in "api" mode). Say so and
            # close, rather than leaving the client with a dead socket.
            active_sessions.dec()
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
                )
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
            return
        registry.put(runner)

    missed = runner.attach(websocket.send_json)

    if missed:
        # What happened while nobody was watching, in one frame so the client
        # can tell a replay from live output: the prompt it never saw, the text
        # so far, the tool calls, the plan, an approval still waiting.
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {
                    "type": "replay",
                    "text": "",
                    "session_id": None,
                    "is_error": False,
                    "frames": missed,
                }
            )

    gate_registry = app.state.gate_registry
    try:
        while True:
            receive_task = asyncio.ensure_future(websocket.receive_text())
            failure_task = asyncio.ensure_future(runner.failed.wait())
            done, pending = await asyncio.wait(
                {receive_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if failure_task in done:
                # The relay is gone: close so the client sees the connection
                # drop instead of queuing messages nobody will answer. The
                # runner is dropped, so the next connection builds a fresh one.
                with contextlib.suppress(Exception):
                    await websocket.close(code=1011)
                await registry.drop(thread_id)
                break
            data = await receive_task
            frame = _parse_client_frame(data)
            if frame.kind == "approval":
                # Answers a pending gate request; the gate endpoint is what is
                # actually waiting on this, not the turn loop.
                # "user" is what the audit row records for the browser; a
                # phone answers the same request as "discord:<user id>".
                gate_registry.resolve_approval(
                    runner.gate_session, frame.request_id, frame.decision, by="user"
                )
                # The buffered card is dropped by the runner's on_resolved hook,
                # which runs for whichever side answered first.
                continue
            if frame.kind == "answer":
                # Same shape one level up: /gate/ask is parked on this, and the
                # agent's own tool call is parked on /gate/ask.
                gate_registry.resolve_answer(
                    runner.gate_session, frame.request_id, frame.answers, by="user"
                )
                continue
            if frame.kind == "cancel":
                if not runner.cancel_current():
                    # No turn is in flight; queued prompts were discarded, so
                    # acknowledge the cancellation to reset the client.
                    with contextlib.suppress(Exception):
                        await websocket.send_json(
                            {
                                "type": "cancelled",
                                "text": "",
                                "session_id": None,
                                "is_error": False,
                            }
                        )
                continue
            # Queued rather than run inline: concurrent prompts are processed in
            # order instead of interrupting the turn in flight. Cancel keeps
            # queued prompts when a turn is running and discards them otherwise.
            accepted = await runner.submit(frame.prompt, frame.agent, frame.plan_mode, frame.client_id)
            if frame.client_id:
                await websocket.send_json({"type": "prompt_ack", "client_id": frame.client_id,
                                           "accepted": accepted})
    except WebSocketDisconnect:
        pass
    finally:
        active_sessions.dec()
        runner.detach(websocket.send_json)
        # The turn keeps running: that is the whole point. What used to happen
        # here — cancel the relay, terminate both CLIs, invalidate the gate
        # token — now happens when the runner has been idle and unwatched for
        # ANA_RUNNER_IDLE_SECONDS, or immediately when background turns are off.
        if not background_turns_enabled():
            await registry.drop(thread_id)


async def _build_runner(
    app,
    *,
    thread_id: str,
    workspace_dir: str,
    workspace_name: str,
    secret_env: dict,
    registered_secrets: list,
    container_prefix: Optional[list],
    container_spec,
    container_error: str,
) -> TurnRunner:
    """Everything one conversation needs, built once and reused by every
    connection that opens it.

    The gate token is registered here rather than per connection, because the
    CLI subprocesses hold it in their environment: a token that died with the
    browser would leave a still-running turn unable to clear the gate."""
    gate_registry = app.state.gate_registry
    gate_session = gate_registry.register(
        thread_id, _no_sink, workspace=workspace_dir
    )

    adapters: dict = {}
    # Which CLIs the settings page has left switched on. A CLI that is off is
    # not built at all: building one writes a prepared home and an MCP config
    # for a CLI the router and the failover chain will both skip. "claude"
    # stays whatever the setting says — it is the default agent, and having
    # nothing to route to is worse than ignoring the preference for one CLI.
    allowed = set(enabled_agents()) | {"claude"}
    try:
        prefix = (
            [part.replace("__token__", gate_session.token) for part in container_prefix]
            if container_prefix
            else None
        )
        adapter = build_adapter(
            gate_session.token,
            extra_env=secret_env,
            workspace_dir=workspace_dir,
            command_prefix=prefix,
        )
        codex_adapter = None
        if "codex" in allowed:
            codex_adapter = build_codex_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                # The Codex side needs its home inside the container too.
                command_prefix=(
                    exec_prefix(
                        container_spec,
                        env={
                            "ANA_GATE_URL": CONTAINER_GATE_URL,
                            "ANA_GATE_TOKEN": gate_session.token,
                            "CODEX_HOME": os.path.join(ANA_STATE_DIR, "codex_home"),
                        },
                    )
                    if container_spec is not None
                    else None
                ),
            )
        kimi_adapter = None
        if "kimi" in allowed:
            kimi_adapter = build_kimi_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                # Kimi reads hooks, effort and MCP servers out of its home, so the
                # container needs to see the same prepared directory.
                command_prefix=(
                    exec_prefix(
                        container_spec,
                        env={
                            "ANA_GATE_URL": CONTAINER_GATE_URL,
                            "ANA_GATE_TOKEN": gate_session.token,
                            "KIMI_CODE_HOME": os.path.join(
                                ANA_STATE_DIR,
                                "kimi_home",
                                default_kimi_effort_from_settings() or "default",
                            ),
                        },
                    )
                    if container_spec is not None
                    else None
                ),
            )
        cursor_home = os.path.join(ANA_STATE_DIR, "cursor_home")
        cursor_adapter = None
        if "cursor" in allowed:
            cursor_adapter = build_cursor_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                command_prefix=(
                    exec_prefix(
                        container_spec,
                        env={
                            "ANA_GATE_URL": CONTAINER_GATE_URL,
                            "ANA_GATE_TOKEN": gate_session.token,
                            "HOME": cursor_home,
                            "XDG_CONFIG_HOME": os.path.join(cursor_home, ".config"),
                        },
                    )
                    if container_spec is not None
                    else None
                ),
            )
        opencode_home = os.path.join(ANA_STATE_DIR, "opencode_home")
        opencode_adapter = None
        if "opencode" in allowed:
            opencode_adapter = build_opencode_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                command_prefix=(
                    exec_prefix(
                        container_spec,
                        env={
                            "ANA_GATE_URL": CONTAINER_GATE_URL,
                            "ANA_GATE_TOKEN": gate_session.token,
                            "HOME": opencode_home,
                            "XDG_DATA_HOME": os.path.join(opencode_home, ".local", "share"),
                            "XDG_CONFIG_HOME": os.path.join(opencode_home, ".config"),
                            "OPENCODE_CONFIG_DIR": "/home/ana/.config/opencode",
                            "OPENCODE_CONFIG": os.path.join(
                                opencode_home, ".config", "opencode", "ana.json"
                            ),
                        },
                    )
                    if container_spec is not None
                    else None
                ),
            )
        transcript = build_transcript()
        adapters.update(
            {
                name: built
                for name, built in (
                    ("claude", adapter),
                    ("codex", codex_adapter),
                    ("kimi", kimi_adapter),
                    ("cursor", cursor_adapter),
                    ("opencode", opencode_adapter),
                )
                if built
            }
        )
        # TAPIE is the same CLI on a key rather than the subscription. Built
        # here so a rung that names it costs nothing extra at turn time, and
        # skipped without comment when no key is configured: an installation
        # without one simply has three agents, not a broken fourth.
        if "tapie" in allowed:
            try:
                adapters["tapie"] = build_adapter(
                    gate_session.token,
                    auth_mode="api",
                    extra_env=secret_env,
                    workspace_dir=workspace_dir,
                    command_prefix=prefix,
                )
            except Exception:  # noqa: BLE001 - no key configured
                pass
    except Exception:
        # Nothing to attach to: drop the token and let the caller report it.
        gate_registry.unregister(gate_session.token)
        for built in list(adapters.values()):
            with contextlib.suppress(Exception):
                await built.terminate()
        raise

    def adapter_factory(
        agent: str,
        auth_mode: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ):
        """Builds the adapter for one failover rung.

        The pre-built trio covers the normal case; a Claude rung on the *other*
        auth mode needs a new adapter, since the mode is baked into the
        subprocess environment. Adapters built here are tracked so closing the
        runner terminates their subprocesses too."""
        if agent == "claude" and auth_mode and auth_mode != read_auth_mode():
            extra = build_adapter(
                gate_session.token,
                auth_mode=auth_mode,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                model=model,
                effort=effort,
            )
            adapters[f"claude/{auth_mode}"] = extra
            return extra
        chosen = adapters.get(agent)
        if chosen is None:
            # A rung naming an agent this installation could not build — TAPIE
            # without a key, so far the only one. Built on demand so the error
            # says which variable is missing rather than KeyError('tapie').
            chosen = adapters[agent] = build_adapter(
                gate_session.token,
                auth_mode="api" if agent == "tapie" else None,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                model=model,
                effort=effort,
            )
        # Assigned even when None: the value is what this rung asked for, and
        # leaving the previous call's setting in place is how the planning
        # call's cheap effort used to leak into the answer.
        chosen.model = model
        chosen.effort = effort
        return chosen

    def worker_factory(
        agent: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        scope: Optional[Scope] = None,
        worker_workspace: Optional[str] = None,
    ):
        """A fresh adapter per sub-agent task. The pre-built trio each hold one
        subprocess handle, so several concurrent workers sharing one object
        would overwrite and terminate each other."""
        # role="worker": sub-agents are the ones allowed to touch files and run
        # commands, and the gate reads that from their environment.
        if agent == "codex":
            built = build_codex_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=worker_workspace or workspace_dir,
                model=model,
                effort=effort,
                role="worker",
                scope=scope,
            )
        elif agent == "kimi":
            built = build_kimi_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=worker_workspace or workspace_dir,
                model=model,
                effort=effort,
                role="worker",
                scope=scope,
            )
        elif agent == "cursor":
            built = build_cursor_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=worker_workspace or workspace_dir,
                model=model,
                effort=effort,
                role="worker",
                scope=scope,
            )
        elif agent == "opencode":
            built = build_opencode_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=worker_workspace or workspace_dir,
                model=model,
                effort=effort,
                role="worker",
                scope=scope,
            )
        else:
            built = build_adapter(
                gate_session.token,
                # Same CLI as claude, different account.
                auth_mode="api" if agent == "tapie" else None,
                extra_env=secret_env,
                workspace_dir=worker_workspace or workspace_dir,
                model=model,
                effort=effort,
                role="worker",
                scope=scope,
            )
        apply_workspace_prompt(built, gate_session.workspace_prompt)
        _drop_finished_workers(adapters)
        adapters[f"worker/{agent}/{uuid.uuid4().hex[:8]}"] = built
        return built

    delegations = DelegationManager(gate_session.active_delegations)

    async def delegate_worker(title: str, prompt: str, task_id: str = "") -> dict:
        async def spawn() -> dict:
            return await run_routed_task(
                title, prompt, worker_factory=worker_factory,
                workspace_dir=workspace_dir, usage_tracker=app.state.usage_tracker,
                native_agents=gate_session.native_agents, send=gate_session.send,
                secrets=registered_secrets,
                # Same clock the gate touches on every tool call: what ends a
                # delegated worker is silence, not a fixed budget.
                activity=gate_session.activity,
                # Names the worker's branch and worktree, so two browser tabs on the
                # same workspace cannot pick the same one.
                thread_id=thread_id,
            )

        return await delegations.request(
            gate_session.turn_generation, title, prompt, spawn, task_id
        )

    gate_session.delegate_worker = delegate_worker

    config = {
        "configurable": {
            "thread_id": thread_id,
            "adapters": adapters,
            "adapter_factory": adapter_factory,
            "worker_factory": worker_factory,
            "gate_session": gate_session,
            "claude_auth_mode": read_auth_mode(),
            # Where this conversation works. The fan-out needs it to give each
            # sub-agent its own git worktree under it.
            "workspace_dir": workspace_dir,
            "tracer": app.state.tracer,
            "usage_tracker": app.state.usage_tracker,
            # Kept alongside "adapters" for callers/tests that still wire a
            # single adapter; graph._resolve_adapter prefers "adapters".
            "adapter": adapters["claude"],
            "pool": app.state.pool,
            "embedding_model": app.state.embedding_model,
            "transcript": transcript,
            # The same registered secrets the gate scans tool calls for. A
            # worker's *output* travels too — into the next task's prompt, the
            # synthesis, the transcript and the turns table — so a credential it
            # echoed would spread through all of them.
            "secrets": registered_secrets,
        }
    }

    return TurnRunner(
        thread_id=thread_id,
        workspace_dir=workspace_dir,
        workspace_name=workspace_name,
        gate_session=gate_session,
        gate_registry=gate_registry,
        adapters=adapters,
        config=config,
        graph=app.state.graph,
        container=bool(container_prefix),
        container_error=container_error,
    )


async def _no_sink(_frame: dict) -> None:
    """The gate session's sink before a runner exists. Replaced immediately by
    TurnRunner, which buffers instead of dropping."""
    return None
