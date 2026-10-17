import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv

# Must run before any route/module reads env vars (e.g. build_adapter()'s
# EnvBuilder / ANA_ALLOWED_ORIGINS): without this, a developer's local .env
# (per the plan's dev-verification instructions: `cp .env.example .env` then
# run uvicorn directly, not via docker-compose) is silently ignored — only
# docker-compose's `env_file:` path actually works. No-op if no .env file is
# present (e.g. inside Docker), and does not override already-set env vars.
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apps.server.app.capabilities import router as capabilities_router
from apps.server.app import scheduler
from apps.server.app.runners import RunnerRegistry
from apps.server.app.gate import router as gate_router
from apps.server.app.graph import build_graph
from apps.server.app.usage_sync import sync_usage
from apps.server.app.websocket import allowed_origins, build_adapter, router as chat_router
from packages.governance import audit
from packages.governance.audit import run_audit_migrations
from packages.governance.secrets import SecretCipher, run_secret_migrations
from packages.governance.session import GateRegistry
from packages.memory.db import create_pool, run_migrations
from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import list_threads, thread_history
from packages.governance.workspace import session_workspace, workspace_root
from packages.observability.claude_usage import fetch_rate_limits as fetch_claude_limits
from packages.observability.codex_usage import read_rate_limits as read_codex_rate_limits
from packages.observability.cursor_usage import fetch_rate_limits as fetch_cursor_limits
from packages.observability.kimi_usage import fetch_rate_limits as fetch_kimi_limits
from packages.observability.limits import UsageTracker
from packages.orchestrator.containers import containers_enabled, parse_ports, stop_workspace
from packages.orchestrator.handoff import AGENTS
from packages.observability.limits import windows_for
from packages.orchestrator.models import model_catalog
from packages.orchestrator.routing import EFFORTS, load_routing_table
from packages.orchestrator.schedules import (
    CronError,
    create_schedule,
    delete_schedule,
    describe,
    get_schedule,
    list_schedules,
    run_schedules_migrations,
    update_schedule,
)
from packages.orchestrator.settings import (
    AUTH_MODES,
    GOVERNANCE_MODES,
    enabled_agents,
    read_auth_mode,
    read_disabled_agents,
    write_disabled_agents,
    read_governance_mode,
    write_governance_mode,
    read_routing_structured,
    write_routing_structured,
    read_routing,
    read_system_prompt,
    write_auth_mode,
    write_routing,
    write_system_prompt,
)
from packages.orchestrator.workspaces import (
    SELF_WORKSPACE_ID,
    WorkspacePathError,
    create_workspace,
    ensure_self_workspace,
    get_workspace,
    set_container_settings,
    set_workspace_archived,
    set_workspace_icon,
    set_workspace_system_prompt,
    forget_workspace,
    list_workspaces,
    rename_workspace,
    run_workspace_migrations,
    self_workspace_path,
)
from packages.observability.metrics import active_sessions, render
from packages.observability.tracing import Tracer
from packages.remote.discord_bridge import DiscordBridge, DiscordConfig
from packages.remote.discord_client import DiscordAPI, DiscordGateway


def start_discord_bridge(gate_registry):
    """Wires the Discord bridge onto the gate, or returns None.

    Returning None rather than raising is the point: a missing token means the
    feature is off, and a bridge that fails to start must not stop ANA from
    serving the browser that can already answer everything."""
    config = DiscordConfig.from_env()
    if config is None:
        return None
    try:
        api = DiscordAPI(config.token)
        bridge = DiscordBridge(config=config, api=api, registry=gate_registry)
        gateway = DiscordGateway(config.token, bridge.handle_interaction)
        gate_registry.notifier = bridge
        return api, gateway, asyncio.create_task(gateway.run(), name="discord-gateway")
    except Exception:  # noqa: BLE001 - never load-bearing
        logging.getLogger(__name__).warning("discord bridge failed to start", exc_info=True)
        return None


async def stop_discord_bridge(started) -> None:
    api, gateway, task = started
    gateway.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    with contextlib.suppress(Exception):
        await api.close()


