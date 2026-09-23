import asyncio
import contextlib
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from packages.adapters.claude_code import ClaudeCodeAdapter, supports_permission_prompts
from packages.adapters.codex import CodexAdapter
from packages.adapters.kimi import KimiAdapter, default_executable as kimi_executable
from packages.adapters.env_builder import (
    EnvBuilder,
    build_codex_env,
    build_kimi_env,
    default_codex_effort_from_settings,
    default_codex_model_from_settings,
    default_codex_sandbox_from_settings,
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
    prepare_kimi_home,
    write_claude_settings,
)
from packages.governance.secrets import load_secrets
from packages.governance.subagent_scope import Scope, with_instructions
from packages.mcp.wiring import claude_mcp, codex_mcp, kimi_mcp
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
from packages.orchestrator.workspaces import ensure_workspace
from packages.observability.metrics import active_sessions
from packages.orchestrator.handoff import AGENTS
from packages.orchestrator.routing import AUTO
from packages.orchestrator.settings import read_auth_mode, system_prompt_for
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
    """Whether the lead decides for itself how to handle a turn.

    It always does, unless ANA_PLANNER=off — which exists for debugging a
    problem that is clearly not about planning. There is no "plan mode" for
    the user to pick: the lead has no tools at all, so whether a request needs
    sub-agents is a judgement about the request, not a preference about the
    UI, and asking the user to choose meant they could pick the mode that
    cannot do the job."""
    return os.environ.get("ANA_PLANNER", "on").lower() != "off"


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
    mode = auth_mode or read_auth_mode()
    env = EnvBuilder(mode).build()
    # Registered secrets, injected as environment variables so the agent can
    # use a credential without it ever appearing in a chat message — and so
    # the gate's leak scan has a value to recognize on the way out.
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
            write_claude_settings(ANA_STATE_DIR, python_executable=sys.executable)
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
    decision: str = "deny"


