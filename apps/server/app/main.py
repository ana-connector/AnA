import contextlib
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
from apps.server.app.runners import RunnerRegistry
from apps.server.app.gate import router as gate_router
from apps.server.app.graph import build_graph
from apps.server.app.websocket import allowed_origins, router as chat_router
from packages.governance import audit
from packages.governance.audit import run_audit_migrations
from packages.governance.secrets import SecretCipher, run_secret_migrations
from packages.governance.session import GateRegistry
from packages.memory.db import create_pool, run_migrations
from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import list_threads, thread_history
from packages.governance.workspace import session_workspace, workspace_root
from packages.observability.codex_usage import read_rate_limits as read_codex_rate_limits
from packages.observability.limits import UsageTracker
from packages.orchestrator.containers import containers_enabled, parse_ports, stop_workspace
from packages.orchestrator.routing import load_routing_table
from packages.orchestrator.settings import (
    AUTH_MODES,
    read_auth_mode,
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
    forget_workspace,
    list_workspaces,
    rename_workspace,
    run_workspace_migrations,
    self_workspace_path,
)
from packages.observability.metrics import active_sessions, render
from packages.observability.tracing import Tracer


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ana")

    pool = await create_pool(dsn)
    await run_migrations(pool)
    await run_audit_migrations(pool)
    await run_secret_migrations(pool)
    await run_workspace_migrations(pool)
    # ANA's own repository as a workspace, when it is mounted: the agent that
    # edits this file is reachable from the same list, and under the same gate,
    # as every other project. A no-op when ANA_SELF_WORKSPACE is unset.
    with contextlib.suppress(Exception):
        await ensure_self_workspace(pool)

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()

        app.state.pool = pool
        app.state.embedding_model = EmbeddingModel()
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

        yield

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
    # "pro" (subscription) or "api" (key + base URL) — the two modes
    # ~/.bashrc defines. Changing it used to mean editing .env and restarting.
    auth_mode: str | None = None


@app.get("/models")
async def models() -> dict:
    """The (CLI, model) pairs the ladder knows, in ladder order. The composer
    offers exactly these, so a manual pick is always something the router could
    have chosen on its own."""
    ladder = load_routing_table()
    return {
        "auto": True,
        "lead": [
            {"agent": r.agent, "model": r.model, "label": r.label} for r in ladder.lead
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
    # Takes effect on the next WebSocket connection: the adapters for an open
    # conversation were built with the mode that was current when it started.
    return {
        "status": "ok",
        "routing": read_routing(),
        "system_prompt": read_system_prompt(),
        "auth_mode": read_auth_mode(),
    }


@app.get("/usage")
async def usage(request: Request) -> dict:
    """Per-agent usage windows for the status bar.

    Both CLIs state their real utilization — Claude on stdout, Codex only in
    its rollout log — so the bar shows the provider's numbers where they
    exist and says "estimated" only where they do not. Codex's log is re-read
    here as well as after each turn, so a restarted server has the real figure
    before it has run anything."""
    with contextlib.suppress(Exception):
        request.app.state.usage_tracker.record_reported_windows(
            "codex", read_codex_rate_limits()
        )
    return {"agents": request.app.state.usage_tracker.snapshot()}


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