# ANA's own loggers had no handler: uvicorn configures its own and nothing
# else, so everything below WARNING was dropped and the rest reached stderr
# only through logging's last-resort handler. A turn that failed to be
# written down said so into a void.
logging.basicConfig(
    level=os.environ.get("ANA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ana")

    pool = await create_pool(dsn)
    await run_migrations(pool)
    await run_audit_migrations(pool)
    await run_secret_migrations(pool)
    await run_workspace_migrations(pool)
    await run_schedules_migrations(pool)
    # ANA's own repository as a workspace, when it is mounted: the agent that
    # edits this file is reachable from the same list, and under the same gate,
    # as every other project. A no-op when ANA_SELF_WORKSPACE is unset.
    with contextlib.suppress(Exception):
        await ensure_self_workspace(pool)

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()

        app.state.pool = pool
        app.state.embedding_model = EmbeddingModel()
        # Warmed here rather than on the first turn. The first embedding
        # loads the ONNX model and builds its session, which on a small
        # machine takes longer than anyone waits — and it happens inside the
        # finally that writes the turn down, so the turn is answered and then
        # appears to vanish. Paying it at startup also means a model that
        # cannot be loaded says so here instead of eating a conversation.
        try:
            await asyncio.to_thread(app.state.embedding_model.embed_one, "warm")
        except Exception:  # noqa: BLE001 - ANA still answers without memory
            logging.getLogger(__name__).exception("the embedding model failed to load")
        app.state.checkpointer = checkpointer
        app.state.graph = build_graph(checkpointer)
        app.state.secret_cipher = SecretCipher()
        # No Langfuse configuration means a no-op tracer, not a failure.
        app.state.tracer = Tracer.from_env()
        app.state.usage_tracker = UsageTracker()
        # One runner per conversation, outliving the browser tabs that watch it:
        # a turn takes minutes and must not die because someone opened another
        # workspace (apps/server/app/runners.py).
        app.state.runners = RunnerRegistry()
        app.state.gate_registry = GateRegistry(
            approval_timeout=float(os.environ.get("ANA_APPROVAL_TIMEOUT_SECONDS", 300)),
            # Longer than an approval on purpose: an approval interrupts someone
            # who is watching the agent work, a question can arrive when they
            # have walked away. A timeout here is a result, not a failure — the
            # agent is told to state its assumption and carry on.
            question_timeout=float(os.environ.get("ANA_QUESTION_TIMEOUT_SECONDS", 900)),
        )
        # A second door to the same pending requests, for answering from a
        # phone (packages/remote). Unset ANA_DISCORD_BOT_TOKEN and none of this
        # exists: the notifier stays null and the gate behaves as before.
        discord = start_discord_bridge(app.state.gate_registry)
        # Turns that start without anybody typing
        # (packages/orchestrator/schedules.py).
        scheduler_task = scheduler.start(app)

        yield

        await scheduler.stop(scheduler_task)
        if discord is not None:
            await stop_discord_bridge(discord)
        # Every runner, with its CLI subprocesses and gate tokens, belongs to
        # this process: shutting down without closing them leaves orphans.
        await app.state.runners.close_all()

    await pool.close()


app = FastAPI(title="ANA Core Loop", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(gate_router)
app.include_router(capabilities_router)


@app.middleware("http")
async def cors_for_allowed_origins(request: Request, call_next):
    """Hand-rolled instead of CORSMiddleware so the allowed set is read per
    request from the same allowed_origins() the WebSocket handshake check
    uses — CORSMiddleware freezes its origin list at construction time,
    which would silently diverge from the WS check whenever
    ANA_ALLOWED_ORIGINS changes.

    The browser needs this because the UI (port 3005) and the API (8100) are
    different origins, so GET /threads/{id}/turns is a cross-origin request."""
    origin = request.headers.get("origin")
    permitted = origin is not None and origin in allowed_origins()

    if request.method == "OPTIONS" and request.headers.get(
        "access-control-request-method"
    ):
        response = Response(status_code=204 if permitted else 403)
    else:
        response = await call_next(request)

    if permitted:
        response.headers["Access-Control-Allow-Origin"] = origin
        # The UI creates and edits workspaces, secrets and settings, so the
        # preflight has to allow the methods it actually uses — a GET-only
        # list fails the PATCH with "Method PATCH is not allowed".
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Allow-Headers"] = "content-type"
        response.headers["Vary"] = "Origin"
    return response


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition. Unauthenticated like the rest of the API, and
    bound to the internal network by the same deployment assumption."""
    body, content_type = render()
    return Response(content=body, media_type=content_type)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


class WorkspaceCreate(BaseModel):
    name: str
    # Optional: an existing project directory to adopt. Left out, ANA makes one
    # under ANA_WORKSPACE_ROOT named after the workspace.
    path: str | None = None


class WorkspaceRename(BaseModel):
    name: str | None = None
    # Container settings. Absent fields are left as they are, so a rename does
    # not silently reset them.
    container_enabled: bool | None = None
    container_image: str | None = None
    container_ports: list | None = None
    system_prompt: str | None = None
    # Archiving only moves the workspace out of the main list; the row, the
    # folder and the conversation all stay.
    archived: bool | None = None
    # An emoji, or a small image as a data URL. "" removes it.
    icon: str | None = None


@app.get("/workspaces")
async def workspaces(request: Request, limit: int = Query(default=500, ge=1, le=1000)) -> dict:
    """The folders ANA works in, each with its lead agent's conversation
    activity. A workspace owns exactly one conversation, so its id is also the
    thread id."""
    return {"workspaces": await list_workspaces(request.app.state.pool, limit=limit)}


@app.post("/workspaces")
async def add_workspace(request: Request, body: WorkspaceCreate) -> dict:
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="workspace name is required")
    try:
        workspace = await create_workspace(
            request.app.state.pool, name=body.name, path=body.path
        )
    except WorkspacePathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return workspace.as_dict()


@app.patch("/workspaces/{workspace_id}")
async def patch_workspace(request: Request, workspace_id: str, body: WorkspaceRename) -> dict:
    pool = request.app.state.pool
    touched = False

    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="workspace name is required")
        touched = await rename_workspace(pool, workspace_id, body.name) or touched

    if body.system_prompt is not None:
        touched = await set_workspace_system_prompt(
            pool, workspace_id, body.system_prompt
        ) or touched

    if body.archived is not None:
        touched = await set_workspace_archived(pool, workspace_id, body.archived) or touched

    if body.icon is not None:
        try:
            touched = await set_workspace_icon(pool, workspace_id, body.icon) or touched
        except ValueError as exc:
            # What is wrong with the icon is something the user can fix, so it
            # is said in the response rather than logged as a server fault.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if (
        body.container_enabled is not None
        or body.container_image is not None
        or body.container_ports is not None
    ):
        current = await get_workspace(pool, workspace_id)
        if current is None:
            raise HTTPException(status_code=404, detail="no such workspace")
        touched = await set_container_settings(
            pool,
            workspace_id,
            enabled=(
                current.container_enabled
                if body.container_enabled is None
                else body.container_enabled
            ),
            image=(
                current.container_image if body.container_image is None else body.container_image
            ),
            # Normalized on the way in: values come from the UI and end up as
            # `docker run -p` arguments.
            ports=[
                p.as_dict()
                for p in parse_ports(
                    list(current.container_ports)
                    if body.container_ports is None
                    else body.container_ports
                )
            ],
        ) or touched

    if not touched:
        raise HTTPException(status_code=404, detail="no such workspace")
    updated = await get_workspace(pool, workspace_id)
    return {"status": "ok", **(updated.as_dict() if updated else {"id": workspace_id})}


@app.get("/workspaces/capabilities")
async def workspace_capabilities() -> dict:
    """What this deployment can do with workspaces.

    `containers`: whether per-workspace containers can run here at all — the UI
    disables the switch rather than letting a user turn on something that would
    only fail at the first turn. `self_workspace`: the id of the workspace that
    points at ANA's own source tree, or null when the repository is not
    mounted; the UI marks that row, since work done there changes ANA."""
    return {
        "containers": containers_enabled(),
        "self_workspace": SELF_WORKSPACE_ID if self_workspace_path() else None,
    }


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    request: Request, workspace_id: str, purge: bool = True
) -> dict:
    """Deletes the workspace and, unless `purge=false`, its conversation —
    turns and checkpoints — so reopening the same id starts empty. The folder
    and the audit log stay: the user owns the files, and the audit log outlives
    the conversation that caused it."""
    if not await forget_workspace(request.app.state.pool, workspace_id, purge=purge):
        raise HTTPException(status_code=404, detail="no such workspace")
    # The container is named after the workspace, so one left running would
    # outlive the thing that asked for it — holding its published ports, and
    # waiting to be inherited by whatever next takes this id.
    with contextlib.suppress(Exception):
        await stop_workspace(workspace_id)
    return {"status": "forgotten", "id": workspace_id}


class SettingsUpdate(BaseModel):
    routing: str | None = None
    system_prompt: str | None = None
    # CLIs to switch off. The router skips them, failover skips them, and the
    # composer stops offering them; at least one has to stay on.
    disabled_agents: list[str] | None = None
    # "pro" (subscription) or "api" (key + base URL) — the two modes
    # ~/.bashrc defines. Changing it used to mean editing .env and restarting.
    auth_mode: str | None = None
    # "normal" or "yolo": whether approvals still reach a human.
    governance_mode: str | None = None


class RoutingRung(BaseModel):
    agent: str
    model: str = ""
    effort: str = ""
    # The window this rung watches and the utilization at which it is spent.
    # Both empty means the last rung, where everything lands.
    window: str = ""
    utilization: float | None = None


class RoutingUpdate(BaseModel):
    lead: list[RoutingRung]
    workers: dict[str, list[RoutingRung]] = {}


class ScheduleCreate(BaseModel):
    workspace_id: str
    name: str
    cron: str
    prompt: str
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


def _schedule_json(schedule) -> dict:
    return {
        "id": schedule.id,
        "workspace_id": schedule.workspace_id,
        "name": schedule.name,
        "cron": schedule.cron,
        # What the expression says, in words: this list should not require
        # knowing cron to read.
        "cron_text": describe(schedule.cron),
        "prompt": schedule.prompt,
        "enabled": schedule.enabled,
        "created_by": schedule.created_by,
        "created_at": schedule.created_at.isoformat(),
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "last_status": schedule.last_status,
        "last_detail": schedule.last_detail,
    }


@app.get("/schedules")
async def get_schedules(request: Request, workspace_id: str | None = None) -> dict:
    rows = await list_schedules(request.app.state.pool, workspace_id=workspace_id)
    return {"schedules": [_schedule_json(row) for row in rows]}


@app.post("/schedules")
async def post_schedule(request: Request, body: ScheduleCreate) -> dict:
    try:
        created = await create_schedule(
            request.app.state.pool,
            workspace_id=body.workspace_id,
            name=body.name,
            cron=body.cron,
            prompt=body.prompt,
            enabled=body.enabled,
            created_by="user",
        )
    except CronError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _schedule_json(created)


@app.patch("/schedules/{schedule_id}")
async def patch_schedule(request: Request, schedule_id: str, body: ScheduleUpdate) -> dict:
    try:
        updated = await update_schedule(
            request.app.state.pool,
            schedule_id,
            name=body.name,
            cron=body.cron,
            prompt=body.prompt,
            enabled=body.enabled,
        )
    except CronError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    return _schedule_json(updated)


@app.delete("/schedules/{schedule_id}")
async def remove_schedule(request: Request, schedule_id: str) -> dict:
    if not await delete_schedule(request.app.state.pool, schedule_id):
        raise HTTPException(status_code=404, detail="no such schedule")
    return {"status": "deleted", "id": schedule_id}


@app.post("/schedules/{schedule_id}/run")
async def run_schedule_now(request: Request, schedule_id: str) -> dict:
    """Runs it this second, without touching when it runs next. The point is
    to see what a job actually does before trusting it to a clock."""
    schedule = await get_schedule(request.app.state.pool, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    from apps.server.app.websocket import ensure_runner

    runner = await ensure_runner(request.app, schedule.workspace_id)
    if runner.busy:
        raise HTTPException(status_code=409, detail="그 대화는 지금 다른 턴을 돌리고 있다")
    await runner.submit(schedule.prompt, "auto", True)
    return {"status": "started", "id": schedule_id}


@app.get("/routing")
async def routing_rules() -> dict:
    """The ladder as data, plus what a rung may say.

    The editor used to be a YAML text box, which asked the person changing one
    threshold to know the file format. The vocabulary travels with the rules so
    the form can offer exactly what the router accepts."""
    rules = read_routing_structured()
    # Whatever the rules already name, so a model this build has not heard of
    # keeps its place in the picker instead of vanishing on first edit.
    used: dict[str, list[str]] = {}
    for rungs in [rules["lead"], *rules["workers"].values()]:
        for rung in rungs:
            if rung.get("model"):
                used.setdefault(rung["agent"], []).append(rung["model"])
    return {
        **rules,
        "agents": list(AGENTS),
        "efforts": {agent: list(levels) for agent, levels in EFFORTS.items()},
        "models": model_catalog(used),
        # Every window name a rung may use, and which agent actually has
        # which: a rung pointing at a window its CLI does not have would never
        # be left, so the editor offers only the real ones.
        "windows": [
            {"key": "five_hour", "label": "5시간"},
            {"key": "seven_day", "label": "7일"},
            {"key": "monthly", "label": "이번 달"},
        ],
        "windows_by_agent": {
            agent: [spec.key for spec in windows_for(agent)] for agent in AGENTS
        },
        # A rung naming a switched-off CLI is kept rather than dropped — the
        # rules outlive the switch — so the editor needs to know which ones
        # the router is currently skipping.
        "disabled_agents": list(read_disabled_agents()),
    }


@app.put("/routing")
async def update_routing_rules(body: RoutingUpdate) -> dict:
    try:
        write_routing_structured(body.model_dump())
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return read_routing_structured()


@app.get("/models")
async def models() -> dict:
    """The (CLI, model) pairs the ladder knows, in ladder order. The composer
    offers exactly these, so a manual pick is always something the router could
    have chosen on its own."""
    ladder = load_routing_table()
    # Only the CLIs that are switched on: the composer's manual picks are
    # supposed to be choices the router could have made itself, and a rung
    # naming a disabled CLI is no longer one of them.
    allowed = set(enabled_agents())
    return {
        "auto": True,
        "lead": [
            {"agent": r.agent, "model": r.model, "label": r.label}
            for r in ladder.lead
            if r.agent in allowed
        ],
    }


@app.get("/settings")
async def get_settings() -> dict:
    """The editable configuration: the routing rules the router actually loads,
    the system prompt appended to every agent, and which billing mode the
    Claude CLI runs under."""
    return {
        "routing": read_routing(),
        "system_prompt": read_system_prompt(),
        "auth_mode": read_auth_mode(),
        "auth_modes": list(AUTH_MODES),
        "governance_mode": read_governance_mode(),
        "governance_modes": list(GOVERNANCE_MODES),
        # Which CLIs ANA may use, and which of them the user switched off. The
        # full list travels with the answer so the page can offer a CLI this
        # build knows about without hard-coding the names a second time.
        "agents": list(AGENTS),
        "disabled_agents": list(read_disabled_agents()),
    }


@app.put("/settings")
async def put_settings(body: SettingsUpdate) -> dict:
    if body.routing is not None:
        try:
            write_routing(body.routing)
        except Exception as exc:  # noqa: BLE001 - the editor is exactly where a
            # typo happens, so the error text is the useful part.
            raise HTTPException(status_code=422, detail=f"routing rules: {exc}") from exc
    if body.system_prompt is not None:
        write_system_prompt(body.system_prompt)
    if body.auth_mode is not None:
        try:
            write_auth_mode(body.auth_mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.governance_mode is not None:
        try:
            write_governance_mode(body.governance_mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.disabled_agents is not None:
        try:
            write_disabled_agents(body.disabled_agents)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Takes effect on the next WebSocket connection: the adapters for an open
    # conversation were built with the mode that was current when it started.
    return {
        "status": "ok",
        "routing": read_routing(),
        "system_prompt": read_system_prompt(),
        "auth_mode": read_auth_mode(),
        # Unlike the others this one takes effect on the very next tool call:
        # the gate reads it per call, not per connection.
        "governance_mode": read_governance_mode(),
        "governance_modes": list(GOVERNANCE_MODES),
        "agents": list(AGENTS),
        # Like the routing rules: read back rather than echoed, so the page
        # shows what was stored (ordered, filtered) and not what was sent.
        "disabled_agents": list(read_disabled_agents()),
    }


@app.get("/usage")
async def usage(request: Request) -> dict:
    """Per-agent usage windows for the status bar.

    Re-reads cheap disk/HTTP sources so a restarted server has real figures
    before it has run a turn. Claude/Cursor/Kimi hit the same quota APIs their
    CLIs' `/usage` commands use; Codex still comes from its rollout log.
    OpenCode has no such API at all and therefore no window to report.

    A CLI the settings page has switched off is left out entirely — including
    its quota call, which is a network round trip for a CLI that will not be
    answering anything."""
    tracker = request.app.state.usage_tracker
    allowed = set(enabled_agents())
    sources = {
        "codex": read_codex_rate_limits,
        "claude": fetch_claude_limits,
        "cursor": fetch_cursor_limits,
        "kimi": fetch_kimi_limits,
    }
    for agent, read in sources.items():
        if agent not in allowed:
            continue
        with contextlib.suppress(Exception):
            tracker.record_reported_windows(agent, read())
    return {
        "agents": {
            agent: data
            for agent, data in tracker.snapshot().items()
            if agent in allowed
        }
    }


@app.post("/usage/sync")
async def sync_usage_now(request: Request) -> dict:
    """Re-read the providers' real usage figures now, rather than at the next turn.

    Codex is free (rollout log). Claude/Cursor/Kimi are the account-quota HTTP
    APIs; Claude still has an optional stream probe fallback when OAuth fails
    (`ANA_USAGE_PROBE`). See apps/server/app/usage_sync.py."""
    results = await sync_usage(
        request.app.state.usage_tracker,
        lambda: build_adapter(role="lead"),
    )
    return {"synced": results, "agents": request.app.state.usage_tracker.snapshot()}


class UsageEstimateUpdate(BaseModel):
    agent: str
    window: str
    utilization: float | None = None
    # When this window next resets, as an ISO-8601 instant. Optional: without
    # it the figure ages out one window length after entry, which assumes the
    # window started the moment it was typed.
    resets_at: datetime | None = None


def _epoch(moment: datetime | None) -> float | None:
    """ISO-8601 to epoch seconds. A value with no timezone is read as local
    time, which is what a browser's datetime-local input sends."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


@app.get("/usage/estimates")
async def usage_estimates(request: Request) -> dict:
    return {"estimates": request.app.state.usage_tracker.estimates()}


@app.put("/usage/estimates")
async def put_usage_estimate(request: Request, body: UsageEstimateUpdate) -> dict:
    try:
        request.app.state.usage_tracker.set_estimate(
            body.agent,
            body.window,
            body.utilization,
            resets_at=_epoch(body.resets_at),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"estimates": request.app.state.usage_tracker.estimates()}


@app.get("/threads")
async def threads(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    """Conversations to choose from in the sidebar, newest first, each with the
    workspace directory it runs in — per-thread isolation is otherwise
    invisible from the UI."""
    rows = await list_threads(request.app.state.pool, limit=limit)
    root = workspace_root()
    for row in rows:
        # create=False: listing must not materialize directories for threads
        # whose workspace was cleaned up.
        row["workspace"] = str(session_workspace(root, row["thread_id"], create=False))
    return {"threads": rows}


@app.get("/threads/{thread_id}/turns")
async def thread_turns(
    request: Request,
    thread_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Past turns of one thread, oldest-first, so the browser can redraw the
    chat after a reload. The conversation itself already survives a refresh
    (CLI sessions plus LangGraph checkpoints); without this the screen came
    back empty while the agent still remembered everything."""
    pool = request.app.state.pool
    turns = await thread_history(pool, thread_id=thread_id, limit=limit)

    # Tool calls are not stored on the turn (the gate records them, and it runs
    # while the turn is still going), so they are attached here by time: every
    # audit row between the previous turn and this one belongs to this one.
    events = await audit.recent_events(pool, thread_id=thread_id, limit=500)
    by_call: dict[str, dict] = {}
    for event in sorted(events, key=lambda e: e["id"]):
        # Later rows for the same call (pending → decided) replace earlier ones.
        by_call[event["tool_use_id"] or f"row-{event['id']}"] = event

    previous_at = ""
    for turn in turns:
        turn["tools"] = [
            {
                "tool_use_id": event["tool_use_id"],
                "tool_name": event["tool_name"],
                "input": event["tool_input"] or {},
                "verdict": event["verdict"],
                "decided_by": event["decided_by"],
                "rule_id": event["rule_id"],
                "category": event["category"],
            }
            for event in sorted(by_call.values(), key=lambda e: e["created_at"])
            if previous_at < event["created_at"] <= turn["created_at"]
        ]
        previous_at = turn["created_at"]

    # Totals so a reopened conversation shows what it actually cost instead of
    # starting from zero. Cost is per turn; tokens live in each turn's meta.
    totals = {
        "cost_usd": sum(t.get("cost_usd") or 0.0 for t in turns),
        "tokens": sum(int((t.get("meta") or {}).get("tokens") or 0) for t in turns),
    }
    return {"thread_id": thread_id, "turns": turns, "totals": totals}