def _parse_client_frame(data: str) -> ClientFrame:
    """Parses one client frame.

    The protocol is JSON — {"prompt": ..., "agent": "claude"|"codex"|"kimi"} for a
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
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = data
    return ClientFrame(
        kind="prompt",
        prompt=prompt,
        # "auto" (or anything unknown) hands the choice to the routing rules;
        # a concrete agent from the client still wins.
        agent=payload.get("agent") if payload.get("agent") in AGENTS else AUTO,
        # Still parsed so an older client's frame is not rejected, but the
        # value no longer decides anything: the lead judges every turn.
        plan_mode=planning_enabled(),
    )


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
    try:
        secret_env = {
            secret.name: secret.value
            for secret in await load_secrets(
                websocket.app.state.pool, websocket.app.state.secret_cipher
            )
        }
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
        workspace = await ensure_workspace(websocket.app.state.pool, requested_id)
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

    gate_registry = websocket.app.state.gate_registry
    gate_session = gate_registry.register(
        thread_id, websocket.send_json, workspace=workspace_dir
    )
    active_sessions.inc()

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
        }
    )

    # build_adapter() defaults to "api" mode (default_mode_from_settings()),
    # where EnvBuilder.build() raises RuntimeError if ANTHROPIC_API_KEY_SAVED
    # isn't set — a real misconfiguration, not a hypothetical. This must be
    # guarded the same way the old build_session() was: send an error frame
    # and close(1011) instead of letting the exception propagate unhandled,
    # which would otherwise leave the client with the thread_id frame and
    # then a dead socket (no error frame, no close).
    # Registered before the adapters are built: the token is what the CLI
    # subprocess's PreToolUse hook presents to the gate, so it has to exist
    # before either adapter's environment is assembled.

    # Populated during setup and read by the teardown below. Setup now awaits
    # the database (registered secrets), so a client that disconnects during
    # setup cancels this coroutine *before* the old inner try/finally existed
    # — cleanup has to live in one block that covers setup as well, or a quick
    # disconnect leaks the gate token and any subprocess already started.
    adapters: dict = {}
    try:
        try:
            prefix = (
                [
                    part.replace("__token__", gate_session.token)
                    for part in container_prefix
                ]
                if container_prefix
                else None
            )
            adapter = build_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                command_prefix=prefix,
            )
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
            kimi_adapter = build_kimi_adapter(
                gate_session.token,
                extra_env=secret_env,
                workspace_dir=workspace_dir,
                # Kimi reads hooks, effort and MCP servers out of its home, so
                # the container needs to see the same prepared directory.
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
            transcript = build_transcript()
            adapters.update(
                {"claude": adapter, "codex": codex_adapter, "kimi": kimi_adapter}
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
                )
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
            return

        graph = websocket.app.state.graph

        def adapter_factory(
            agent: str,
            auth_mode: Optional[str] = None,
            model: Optional[str] = None,
            effort: Optional[str] = None,
        ):
            """Builds the adapter for one failover rung.

            The pre-built pair covers the normal case; a Claude rung on the *other*
            auth mode needs a new adapter, since the mode is baked into the
            subprocess environment. Adapters built here are tracked so a client
            disconnect terminates their subprocesses too."""
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
            adapter = adapters[agent]
            if model is not None:
                adapter.model = model
            if effort is not None:
                adapter.effort = effort
            return adapter

        def worker_factory(
            agent: str,
            model: Optional[str] = None,
            effort: Optional[str] = None,
            scope: Optional[Scope] = None,
            worker_workspace: Optional[str] = None,
        ):
            """A fresh adapter per sub-agent task. The pre-built pair each hold
            one subprocess handle, so several concurrent workers sharing one
            object would overwrite and terminate each other. Tracked in
            `adapters` so a disconnect kills their subprocesses too."""
            # role="worker": sub-agents are the ones allowed to touch files and
            # run commands, and the gate reads that from their environment.
            built = (
                build_codex_adapter(
                    gate_session.token,
                    extra_env=secret_env,
                    workspace_dir=worker_workspace or workspace_dir,
                    model=model,
                    effort=effort,
                    role="worker",
                    scope=scope,
                )
                if agent == "codex"
                else build_kimi_adapter(
                    gate_session.token,
                    extra_env=secret_env,
                    workspace_dir=worker_workspace or workspace_dir,
                    model=model,
                    effort=effort,
                    role="worker",
                    scope=scope,
                )
                if agent == "kimi"
                else build_adapter(
                    gate_session.token,
                    extra_env=secret_env,
                    workspace_dir=worker_workspace or workspace_dir,
                    model=model,
                    effort=effort,
                    role="worker",
                    scope=scope,
                )
            )
            _drop_finished_workers(adapters)
            adapters[f"worker/{agent}/{uuid.uuid4().hex[:8]}"] = built
            return built

        config = {
            "configurable": {
                "thread_id": thread_id,
                "adapters": adapters,
                "adapter_factory": adapter_factory,
                "worker_factory": worker_factory,
                "claude_auth_mode": read_auth_mode(),
                # Where this conversation works. The fan-out needs it to give
                # each sub-agent its own git worktree under it.
                "workspace_dir": workspace_dir,
                "tracer": websocket.app.state.tracer,
                "usage_tracker": websocket.app.state.usage_tracker,
                # Kept alongside "adapters" for callers/tests that still wire a
                # single adapter; graph._resolve_adapter prefers "adapters".
                "adapter": adapter,
                "pool": websocket.app.state.pool,
                "embedding_model": websocket.app.state.embedding_model,
                "transcript": transcript,
            }
        }

        # The queue lives here, in the websocket handler, not inside a
        # ChatSession object: graph.astream() is called once per turn (not once
        # forever like the old ChatSession.run() loop), so the queueing that
        # ChatSession used to provide internally is now explicit here —
        # relay_events() loops pulling one prompt at a time off `queue`, keeping
        # concurrent user messages processed in order instead of interrupting an
        # in-flight turn (see roadmap doc, "동시 메시지 처리: 큐잉").
        queue: "asyncio.Queue[tuple[str, str, bool]]" = asyncio.Queue()
        # Set when relay_events() hits an unrecoverable failure, so the receive
        # loop below (which owns the only calls to websocket.close()) can stop
        # and actually close the connection instead of leaving a dead socket
        # that silently swallows every later message the client sends.
        relay_failed = asyncio.Event()
        # The turn currently being streamed, so a "cancel" frame from the client
        # can stop that one turn without tearing down the connection. Written by
        # relay_events(), read by the receive loop.
        current_turn_task: Optional[asyncio.Task] = None
        current_turn_agent: Optional[str] = None
        # Distinguishes "the client asked to cancel this turn" from "the whole
        # connection is being torn down": both surface as a CancelledError out of
        # the awaited turn task, but only the first one should be reported to the
        # client and followed by more turns.
        cancel_requested = asyncio.Event()

        async def run_turn(prompt: str, agent: str, plan_mode: bool) -> None:
            # Session id continuity across turns now comes from
            # graph.aget_state(config) (Postgres-backed via the checkpointer)
            # instead of an in-memory ChatSession attribute — this is the whole
            # point of this subproject. session_ids/last_agent come from the same
            # snapshot, which is what lets an agent switch resume the incoming
            # CLI's own session and hand it the other CLI's recent context.
            timeline = TurnTimeline()
            gate_session.timeline = timeline
            gate_session.native_agents.clear()
            config["configurable"]["timeline"] = timeline
            config["configurable"]["native_agents"] = gate_session.native_agents
            try:
                snapshot = await graph.aget_state(config)
                values = snapshot.values or {}
                async for event in graph.astream(
                    {
                        "prompt": prompt,
                        "agent": agent,
                        # Planning applies only to the turn that requested it.
                        "plan_mode": plan_mode,
                        "session_id": values.get("session_id"),
                        "session_ids": values.get("session_ids") or {},
                        "last_agent": values.get("last_agent"),
                        "response_text": "",
                    },
                    config=config,
                    stream_mode="custom",
                ):
                    await websocket.send_json(event)
            finally:
                gate_session.timeline = None
                config["configurable"].pop("timeline", None)
                config["configurable"].pop("native_agents", None)

        async def relay_events() -> None:
            nonlocal current_turn_task, current_turn_agent
            try:
                while True:
                    prompt, agent, plan_mode = await queue.get()
                    # Each turn runs as its own task so it can be cancelled
                    # individually; awaiting graph.astream() inline would make a
                    # cancel request indistinguishable from killing the relay.
                    current_turn_task = asyncio.create_task(run_turn(prompt, agent, plan_mode))
                    current_turn_agent = agent
                    # So an audit row names the CLI that actually made the call.
                    gate_session.current_agent = agent
                    try:
                        await current_turn_task
                    except asyncio.CancelledError:
                        if not cancel_requested.is_set():
                            raise  # connection teardown, not a client cancel
                        cancel_requested.clear()
                        with contextlib.suppress(Exception):
                            await websocket.send_json(
                                {
                                    "type": "cancelled",
                                    "text": "",
                                    "session_id": None,
                                    "is_error": False,
                                    "agent": agent,
                                }
                            )
                    finally:
                        # A teardown cancel lands on the relay while it awaits
                        # the turn task, leaving that task running against a
                        # closing socket unless it is cancelled here too.
                        if current_turn_task is not None and not current_turn_task.done():
                            current_turn_task.cancel()
                        current_turn_task = None
                        current_turn_agent = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Best-effort: the socket may already be closing/closed by the
                # time a mid-turn failure happens, so swallow send errors here —
                # the point is to try, not to guarantee delivery.
                with contextlib.suppress(Exception):
                    await websocket.send_json(
                        {"type": "error", "text": str(exc), "session_id": None, "is_error": True}
                    )
                relay_failed.set()

        relay_task = asyncio.create_task(relay_events())
        try:
            while True:
                receive_task = asyncio.ensure_future(websocket.receive_text())
                failure_task = asyncio.ensure_future(relay_failed.wait())
                done, pending = await asyncio.wait(
                    {receive_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if failure_task in done:
                    # relay_events() already sent an error frame; now actually
                    # close so the client sees the connection drop instead of
                    # silently queuing messages nobody will ever answer.
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1011)
                    break
                data = await receive_task
                frame = _parse_client_frame(data)
                if frame.kind == "approval":
                    # Answers a pending gate request; the gate endpoint is what is
                    # actually waiting on this, not the turn loop.
                    gate_registry.resolve_approval(
                        gate_session, frame.request_id, frame.decision
                    )
                    continue
                if frame.kind == "cancel":
                    task = current_turn_task
                    if task is not None and not task.done():
                        cancel_requested.set()
                        task.cancel()
                        # Cancelling the await unwinds the adapter's async
                        # generator, but the CLI subprocess it spawned is a
                        # separate OS process and outlives that unwinding unless
                        # it is killed explicitly.
                        if current_turn_agent is not None:
                            with contextlib.suppress(Exception):
                                await adapters[current_turn_agent].terminate()
                    else:
                        # Nothing in flight: still answer, so a client whose UI
                        # thinks a turn is running can reset it.
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
                # Queued rather than run inline: concurrent prompts are processed
                # in order instead of interrupting the turn in flight. A cancel
                # frame stops only that in-flight turn, not what is queued behind
                # it.
                await queue.put((frame.prompt, frame.agent, frame.plan_mode))
        except WebSocketDisconnect:
            pass
        finally:
            relay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay_task
    finally:
        # Runs even when the client disconnects during setup, which cancels
        # this coroutine before the loop above is ever reached.
        #
        # Both adapters, not just the one that ran last: a disconnect mid-turn
        # can leave either CLI's subprocess alive, and terminate() is a no-op
        # for an adapter that never started one.
        for each_adapter in list(adapters.values()):
            with contextlib.suppress(Exception):
                await each_adapter.terminate()
        # Invalidates the gate token with the connection: a hook that outlives
        # its conversation must not keep getting approvals, and anything still
        # waiting on this browser is failed (which denies) rather than hung.
        gate_registry.unregister(gate_session.token)
        active_sessions.dec()
