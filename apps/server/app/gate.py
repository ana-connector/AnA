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
from packages.governance.subagent_scope import Scope, violation as scope_violation
from packages.governance.workspace import escaping_paths
from packages.mcp.registry import load_registry
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

# What the lead may call without being granted file, shell, network or search
# access. Hook tool names omit punctuation, so a tool can arrive in either
# spelling and both are listed.
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
LEAD_CONTROL_SERVERS = {"ask"}

# ANA's own MCP server: one tool, `ask_user`, which puts a question to the
# browser and waits (packages/mcp/servers/ask_user.py).
ASK_SERVER = "ask"

# The CLIs' own sub-agent tooling. ANA refuses it by default and delegates
# through its planner instead, because a natively-spawned sub-agent skips
# everything ANA delegation provides: no per-task permission scope, no git
# worktree of its own, no verification command, and no visible instruction —
# both CLIs hand these tools an *encrypted* `message` (a Fernet token), so the
# audit row, the approval card and the secret-leak scan all see ciphertext
# where the delegated task should be. A delegation ANA cannot read is one it
# cannot govern.
#
# ANA_NATIVE_SUBAGENTS=on puts them back, for comparing the two paths.
NATIVE_DELEGATION_TOOLS = {
    "agent",
    "task",
    "collaborationspawn_agent",
    "collaborationfollowup_task",
    "collaborationsend_message",
    "collaborationwait_agent",
    "collaborationlist_agents",
    "collaborationinterrupt_agent",
}


def native_subagents_allowed() -> bool:
    return os.environ.get("ANA_NATIVE_SUBAGENTS", "off").lower() == "on"


def lead_control_tools() -> set[str]:
    if native_subagents_allowed():
        return LEAD_CONTROL_TOOLS | NATIVE_DELEGATION_TOOLS
    return LEAD_CONTROL_TOOLS


def lead_may_call(tool_name: str) -> bool:
    """Whether the lead itself may make this call.

    The lead does no work of its own — everything that touches the machine goes
    through a sub-agent — with two exceptions: reading the clock, and asking the
    user. Both spellings of an MCP tool name are recognized, and the server is
    matched as well as the literal name, so a renamed tool inside the `ask`
    server does not silently become a denial."""
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
    task = session.native_agents.get(body.agent_id) or {
        "id": f"native:{body.agent_id}",
        "title": body.agent_type or "하위 에이전트",
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


class Question(BaseModel):
    """One question as `ask_user` states it."""

    question: str
    # Choices to pick from. Empty means a free-text answer.
    options: list[str] = []
    # Whether several options may be picked at once.
    multi: bool = False
    # Shown under the question: why it is being asked, or what each option
    # implies.
    note: str = ""


class AskRequest(BaseModel):
    questions: list[Question] = []
    # Which agent is asking, for the card's header.
    role: str | None = None


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
    if any(len(q.options) < 2 for q in questions):
        raise HTTPException(status_code=422, detail="each question needs at least two options")

    payload = [q.model_dump() for q in questions]
    answers = await registry.request_answer(
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

    answered = answers is not None
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
                "decided_by": "user" if answered else "timeout",
                "rule_id": None,
                "category": "question",
                "agent_id": None,
            }
        )

    if not answered:
        return {"answered": False, "answers": [], "note": UNANSWERED}
    return {"answered": True, "answers": answers}


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

    if body.model and session.models.get(session.current_agent or "") != body.model:
        session.models[session.current_agent or ""] = body.model
        with contextlib.suppress(Exception):
            await session.send(
                {
                    "type": "model",
                    "text": body.model,
                    "session_id": body.cli_session_id,
                    "is_error": False,
                    "agent": session.current_agent,
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
        # Counted next to the audit row so the metric and the log can never
        # disagree about what happened.
        record_gate_decision(decision, decided_by, verdict.rule_id)
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
                "agent": session.current_agent or "codex",
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
                    "agent": session.current_agent,
                    "tool": tool_event,
                }
            )
        await audit.record_event(
            pool,
            thread_id=session.thread_id,
            agent=session.current_agent,
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
        escaping = escaping_paths(body.tool_input, session.workspace)

    if (
        body.tool_name.lower() in NATIVE_DELEGATION_TOOLS
        and not native_subagents_allowed()
    ):
        # Refused for every role, not just the lead: a sub-agent spawning its
        # own sub-agents is the same blind spot one level down.
        await _audit(DENY, "delegation", "native sub-agent tooling is disabled")
        return GateDecision(
            decision=DENY,
            # English, like every other text ANA writes for an agent to read:
            # the words the *user* sees are the card's Korean description.
            reason=(
                "This CLI's own sub-agent tooling is not available. To split work, "
                "answer with a plan (JSON) — ANA runs each task with a permission "
                "scope, its own worktree and a verification command."
            ),
            rule_id="native-subagents-disabled",
            category="delegation",
        )

    if body.role == "lead" and not body.agent_id and not lead_may_call(body.tool_name):
        # The lead agent plans and writes the answer; everything that touches
        # the machine goes through a sub-agent. Enforced here rather than by
        # CLI flags so it holds for both CLIs and cannot be argued out of.
        await _audit(DENY, "role", "lead sessions do not use tools")
        return GateDecision(
            decision=DENY,
            reason=(
                "The lead session does not use tools. Delegate this to a sub-agent "
                "(answer with a plan that splits the work)."
            ),
            rule_id="lead-no-tools",
            category="delegation",
        )

    # The scope its lead gave this sub-agent (roadmap stage 3's 권한 스코프,
    # stage 5's Elevation-of-Privilege row). Checked before the rule set
    # because it only ever narrows: a scope cannot grant what the rules deny.
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

    # ASK: record that approval was sought *before* waiting on a human, so an
    # abandoned request is still visible in the log.
    await _audit("pending", "rule", verdict.reason)
    asked_at = time.time()
    answer = await registry.request_approval(
        session,
        {
            "tool_name": body.tool_name,
            "tool_input": safe_input,
            "rule_id": verdict.rule_id,
            "category": verdict.category,
            # Why this is being asked, in the words of whoever wrote the rule
            # (config/governance.yaml). The id and the category are for querying
            # the audit log afterwards; they do not tell the person answering
            # what the machine is worried about.
            "description": verdict.description,
            "agent": session.current_agent,
            "cwd": body.cwd,
            "workspace": session.workspace,
        },
    )

    gate_wait_seconds.observe(time.time() - asked_at)

    if answer == ALLOW:
        await _audit(ALLOW, "user", "approved in the web UI")
        return GateDecision(
            decision=ALLOW, reason="approved by the user", rule_id=verdict.rule_id,
            category=verdict.category,
        )

    decided_by = "timeout" if answer == "timeout" else "user"
    reason = (
        "no answer before the approval timeout"
        if answer == "timeout"
        else "denied in the web UI"
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
    return {"secrets": await list_secrets(request.app.state.pool, scope=scope)}


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
