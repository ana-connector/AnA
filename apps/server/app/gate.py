"""The gate endpoint the PreToolUse hooks call.

One request per tool call the CLIs want to make. Order matters: the audit row
is written before the user is ever asked, so a call that is denied, approved
or abandoned all leave the same trail (roadmap stage 3: "게이트 통과 시 먼저
삭제 불가능한 감사 로그를 무조건 생성").
"""
import contextlib
import json
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from packages.governance import audit
from packages.observability.metrics import gate_wait_seconds, record_gate_decision
from packages.governance.rules import ALLOW, ASK, DENY, Verdict, load_ruleset
from packages.governance.subagent_scope import (
    Scope,
    reads_only,
    violation as scope_violation,
)
from packages.governance.workspace import agent_state_prefixes, escaping_paths
from packages.mcp.registry import load_registry
from packages.orchestrator import timeline as timeline_blocks
from packages.orchestrator.branches import BranchError
from packages.orchestrator.branch_store import (
    branch_capability,
    create_branch,
    delete_branch,
    merge_branch,
)
from packages.orchestrator.workspaces import (
    get_workspace,
    list_branches,
    rename_workspace,
)
from packages.orchestrator.settings import yolo as yolo_mode
from packages.orchestrator.schedules import (
    CronError,
    create_schedule,
    delete_schedule,
    describe,
    get_schedule,
    list_schedules,
)
from packages.mcp.scope import scope_denial, server_for_tool
from packages.governance.secrets import (
    GLOBAL_SCOPE,
    delete_secret,
    list_secrets,
    load_secrets,
    mask,
    put_secret,
    scan_for_secrets,
    valid_secret_name,
)

router = APIRouter()

# Control tools used by the optional planner. Ordinary lead turns can use
# work tools; this list remains useful for classifying control calls.
LEAD_CONTROL_TOOLS = {
    "clockcurr_time", "askask_user", "mcp__ask__ask_user",
    # Codex's built-in question tools are reported directly by its hook,
    # rather than as calls to ANA's ask MCP server.
    "request_user_input", "request_user_input_async",
    "functionsrequest_user_input", "functionsrequest_user_input_async",
    "functions.request_user_input", "functions.request_user_input_async",
}

# MCP servers whose tools the lead may call, whatever the spelling the CLI
# reports. `ask` is how the lead reaches the user (see ASK_SERVER below):
# withholding it would leave the lead guessing at a decision that is the
# user's, which is the thing it is supposed to stop doing.
# `schedule` joins it: putting work on a clock is the lead deciding when a
# turn happens, not the lead doing the work itself.
LEAD_CONTROL_SERVERS = {"ask", "schedule", "delegate"}

# ANA's own MCP server: one tool, `ask_user`, which puts a question to the
# browser and waits (packages/mcp/servers/ask_user.py).
ASK_SERVER = "ask"

# Native CLI delegation is available by default. ANA's planner remains an
# optional path for tasks that need separate worktrees and explicit scopes.
# ANA_NATIVE_SUBAGENTS=off disables native delegation when needed.
NATIVE_DELEGATION_TOOLS = {
    "agent",
    "task",
    "agentswarm",
    "collaborationspawn_agent",
    "collaborationfollowup_task",
    "collaborationsend_message",
    "collaborationwait_agent",
    "collaborationlist_agents",
    "collaborationinterrupt_agent",
}


def native_subagents_allowed() -> bool:
    return os.environ.get("ANA_NATIVE_SUBAGENTS", "on").lower() == "on"


def lead_control_tools() -> set[str]:
    if native_subagents_allowed():
        return LEAD_CONTROL_TOOLS | NATIVE_DELEGATION_TOOLS
    return LEAD_CONTROL_TOOLS


def lead_may_call(tool_name: str) -> bool:
    """Whether a tool is a lead control call rather than a work call."""
    if tool_name.lower() in lead_control_tools():
        return True
    return server_for_tool(tool_name, load_registry().names()) in LEAD_CONTROL_SERVERS


class ToolCallRequest(BaseModel):
    tool_name: str = ""
    tool_input: dict = {}
    cli_session_id: str | None = None
    cwd: str | None = None
    tool_use_id: str | None = None
    # Both CLIs put the running model in the hook payload; for Codex this is
    # the only place it is stated at all.
    model: str | None = None
    role: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    # The permission scope this sub-agent's lead gave it, as the hook read it
    # out of the environment (packages/governance/subagent_scope.py).
    scope_mode: str | None = None
    scope_paths: str | None = None


