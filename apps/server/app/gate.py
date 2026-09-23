"""The gate endpoint the PreToolUse hooks call.

One request per tool call the CLIs want to make. Order matters: the audit row
is written before the user is ever asked, so a call that is denied, approved
or abandoned all leave the same trail (roadmap stage 3: "게이트 통과 시 먼저
삭제 불가능한 감사 로그를 무조건 생성").
"""
import contextlib
import json
import time

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from packages.governance import audit
from packages.observability.metrics import gate_wait_seconds, record_gate_decision
from packages.governance.rules import ALLOW, ASK, DENY, Verdict, load_ruleset
from packages.governance.workspace import escaping_paths
from packages.mcp.registry import load_registry
from packages.mcp.scope import scope_denial
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

# These calls let the lead delegate and check the clock without granting it
# file, shell, network, or search access. Hook tool names omit punctuation.
LEAD_CONTROL_TOOLS = {
    "agent",
    "task",
    "collaborationspawn_agent",
    "collaborationfollowup_task",
    "collaborationsend_message",
    "collaborationwait_agent",
    "collaborationlist_agents",
    "collaborationinterrupt_agent",
    "clockcurr_time",
}


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

    if body.role == "lead" and not body.agent_id and body.tool_name.lower() not in LEAD_CONTROL_TOOLS:
        # The lead agent plans and writes the answer; everything that touches
        # the machine goes through a sub-agent. Enforced here rather than by
        # CLI flags so it holds for both CLIs and cannot be argued out of.
        await _audit(DENY, "role", "lead sessions do not use tools")
        return GateDecision(
            decision=DENY,
            reason=(
                "대장 세션은 도구를 쓰지 않는다. 이 작업은 하위 에이전트에게 위임해라 "
                "(계획을 세워 작업을 나눠라)."
            ),
            rule_id="lead-no-tools",
            category="delegation",
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