class SubagentEvent(BaseModel):
    event: str
    agent_id: str
    agent_type: str | None = None
    output: str = ""


class DelegateRequest(BaseModel):
    title: str = ""
    prompt: str = ""
    task_id: str = ""


@router.post("/gate/delegate")
async def gate_delegate(
    request: Request,
    body: DelegateRequest,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    session = request.app.state.gate_registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    if session.delegate_worker is None:
        raise HTTPException(status_code=409, detail="no worker runner for this session")
    if not body.task_id and (not body.title.strip() or not body.prompt.strip()):
        raise HTTPException(status_code=422, detail="title and prompt are required")
    return await session.delegate_worker(
        body.title.strip()[:120], body.prompt.strip(), body.task_id.strip()
    )


@router.post("/gate/subagent")
async def gate_subagent_event(
    request: Request,
    body: SubagentEvent,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    session = request.app.state.gate_registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    if body.event not in ("SubagentStart", "SubagentStop"):
        raise HTTPException(status_code=400, detail="unknown subagent event")
    title = ""
    if body.event == "SubagentStart" and body.agent_id not in session.native_agents:
        if session.pending_native_titles:
            title = session.pending_native_titles.pop(0)
    task = session.native_agents.get(body.agent_id) or {
        "id": f"native:{body.agent_id}",
        "title": title or "하위 에이전트",
        "agent": session.current_agent or "claude",
        "status": "running",
        "output": "",
        "tools": [],
    }
    if body.event == "SubagentStop":
        task.update(status="done", output=body.output)
    session.native_agents[body.agent_id] = task
    await session.send({
        "type": "subagent", "text": "", "session_id": None,
        "is_error": False, "agent": task["agent"], "task": dict(task),
    })
    return {"ok": True}


class QuestionOption(BaseModel):
    value: str
    label: str
    description: str = ""


class QuestionInput(BaseModel):
    label: str
    placeholder: str = ""


class Question(BaseModel):
    """One question as `ask_user` states it."""

    question: str
    options: list[str | QuestionOption] = []
    input: QuestionInput | None = None
    required: bool = True
    # Whether several options may be picked at once.
    multi: bool = False
    # Shown under the question: why it is being asked, or what each option
    # implies.
    note: str = ""


class AskRequest(BaseModel):
    questions: list[Question] = []
    # Which agent is asking, for the card's header.
    role: str | None = None
    # The sub-agent asking, when it is one. Only used to pause its idle clock.
    agent_id: str | None = None


# A question the user never answered. Returned as a result rather than an
# error: an agent that loses its turn because someone stepped away is worse
# than one that states its assumption and carries on.
UNANSWERED = (
    "The user did not answer (timed out). Do nothing irreversible; state the "
    "assumption you are proceeding on, clearly, in your answer."
)


@router.post("/gate/ask")
async def gate_ask(
    request: Request,
    body: AskRequest,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    """Puts the agent's own question to the user and waits for the answer.

    The counterpart of /gate/tool-call: that one is the machine asking
    permission, this one is the agent asking a question. Both reach the same
    browser through the same registered session, because the connection that
    started the turn is the only route to the person who wants the work done.

    Unlike an approval this is *not* a verdict on a tool call — the call to
    `ask_user` itself already passed the gate — so nothing here is audited a
    second time. The question and its answer are appended to the turn's
    transcript instead, so reopening the conversation still shows what was
    asked."""
    registry = request.app.state.gate_registry
    session = registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    questions = [q for q in body.questions if q.question.strip()]
    if not questions:
        raise HTTPException(status_code=422, detail="at least one question is required")
    if any(len(q.options) < 2 and not (q.input and q.input.label.strip()) for q in questions):
        raise HTTPException(status_code=422, detail="each question needs two choices or a labelled input")

    payload = [q.model_dump() for q in questions]
    # Same reason as an approval: the agent is waiting on a person, and a
    # question can sit unanswered far longer than an approval does.
    session.activity.hold(body.agent_id)
    try:
        result = await registry.request_answer(
            session,
            {
                "text": "",
                "session_id": None,
                "is_error": False,
                "agent": session.current_agent,
                "role": body.role or "lead",
                "questions": payload,
            },
        )
    finally:
        session.activity.release(body.agent_id)

    answers = result.value
    answered = answers is not None
    # Who answered, for the transcript: "user" is the browser, "discord:<id>"
    # is someone who answered from a phone.
    answered_by = result.by or "user"
    if session.timeline is not None:
        # Recorded as a tool block, which is what the transcript and the turns
        # table already know how to carry — the alternative was a new block
        # kind and a migration for one row shape.
        session.timeline.append_tool(
            {
                "tool_use_id": None,
                "tool_name": "ask_user",
                "input": {
                    "questions": payload,
                    "answers": answers if answered else None,
                },
                "verdict": "allow" if answered else "timeout",
                "decided_by": answered_by if answered else "timeout",
                "rule_id": None,
                "category": "question",
                "agent_id": None,
            }
        )

    if not answered:
        return {"answered": False, "answers": [], "note": UNANSWERED}
    return {"answered": True, "answers": answers}


class ScheduleRequest(BaseModel):
    name: str
    cron: str
    prompt: str


@router.post("/gate/schedules")
async def gate_create_schedule(
    request: Request,
    body: ScheduleRequest,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    """An agent scheduling a turn for the conversation it is already in.

    Scoped to that conversation on purpose: the token says which one this is,
    and an agent that could schedule work in someone else's workspace is an
    agent that can reach outside its own sandbox on a timer."""
    session = request.app.state.gate_registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    try:
        created = await create_schedule(
            request.app.state.pool,
            workspace_id=session.thread_id,
            name=body.name.strip() or "이름 없는 자동화",
            cron=body.cron.strip(),
            prompt=body.prompt,
            created_by="agent",
        )
    except CronError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": created.id,
        "name": created.name,
        "cron": created.cron,
        "cron_text": describe(created.cron),
        "next_run_at": created.next_run_at.isoformat() if created.next_run_at else None,
    }


@router.get("/gate/schedules")
async def gate_list_schedules(
    request: Request, x_ana_gate_token: str = Header(default="")
) -> dict:
    session = request.app.state.gate_registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    rows = await list_schedules(request.app.state.pool, workspace_id=session.thread_id)
    return {
        "schedules": [
            {
                "id": row.id,
                "name": row.name,
                "cron": row.cron,
                "cron_text": describe(row.cron),
                "prompt": row.prompt,
                "enabled": row.enabled,
                "created_by": row.created_by,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                "last_status": row.last_status,
            }
            for row in rows
        ]
    }


@router.delete("/gate/schedules/{schedule_id}")
async def gate_delete_schedule(
    request: Request, schedule_id: str, x_ana_gate_token: str = Header(default="")
) -> dict:
    session = request.app.state.gate_registry.get(x_ana_gate_token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    existing = await get_schedule(request.app.state.pool, schedule_id)
    # 404 rather than 403 for someone else's: the agent has no business
    # learning that an id it cannot touch exists.
    if existing is None or existing.workspace_id != session.thread_id:
        raise HTTPException(status_code=404, detail="no such schedule")
    await delete_schedule(request.app.state.pool, schedule_id)
    return {"status": "deleted", "id": schedule_id}


class BranchCreateRequest(BaseModel):
    name: str
    # The git branch. Derived from the name when the agent leaves it out.
    branch: str | None = None


class BranchRenameRequest(BaseModel):
    name: str


class BranchMergeRequest(BaseModel):
    # Main's lead agent edits the project folder directly and ANA never commits
    # for it, so a dirty project is the normal case. The merge refuses it; this
    # is the agent having decided to commit that work first.
    commit_project: bool = False


async def _project_for_token(request: Request, token: str):
    """The project an agent may manage branches in, and nothing else.

    Scoped exactly as schedules are. The token says which conversation is
    calling; a *project's* agent manages that project, and a *branch's* agent
    manages its parent project — otherwise a branch agent could not see even
    itself, and could not finish the merge it was handed a conflict for.

    Nothing here lets an agent reach a project it is not working in: that is
    the whole reason this goes through the gate rather than /workspaces."""
    session = request.app.state.gate_registry.get(token)
    if session is None:
        raise HTTPException(status_code=403, detail="unknown gate token")
    caller = await get_workspace(request.app.state.pool, session.thread_id)
    if caller is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    if caller.parent_id is None:
        return caller
    project = await get_workspace(request.app.state.pool, caller.parent_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    return project


async def _branch_in_project(request: Request, project, branch_id: str):
    """One branch of that project.

    404 for a branch of someone else's project, not 403: an agent has no
    business learning that an id it cannot touch exists."""
    branch = await get_workspace(request.app.state.pool, branch_id)
    if branch is None or branch.parent_id != project.id:
        raise HTTPException(status_code=404, detail="no such branch")
    return branch


@router.get("/gate/branches")
async def gate_list_branches(
    request: Request, x_ana_gate_token: str = Header(default="")
) -> dict:
    """This project and its feature branches."""
    project = await _project_for_token(request, x_ana_gate_token)
    rows = await list_branches(request.app.state.pool, project.id)
    can, reason = await branch_capability(request.app.state.pool, project.id)
    return {
        "project": {"id": project.id, "name": project.name, "path": project.path},
        "can_branch": can,
        "reason": reason,
        "branches": [
            {
                "id": row.id,
                "name": row.name,
                "branch": row.branch,
                "base_branch": row.base_branch,
                "path": row.path,
                "archived": row.archived,
            }
            for row in rows
        ],
    }


@router.post("/gate/branches")
async def gate_create_branch(
    request: Request,
    body: BranchCreateRequest,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    project = await _project_for_token(request, x_ana_gate_token)
    try:
        branch = await create_branch(
            request.app.state.pool,
            project_id=project.id,
            name=body.name,
            branch=body.branch,
        )
    except BranchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return branch.as_dict()


@router.patch("/gate/branches/{branch_id}")
async def gate_rename_branch(
    request: Request,
    branch_id: str,
    body: BranchRenameRequest,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    """Renames the branch's *workspace*, not the git branch.

    The git branch name is the checkout's directory and the merge target, and
    moving those under a running agent is how a branch loses its work. The
    display name is what anyone actually reads."""
    project = await _project_for_token(request, x_ana_gate_token)
    branch = await _branch_in_project(request, project, branch_id)
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="branch name is required")
    await rename_workspace(request.app.state.pool, branch.id, body.name)
    updated = await get_workspace(request.app.state.pool, branch.id)
    return (updated or branch).as_dict()


@router.post("/gate/branches/{branch_id}/merge")
async def gate_merge_branch(
    request: Request,
    branch_id: str,
    body: BranchMergeRequest | None = None,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    project = await _project_for_token(request, x_ana_gate_token)
    branch = await _branch_in_project(request, project, branch_id)
    outcome = await merge_branch(
        request.app.state.pool,
        branch.id,
        commit_project=(body or BranchMergeRequest()).commit_project,
    )
    return {
        "status": outcome.status,
        "detail": outcome.detail,
        "paths": list(outcome.paths),
    }


@router.delete("/gate/branches/{branch_id}")
async def gate_delete_branch(
    request: Request,
    branch_id: str,
    force: bool = False,
    x_ana_gate_token: str = Header(default=""),
) -> dict:
    """Removes the branch, its checkout and its conversation.

    Refused while it holds commits the project does not have. `force` exists,
    but an agent reaching for it is deciding to destroy the only copy of that
    work, so the refusal is what it gets first."""
    project = await _project_for_token(request, x_ana_gate_token)
    branch = await _branch_in_project(request, project, branch_id)
    try:
        await delete_branch(request.app.state.pool, branch.id, force=force)
    except BranchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted", "id": branch.id}


class GateDecision(BaseModel):
    decision: str
    reason: str = ""
    rule_id: str | None = None
    category: str | None = None


@router.post("/gate/tool-call", response_model=GateDecision)
async def gate_tool_call(
    request: Request,
    body: ToolCallRequest,
    x_ana_gate_token: str = Header(default=""),
) -> GateDecision:
    registry = request.app.state.gate_registry
    session = registry.get(x_ana_gate_token)
    if session is None:
        # No audit row: an unknown token means we cannot attribute the call to
        # any conversation, and the caller is not part of this system.
        raise HTTPException(status_code=403, detail="unknown gate token")

    pool = request.app.state.pool
    call_agent = (
        session.native_agents.get(body.agent_id, {}).get("agent")
        if body.agent_id else None
    ) or session.current_agent

    if body.model and session.models.get(call_agent or "") != body.model:
        session.models[call_agent or ""] = body.model
        with contextlib.suppress(Exception):
            await session.send(
                {
                    "type": "model",
                    "text": body.model,
                    "session_id": body.cli_session_id,
                    "is_error": False,
                    "agent": call_agent,
                }
            )

    verdict = load_ruleset().evaluate(body.tool_name, body.tool_input)

    # Leak check first: a call that carries a registered secret value is
    # refused whatever the rules say about the command itself, and the audit
    # row is written with the value masked so the log is not the leak.
    known_secrets = await load_secrets(pool, request.app.state.secret_cipher)
    leaked = scan_for_secrets(json.dumps(body.tool_input, ensure_ascii=False), known_secrets)
    safe_input = (
        json.loads(mask(json.dumps(body.tool_input, ensure_ascii=False), known_secrets))
        if known_secrets
        else body.tool_input
    )

    async def _audit(decision: str, decided_by: str, reason: str) -> None:
        if (
            decision == ALLOW and body.role == "lead" and not body.agent_id
            and body.tool_name.lower() in {"agent", "task"}
        ):
            title = str(body.tool_input.get("description") or "").strip()
            if title:
                session.pending_native_titles.append(title[:120])
        # Counted next to the audit row so the metric and the log can never
        # disagree about what happened. The metric gets only the channel --
        # "discord", not "discord:<user id>" -- because a Prometheus label with
        # one value per person is a cardinality leak.
        record_gate_decision(decision, decided_by.split(":", 1)[0], verdict.rule_id)
        # A tool call is this sub-agent's proof of life, whatever the verdict
        # was: a worker whose commands keep being denied is still working, and
        # the watchdog that gives up on silence must not count it as silent.
        session.activity.touch(body.agent_id)
        tool_event = {
            "tool_use_id": body.tool_use_id,
            "tool_name": body.tool_name,
            "input": safe_input,
            "verdict": decision,
            "decided_by": decided_by,
            "rule_id": verdict.rule_id,
            "category": verdict.category,
            "agent_id": body.agent_id,
        }
        if body.agent_id:
            task = session.native_agents.setdefault(body.agent_id, {
                "id": f"native:{body.agent_id}",
                "title": body.agent_type or "하위 에이전트",
                "agent": call_agent or "codex",
                "status": "running",
                "output": "",
                "tools": [],
            })
            tools = task.setdefault("tools", [])
            match = next((i for i, item in enumerate(tools)
                          if body.tool_use_id and item.get("tool_use_id") == body.tool_use_id), None)
            if match is None:
                tools.append(tool_event)
            else:
                tools[match] = tool_event
            # And in order, next to what the sub-agent said around them: the
            # panel that shows one worker's record reads like the lead's
            # transcript, which needs the interleaving, not two separate lists.
            timeline_blocks.append_tool(task.setdefault("blocks", []), tool_event)
        if session.timeline is not None:
            session.timeline.append_tool(tool_event)
        # Also shown live in the chat: an agent silently running commands is
        # the part of an agent UI people most want to see.
        with contextlib.suppress(Exception):
            await session.send(
                {
                    "type": "tool",
                    "text": reason,
                    "session_id": body.cli_session_id,
                    "is_error": decision == DENY,
                    "agent": call_agent,
                    "tool": tool_event,
                }
            )
        await audit.record_event(
            pool,
            thread_id=session.thread_id,
            agent=call_agent,
            cli_session_id=body.cli_session_id,
            tool_name=body.tool_name,
            tool_input=safe_input,
            rule_id=verdict.rule_id,
            category=verdict.category,
            verdict=decision,
            decided_by=decided_by,
            reason=reason,
            tool_use_id=body.tool_use_id,
        )

    # Paths that leave this conversation's own workspace need a human, even
    # when the command itself matched nothing in the rule set: the CLIs run
    # with their own sandboxes off inside the container, so this is where
    # "outside your workspace" is decided.
    escaping: list[str] = []
    if session.workspace:
        escaping = escaping_paths(
            body.tool_input,
            session.workspace,
            # An agent reading its own CLI logs (or npm's) is not escaping
            # anything, and asking about it cost more than it protected — see
            # agent_state_prefixes(). Writing there still needs a human.
            extra_allowed=(
                agent_state_prefixes()
                if reads_only(body.tool_name, body.tool_input)
                else ()
            ),
        )

    if (
        body.tool_name.lower() in NATIVE_DELEGATION_TOOLS
        and not native_subagents_allowed()
    ):
        # The explicit off switch applies to every role.
        await _audit(DENY, "delegation", "native sub-agent tooling is disabled")
        return GateDecision(
            decision=DENY,
            # English, like every other text ANA writes for an agent to read:
            # the words the *user* sees are the card's Korean description.
            reason="This CLI's own sub-agent tooling is disabled by ANA_NATIVE_SUBAGENTS=off.",
            rule_id="native-subagents-disabled",
            category="delegation",
        )

    # Ordinary lead turns have full workspace scope. An explicit plan may
    # narrow that scope; workers still use the scope passed by their lead.
    if body.role == "lead" and not body.agent_id:
        scope = session.lead_scope or Scope()
    else:
        scope = Scope.from_payload(body.scope_mode, body.scope_paths)
    scope_reason = scope_violation(
        body.tool_name, body.tool_input, scope, session.workspace, body.cwd
    )
    if scope_reason:
        verdict = Verdict(
            action=DENY,
            rule_id="subagent-scope",
            category="privilege-scope",
            reason=scope_reason,
        )
        await _audit(DENY, "scope", scope_reason)
        return GateDecision(
            decision=DENY,
            reason=scope_reason,
            rule_id="subagent-scope",
            category="privilege-scope",
        )

    # Scope partitioning (roadmap stage 6): a role only gets the MCP servers
    # config/mcp.yaml grants it. The adapter already builds the CLI with just
    # those, so this catches the cases that route around that — a resumed
    # session started under an older config, or a server configured on the
    # host rather than by ANA.
    mcp_registry = load_registry()
    mcp_role = body.role or "worker"
    mcp_reason = scope_denial(
        body.tool_name,
        mcp_role,
        mcp_registry.names_for(mcp_role),
        mcp_registry.names(),
    )
    if mcp_reason:
        # Recorded under its own rule id rather than the rule set's (which
        # matched nothing, since no YAML rule is about MCP scope): an audit
        # row saying "denied, decided_by mcp-scope, rule_id null" is hard to
        # query for afterwards.
        verdict = Verdict(
            action=DENY,
            rule_id="mcp-scope",
            category="scope-partitioning",
            reason=mcp_reason,
        )
        await _audit(DENY, "mcp-scope", mcp_reason)
        return GateDecision(
            decision=DENY,
            reason=mcp_reason,
            rule_id="mcp-scope",
            category="scope-partitioning",
        )

    if leaked:
        names = ", ".join(leaked)
        await _audit(DENY, "secret-scan", f"tool input carries secret(s): {names}")
        return GateDecision(
            decision=DENY,
            reason=f"blocked: the command carries the value of {names}",
            rule_id="secret-egress",
            category="credential-exfiltration",
        )

    if verdict.action == DENY:
        await _audit(DENY, "rule", verdict.reason)
        return GateDecision(
            decision=DENY,
            reason=f"blocked by ANA governance rule {verdict.rule_id}",
            rule_id=verdict.rule_id,
            category=verdict.category,
        )

    if verdict.action == ALLOW and not escaping:
        await _audit(ALLOW, "rule", verdict.reason)
        return GateDecision(
            decision=ALLOW, reason="", rule_id=verdict.rule_id, category=verdict.category
        )

    if verdict.action == ALLOW and escaping:
        # Upgraded from allow to ask, never the other way round: a rule that
        # says "deny" still denies above, and one that says "ask" is already
        # asking.
        verdict = Verdict(
            action=ASK,
            rule_id="workspace-escape",
            category="session-isolation",
            reason=f"touches paths outside the session workspace: {', '.join(escaping[:3])}",
            description=(
                "이 대화의 워크스페이스 밖 경로를 건드린다: "
                + ", ".join(escaping[:3])
            ),
        )

    # YOLO mode: the user has said in advance that anything ANA would ask
    # about is approved. Deliberately placed *here*, at the last step before a
    # human is involved, and nowhere earlier — every `deny` rule, the secret
    # scan, sub-agent scope and MCP scope have already run and already
    # returned above. What this removes is the person, not the floor.
    #
    # `decided_by` is "yolo", never "user": the audit log has to say that
    # nobody looked at this. A row claiming a human approved something they
    # never saw is worse than no row.
    if yolo_mode():
        await _audit(ALLOW, "yolo", f"auto-approved in YOLO mode: {verdict.reason}")
        return GateDecision(
            decision=ALLOW,
            reason="auto-approved in YOLO mode",
            rule_id=verdict.rule_id,
            category=verdict.category,
        )

    # ASK: record that approval was sought *before* waiting on a human, so an
    # abandoned request is still visible in the log.
    await _audit("pending", "rule", verdict.reason)
    asked_at = time.time()
    # The agent is now waiting on a person, and that wait is not its idleness:
    # an approval that takes five minutes to answer used to be charged to the
    # worker's own budget, so a slow human could get a working sub-agent killed.
    session.activity.hold(body.agent_id)
    try:
        answer = await registry.request_approval(
            session,
            {
                "tool_name": body.tool_name,
                "tool_input": safe_input,
                "rule_id": verdict.rule_id,
                "category": verdict.category,
                # Why this is being asked, in the words of whoever wrote the
                # rule (config/governance.yaml). The id and the category are for
                # querying the audit log afterwards; they do not tell the person
                # answering what the machine is worried about.
                "description": verdict.description,
                "agent": session.current_agent,
                "cwd": body.cwd,
                "workspace": session.workspace,
            },
        )
    finally:
        # Whatever the answer was, and even if the wait was cancelled: a hold
        # that is never released stops that agent's clock forever.
        session.activity.release(body.agent_id)

    gate_wait_seconds.observe(time.time() - asked_at)

    # Where the answer came from, recorded as `decided_by`: "user" is the
    # browser, "discord:<user id>" is a phone (packages/remote).
    answered_by = answer.by or "user"
    where = "in Discord" if answered_by.startswith("discord:") else "in the web UI"

    if answer.value == ALLOW:
        await _audit(ALLOW, answered_by, f"approved {where}")
        return GateDecision(
            decision=ALLOW, reason="approved by the user", rule_id=verdict.rule_id,
            category=verdict.category,
        )

    decided_by = "timeout" if answer.value == "timeout" else answered_by
    reason = (
        "no answer before the approval timeout"
        if answer.value == "timeout"
        else f"denied {where}"
    )
    await _audit(DENY, decided_by, reason)
    return GateDecision(
        decision=DENY, reason=reason, rule_id=verdict.rule_id, category=verdict.category
    )


@router.get("/audit")
async def list_audit_events(request: Request, thread_id: str | None = None, limit: int = 100):
    """Read-only view of the audit log (append-only in the database)."""
    events = await audit.recent_events(
        request.app.state.pool, thread_id=thread_id, limit=min(max(limit, 1), 500)
    )
    return {"events": events}


class SecretUpsert(BaseModel):
    name: str
    value: str
    scope: str = GLOBAL_SCOPE


@router.get("/secrets")
async def get_secrets(request: Request, scope: str | None = None):
    """Names and scopes only. Values never leave the server — the whole point
    of holding them here is that they are injected into the agent's
    environment without passing through a chat message."""
    stored = await list_secrets(request.app.state.pool, scope=scope)
    # Without ANA_SECRET_KEY the cipher takes a random key per process, so
    # everything stored here stops being readable the next time the server
    # restarts -- and the only symptom is an agent that says its key is
    # missing while this list still shows the name. The UI says so up front.
    return {
        "secrets": stored,
        "key_persistent": bool(os.environ.get("ANA_SECRET_KEY")),
    }


@router.put("/secrets")
async def upsert_secret(request: Request, body: SecretUpsert):
    if not valid_secret_name(body.name):
        raise HTTPException(
            status_code=422,
            detail="secret names must look like environment variables",
        )
    await put_secret(
        request.app.state.pool,
        request.app.state.secret_cipher,
        scope=body.scope,
        name=body.name,
        value=body.value,
    )
    return {"status": "ok", "name": body.name, "scope": body.scope}


@router.delete("/secrets/{scope}/{name}")
async def remove_secret(request: Request, scope: str, name: str):
    removed = await delete_secret(request.app.state.pool, scope=scope, name=name)
    if not removed:
        raise HTTPException(status_code=404, detail="no such secret")
    return {"status": "deleted", "name": name, "scope": scope}


ASK_ACTION = ASK  # re-exported for tests that assert on the three actions
