import asyncio
import contextlib
import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from packages.memory.rag import recent_turns, search_similar_turns, store_turn
from packages.orchestrator.breaker import (
    BreakerConfig,
    BreakerState,
    build_chain,
    is_auth_failure,
    is_usage_limit,
    is_recoverable_failure,
    next_rung,
    rate_limit_exceeded,
    thread_budget_exceeded,
)
from packages.governance.secrets import mask as mask_secrets
from packages.observability.codex_usage import read_rate_limits as read_codex_rate_limits
from packages.orchestrator import context as shared_context, verification, worktrees
from packages.observability.metrics import record_failover, record_subagent, record_turn
from packages.observability.tracing import Tracer
from packages.orchestrator.cooldown import (
    label_for,
    shared as shared_cooldowns,
    usable,
)
from packages.orchestrator.handoff import build_handoff_prompt
from packages.orchestrator.routing import (
    EFFORTS,
    LEAD,
    WORKER,
    decide,
    planning_effort,
    load_routing_table,
    settings_for,
)
from packages.orchestrator.planner import (
    EXECUTE,
    Plan,
    build_planning_prompt,
    build_replan_prompt,
    build_task_prompt,
    build_synthesis_prompt,
    max_concurrency,
    max_tasks,
    parse_plan,
    plan_rounds,
)
from packages.orchestrator.settings import apply_workspace_prompt
from packages.orchestrator.workspaces import get_workspace
from packages.orchestrator.supervisor import (
    WorkerRun,
    run_tasks,
    summarize,
)
from packages.orchestrator import timeline as timeline_blocks
from packages.orchestrator.activity import (
    idle_timeout_seconds,
    wall_timeout_seconds,
)

# A disabled tracer, used when no caller wired one in (tests, single-agent
# callers). Every method is a no-op, so nothing downstream needs a guard.
NULL_TRACER = Tracer(None)

logger = logging.getLogger("ana.graph")

# Resumed CLI sessions can retain an older system prompt even after the server
# image changes. Bump this when lead permissions or instructions change.
CLI_INSTRUCTIONS_VERSION = 3


@contextlib.contextmanager
def _suppress_tracing():
    """Tracing is never a reason a turn fails."""
    try:
        yield
    except Exception:  # noqa: BLE001
        pass



class GraphState(TypedDict):
    prompt: str
    session_id: Optional[str]
    response_text: str
    total_cost_usd: float
    # Cumulative input+output tokens across the thread's turns. Tracked
    # alongside total_cost_usd, not instead of it: the Claude CLI reports a
    # dollar figure and the Codex CLI reports tokens, so a thread that used
    # both has one meaningful number from each.
    total_tokens: int
    # Which CLI runs this turn ("claude" | "codex"). Absent/unknown values
    # normalize to the default agent, so a client that never sends one keeps
    # the single-agent behavior this graph shipped with.
    agent: str
    # One CLI session id per agent: each CLI resumes its *own* conversation
    # (claude --resume / codex exec resume), so they cannot share the single
    # `session_id` field above -- that one stays as the active agent's id for
    # backward compatibility with callers/tests that read it.
    session_ids: dict[str, Optional[str]]
    instruction_version: int
    # Which agent ran the previous turn in this thread; a mismatch with
    # `agent` is what triggers the handoff brief.
    last_agent: Optional[str]
    # Circuit breaker memory: which failover rung this thread fell back to,
    # when it tripped, and why. Empty/zero means "running on the preferred
    # rung"; the cooldown in BreakerConfig is what ends a trip.
    failover_rung: Optional[str]
    failover_tripped_at: float
    failover_reason: str
    # Set per turn by the client: ask the lead for a plan first and fan the
    # work out to sub-agents when it says so. Off by default because planning
    # costs an extra CLI call.
    plan_mode: bool


# Bound on each matched turn's prompt/response before interpolation into the
# augmented prompt. turns.response is an unbounded TEXT column holding a full
# agent reply (this project already needed a 10MiB stdout reader limit for
# real long lines -- see ClaudeCodeAdapter -- so a large stored response is a
# real, not hypothetical, occurrence). The augmented prompt is passed to the
# `claude` CLI as a single argv element, and Linux caps a single argv string
# at MAX_ARG_STRLEN (131072 bytes); truncating every match's fields keeps
# even multiple matches nowhere near that limit.
_MAX_MATCH_FIELD_CHARS = 300


def _truncate(text: str, limit: int = _MAX_MATCH_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head - 3
    return text[:head] + "..." + text[-tail:]


def _build_augmented_prompt(prompt: str, matches: list[dict]) -> str:
    if not matches:
        return prompt
    context_lines = "\n".join(
        f"{i + 1}. Q: {_truncate(m['prompt'])}\n   A: {_truncate(m['response'])}"
        for i, m in enumerate(matches)
    )
    return (
        f"관련 과거 대화:\n{context_lines}\n\n현재 질문: {prompt}"
    )


def _tokens_from_usage(usage: Optional[dict]) -> int:
    """Input+output tokens out of either CLI's usage payload. Cache-hit and
    cache-write counts are deliberately excluded: both CLIs report them as a
    breakdown *of* input_tokens, so adding them double-counts. Returns 0 for
    a payload with no token counts at all (the Claude CLI's
    rate_limit_event, which reports window utilization instead)."""
    if not usage:
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


def _with_cards(results: list[dict], cards: dict) -> list[dict]:
    """The turn's sub-agents as the transcript should keep them.

    A planned task appears in `results`; its live card is where its tool calls
    and narration accumulated, so the two are merged rather than both listed.
    The merge also rescues the record of a worker that was given up on — its
    result has no tools or blocks of its own, and dropping the card would leave
    the panel empty for exactly the run someone wants to read. Cards that match
    no result are the CLIs' own sub-agents and pass through unchanged."""
    merged = []
    by_id = {card.get("id"): card for card in cards.values()}
    for result in results:
        card = by_id.get(result.get("id"))
        if card is not None:
            result = {
                **result,
                "tools": result.get("tools") or card.get("tools") or [],
                "blocks": result.get("blocks") or card.get("blocks") or [],
            }
        merged.append(result)
    claimed = {result.get("id") for result in results}
    merged.extend(card for card in cards.values() if card.get("id") not in claimed)
    return merged


def _timeline_blocks(configurable: dict) -> Optional[list]:
    """The lead turn's ordered transcript, when a caller wired one."""
    timeline = configurable.get("timeline")
    return timeline.blocks if timeline is not None else None


def _switch_notice(from_rung: str, to_rung: str, text: str) -> str:
    """The one-line divider that marks where a turn changed models.

    Written by the server so the live divider and the restored one are the same
    sentence: the browser used to compose this itself, which is why reopening a
    conversation showed a transcript with the switch silently removed."""
    reason = "인증 오류" if is_auth_failure(text or "") else ""
    if not reason and is_usage_limit(text or ""):
        reason = "사용량 한도"
    if not reason:
        reason = " ".join((text or "오류").split())[:120] or "오류"
    return f"{from_rung} → {to_rung} 전환 ({reason})"


def _failover_frame(
    writer,
    *,
    text: str,
    agent: str,
    from_rung: str,
    to_rung: str,
    session_id=None,
    task_id=None,
    blocks: Optional[list] = None,
) -> None:
    """Records the switch where it belongs and tells the browser about it.

    `blocks` is the transcript the notice is part of — the lead turn's timeline,
    or one sub-agent's card when a worker is the thing that moved."""
    record_failover(from_rung, to_rung)
    notice = _switch_notice(from_rung, to_rung, text)
    if blocks is not None:
        timeline_blocks.append_notice(blocks, notice)
    frame = {
        "type": "failover",
        "text": text,
        "session_id": session_id,
        "is_error": False,
        "agent": agent,
        "from_rung": from_rung,
        "to_rung": to_rung,
        # Sent rather than derived in the browser, so the divider on screen and
        # the divider in the saved transcript cannot drift apart.
        "notice": notice,
    }
    if task_id is not None:
        frame["task_id"] = task_id
    writer(frame)


def attribute(adapter, agent_id: str) -> None:
    """Tells this worker's CLI which card its tool calls belong to.

    The hook reads ANA_GATE_AGENT_ID and passes it to the gate, which is how a
    sub-agent's commands land on its own card rather than the lead's — and how
    the idle watchdog knows which worker just did something."""
    env = getattr(adapter, "env", None)
    if isinstance(env, dict):
        env["ANA_GATE_AGENT_ID"] = agent_id
    # docker exec does not inherit this process's environment, so a worker
    # running inside the workspace container has to be handed the value.
    prefix = getattr(adapter, "command_prefix", None)
    if isinstance(prefix, list) and prefix[:2] == ["docker", "exec"]:
        prefix[-1:-1] = ["-e", f"ANA_GATE_AGENT_ID={agent_id}"]


async def _run_worker(
    adapter,
    prompt: str,
    *,
    on_text=None,
    tracker=None,
    key: Optional[str] = None,
) -> WorkerRun:
    """One sub-agent: a fresh CLI session (never resumed) that answers exactly
    one task. Errors are returned, not raised — the supervisor turns them into
    a task result so the other workers keep going.

    `on_text` receives the worker's narration as it streams, so its card can
    show what it is saying while it works rather than only a final answer that a
    cut-off run never produces. Every event also marks the worker as alive for
    the idle watchdog: a CLI that streams text between tool calls is working
    even while the gate sees nothing."""
    run = WorkerRun(adapter=adapter)
    try:
        async for event in adapter.run_stream(prompt, session_id=None):
            if tracker is not None:
                tracker.touch(key)
            # Reported once, usually on the first frame; kept so the task can
            # say which model did the work rather than only which CLI.
            if getattr(event, "model", None):
                run.model = event.model
            if event.type == "text":
                run.output += event.text
                if on_text is not None:
                    update = on_text(event.text)
                    if inspect.isawaitable(update):
                        await update
            elif event.type == "result":
                # The result event is the CLI's final answer. Streaming text
                # can contain earlier progress messages, or miss a final
                # chunk; pass the complete final answer to the lead.
                if event.text:
                    run.output = event.text
                if event.cost_usd:
                    run.cost_usd += event.cost_usd
                run.tokens += _tokens_from_usage(event.usage)
                if event.is_error:
                    run.failed = True
                    run.error = event.text
            elif event.type == "usage":
                run.tokens += _tokens_from_usage(event.usage)
            elif event.type == "error":
                run.failed = True
                if not is_recoverable_failure(run.error):
                    run.error = event.text or run.error
    finally:
        # The worker's subprocess must not outlive the fan-out, including when
        # the turn is cancelled mid-flight.
        with contextlib.suppress(Exception):
            await adapter.terminate()
    return run


async def _work_the_task(
    task, prompt, base_prompt, cwd, worker_route, worker_factory, writer,
    *, card=None, tracker=None,
):
    """Runs one task's worker, re-dispatching once when ANA's own check fails.

    Split out of the fan-out so the caller can wrap it in a `finally` that
    always merges and removes the worktree.

    `card` is the live task record the browser is watching: the worker's
    narration is appended to it as it streams, and its gate-screened tool calls
    land there too (the CLI is told which card it is, see delegation.attribute),
    so a planned sub-agent's panel shows the same ordered record a lead turn
    does instead of a bare final answer."""
    run = None
    checked = None
    rungs = load_routing_table().rungs_for(WORKER)
    selected = next(
        (i for i, rung in enumerate(rungs)
         if (rung.agent, rung.model) == (worker_route.agent, worker_route.model)),
        -1,
    )
    # Same as the delegate path: a rung resting off a usage limit is skipped
    # instead of asked once per task (packages/orchestrator/cooldown.py).
    routes = usable([worker_route] + (rungs[selected + 1:] if selected >= 0 else []))
    cooldowns = shared_cooldowns()
    for attempt in range(verification.max_retries() + 1):
        for index, route in enumerate(routes):
            adapter = worker_factory(route.agent, route.model, route.effort, task.scope, cwd)
            attribute(adapter, task.id)
            if tracker is not None:
                tracker.start(task.id)
            run = await _run_worker(
                adapter,
                prompt,
                on_text=(
                    (lambda text: timeline_blocks.append_text(
                        card.setdefault("blocks", []), text
                    ))
                    if card is not None
                    else None
                ),
                tracker=tracker,
                key=task.id,
            )
            run.extras["agent"] = route.agent
            if not run.model:
                run.model = route.model
            combined = f"{run.error}\n{run.output}"
            if run.failed and (is_usage_limit(combined) or is_auth_failure(combined)):
                cooldowns.trip(
                    label_for(route.agent, route.model), reason=(run.error or "")[:200]
                )
            # A failed worker CLI goes to the next rung whatever the reason;
            # only limits and bad credentials also rest the rung (above).
            if not run.failed:
                break
            if index + 1 < len(routes):
                next_route = routes[index + 1]
                _failover_frame(
                    writer,
                    text=run.error or run.output,
                    agent=next_route.agent,
                    from_rung=f"{route.agent}/{route.model}",
                    to_rung=f"{next_route.agent}/{next_route.model}",
                    task_id=task.id,
                    blocks=card.setdefault("blocks", []) if card is not None else None,
                )
        if run.failed:
            break
        if not task.verify:
            break
        checked = await verification.verify(task.verify, cwd or ".")
        if not checked.failed or not checked.retryable or attempt >= verification.max_retries():
            break
        # The check is the fact; the worker's summary is a claim. Send it back
        # once with the failure output attached.
        writer(
            {
                "type": "subagent",
                "text": f"검증 실패, 재시도: {checked.command}",
                "session_id": None,
                "is_error": True,
                "agent": task.agent,
                "task": {**_task_frame(task), "status": "running"},
            }
        )
        prompt = verification.retry_prompt(base_prompt, checked)
    return run, checked


def _task_frame(task) -> dict:
    """One planned task as the UI reads it."""
    return {
        "id": task.id,
        "title": task.title,
        "agent": task.agent,
        "prompt": task.prompt,
        "scope": task.scope.as_dict(),
        "verify": task.verify,
        "needs": list(task.needs),
    }


def _status(writer, phase: str, agent: str, count: int = 0) -> None:
    """Tells the browser which phase this turn is in.

    Only a key and a count: the wording belongs to the UI, which is where every
    other Korean string lives. Phases are "planning", "delegating",
    "replanning", "synthesizing" and "answering" — the browser turns the last
    frame it saw into "…하고 있습니다" and drops it when the answer starts."""
    writer(
        {
            "type": "status",
            "text": phase,
            "session_id": None,
            "is_error": False,
            "agent": agent,
            "count": count,
            # When this phase started, by the server's clock. The browser used
            # to stamp it on arrival, so leaving the conversation and coming
            # back restarted the counter at zero while the turn kept going.
            "started_at": time.time(),
        }
    )


async def _plan_turn(adapter, prompt: str, *, tracer, trace, on_event=None):
    """Asks the lead for a plan. A planning call that fails costs latency, not
    the turn: parse_plan() falls back to a direct answer."""
    return await _ask_for_plan(adapter, build_planning_prompt(prompt), tracer=tracer,
                               trace=trace, span="plan", on_event=on_event)


async def _replan_turn(adapter, prompt: str, results: list[dict], existing_ids, *,
                       tracer, trace, round_index: int, on_event=None):
    """Between rounds: shown what came back, does the lead want another step?

    A plan written before anything ran is a guess about what the work will need.
    This is where the guess gets corrected — and it uses the same parser, so
    "done" needs no special case: anything that is not a delegation with tasks
    ends the fan-out."""
    return await _ask_for_plan(
        adapter,
        build_replan_prompt(prompt, results, existing_ids),
        tracer=tracer,
        trace=trace,
        span=f"replan-{round_index}",
        # ANA numbers the tasks, so a later round continues the numbering
        # instead of colliding with the round that already ran, and may name
        # those finished ids in `needs`.
        id_offset=len(tuple(existing_ids)),
        known_ids=existing_ids,
        on_event=on_event,
    )


async def _ask_for_plan(adapter, prompt: str, *, tracer, trace, span: str,
                        id_offset: int = 0, known_ids=(), on_event=None):
    planning = WorkerRun()
    with tracer.span(trace, name=span):
        try:
            async for event in adapter.run_stream(prompt, session_id=None):
                # Every event is a sign of life, which is what the caller's
                # timeout is actually watching for.
                if on_event is not None:
                    on_event()
                if event.type == "text":
                    planning.output += event.text
                elif event.type == "result" and event.is_error:
                    planning.error = event.text or planning.output
                elif event.type == "error":
                    planning.error = event.text or planning.error
        except Exception as exc:  # noqa: BLE001 - a planning call that fails
            # costs latency, not the turn.
            planning.error = str(exc)
    plan = parse_plan(planning.output, id_offset=id_offset, known_ids=known_ids)
    if is_recoverable_failure(planning.error) or (
        not plan.delegating and plan.note and is_recoverable_failure(planning.output)
    ):
        raise _PlanningLimit(planning.error or planning.output)
    return plan


class _PlanningLimit(Exception):
    """A planning CLI exhausted its window; another rung must make the plan."""


async def _plan_with_failover(configurable, agent, model, effort, writer, tracer, trace,
                              prompt, *, results=None, existing_ids=(), round_index=0,
                              start_rung=None):
    """Run planning on the lead ladder, carrying the chosen model into work."""
    # ANA_PLANNER_TIMEOUT_SECONDS is a *silence* budget, not a wall clock: a
    # lead that is still streaming its plan is working, and killing it at sixty
    # seconds threw away a plan that was arriving. The total is the backstop
    # for the opposite case -- a model that talks forever without landing.
    try:
        per_model_timeout = max(1.0, float(os.environ.get("ANA_PLANNER_TIMEOUT_SECONDS", "60")))
        total_timeout = max(1.0, float(os.environ.get("ANA_PLANNER_TOTAL_TIMEOUT_SECONDS", "600")))
    except ValueError:
        per_model_timeout, total_timeout = 60.0, 600.0
    deadline = time.monotonic() + total_timeout
    chain = build_chain(agent, configurable.get("claude_auth_mode"))
    start_index = next(
        (i for i, rung in enumerate(chain) if rung.label == start_rung), 0
    )
    last_error = ""
    for index in range(start_index, len(chain)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        rung = chain[index]
        if rung.agent == agent:
            rung_model, rung_effort = model, effort
        else:
            rung_model, rung_effort = settings_for(rung.agent)
        try:
            adapter = _resolve_adapter(
                configurable, rung.agent, rung.auth_mode,
                model=rung_model, effort=planning_effort(rung.agent),
            )
            try:
                async with asyncio.timeout(min(per_model_timeout, remaining)) as limit:

                    def saw_output() -> None:
                        """Push the deadline out again, bounded by the total."""
                        left = deadline - time.monotonic()
                        if left <= 0:
                            return
                        limit.reschedule(
                            asyncio.get_running_loop().time()
                            + min(per_model_timeout, left)
                        )

                    if results is None:
                        plan = await _plan_turn(
                            adapter, prompt, tracer=tracer, trace=trace, on_event=saw_output
                        )
                    else:
                        plan = await _replan_turn(
                            adapter, prompt, results, existing_ids,
                            tracer=tracer, trace=trace, round_index=round_index,
                            on_event=saw_output,
                        )
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await adapter.terminate()
                raise _PlanningLimit(
                    f"planning produced nothing for {min(per_model_timeout, remaining):.0f}s"
                )
            return plan, rung.agent, rung_model, rung_effort, index > 0, last_error, rung.label
        except _PlanningLimit as exc:
            last_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - an unavailable auth mode is skipped
            last_error = str(exc)
        if index + 1 < len(chain):
            next_label = chain[index + 1].label
            _failover_frame(
                writer,
                text=last_error,
                agent=rung.agent,
                from_rung=rung.label,
                to_rung=next_label,
                blocks=_timeline_blocks(configurable),
            )
    raise _PlanningLimit(last_error or "all planning models are unavailable")


def _resolve_adapter(
    configurable: dict,
    agent: str,
    auth_mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
):
    """Picks the adapter for one rung of the failover chain.

    `configurable["adapter_factory"]`, when present, can build an adapter for
    a specific auth mode — which is what lets the breaker retry the Claude CLI
    on its other billing bucket. Without it the pre-built
    `configurable["adapters"]` map is used, and failing that the single
    `configurable["adapter"]` this graph originally took (still how the
    single-agent callers and tests wire it up)."""
    factory = configurable.get("adapter_factory")
    if factory is not None:
        adapter = factory(agent, auth_mode, model, effort)
        apply_workspace_prompt(adapter, configurable.get("workspace_prompt") or "")
        return adapter
    adapters = configurable.get("adapters")
    adapter = adapters.get(agent) if adapters else configurable.get("adapter")
    if adapter is None:
        raise KeyError(f"no adapter configured for agent {agent!r}")
    apply_workspace_prompt(adapter, configurable.get("workspace_prompt") or "")
    # Set unconditionally, None included. Turns are serialized per connection,
    # so writing to the shared adapter is safe — but skipping the write when
    # the caller passes None left the *previous* call's value in place: the
    # planning call runs at the cheapest effort, so a rung that names no
    # effort of its own silently answered at planning effort for the rest of
    # the turn. None means "let the CLI decide", which is a value, not an
    # absence.
    if hasattr(adapter, "model"):
        adapter.model = model
    if hasattr(adapter, "effort"):
        adapter.effort = effort
    return adapter


@dataclass
class _TurnOutcome:
    """What one attempt on one rung produced."""

    response_text: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    turn_tokens: int = 0
    error_text: str = ""
    # The end-of-turn frame, held back until the caller knows whether this
    # attempt is the last one: a rung that is about to be retried must not tell
    # the UI the turn is over.
    result_payload: Optional[dict] = None
    # The model the CLI said it was running, when it said so.
    model: Optional[str] = None
    usage_limited: bool = False
    caught_exc: Optional[BaseException] = None

    @property
    def failed(self) -> bool:
        return bool(self.error_text) or self.caught_exc is not None


async def _stream_turn(
    adapter,
    *,
    outcome: _TurnOutcome,
    prompt: str,
    agent: str,
    session_id: Optional[str],
    writer,
    base_cost_usd: float,
    base_tokens: int,
    rate_limit_utilization: float,
    usage_tracker=None,
    timeline=None,
    native_agents=None,
) -> _TurnOutcome:
    """Runs one attempt, streaming its events to the client.

    `outcome` is created by the caller and filled in here rather than returned
    fresh: a cancelled turn (client disconnect, Stop button) never returns at
    all, and whatever partial answer it produced still has to reach the
    transcript.

    Error frames are *not* written here: the caller may still have a rung left
    to try, and reporting a failure the system is about to recover from would
    be noise. Text and usage frames stream as they arrive, because a partial
    answer is still worth showing."""
    saw_result = False
    try:
        async for event in adapter.run_stream(prompt, session_id=session_id):
            if event.type == "result" and event.session_id:
                outcome.session_id = event.session_id
            if event.type == "text":
                outcome.response_text += event.text
            if event.type == "result":
                saw_result = True
                outcome.cost_usd = event.cost_usd
                if event.is_error:
                    outcome.error_text = event.text or "CLI reported a failed turn"
                # `is_error` is not always set for this: a plan that cannot pay
                # for the model answers "Fable 5 requires usage credits…" as a
                # perfectly successful result whose entire content is that
                # sentence. A reply that is *only* a limit message is a limit
                # message.
                looks_like_failure = is_recoverable_failure(event.text)
                whole_reply = outcome.response_text.strip() in ("", event.text.strip())
                if looks_like_failure and (event.is_error or whole_reply):
                    outcome.usage_limited = is_usage_limit(event.text)
                    outcome.error_text = event.text
            if event.type == "error":
                # Never clobber a real message with an empty one: the CLI can
                # follow a "requires usage credits" result with a bare non-zero
                # exit whose stderr is empty, and overwriting the reason made
                # the turn look like it had not failed at all.
                if (event.text and not is_recoverable_failure(outcome.error_text)) or not outcome.error_text:
                    outcome.error_text = event.text or "CLI exited with an error"
                if is_usage_limit(event.text):
                    outcome.usage_limited = True
            if event.type in ("usage", "result"):
                outcome.turn_tokens += _tokens_from_usage(event.usage)
            if event.type == "usage" and usage_tracker is not None:
                # The Claude CLI's rate_limit_event carries the provider's own
                # utilization; taking it beats any estimate ANA could make.
                usage_tracker.record_reported(agent, event.usage)
            if event.type == "usage" and rate_limit_exceeded(
                event.usage, rate_limit_utilization
            ):
                # The Claude CLI reports window utilization before it starts
                # refusing; treating "at the cap" as a limit hit is what makes
                # the breaker preemptive rather than purely reactive.
                outcome.usage_limited = True

            if event.type == "text":
                if timeline is not None:
                    timeline.append_text(event.text)
                writer({"type": event.type, "text": event.text})
                continue
            if event.type == "reasoning":
                writer(
                    {
                        "type": "reasoning",
                        "text": event.text,
                        "session_id": event.session_id,
                        "is_error": False,
                        "agent": agent,
                    }
                )
                continue
            if event.type == "model":
                outcome.model = event.text
                writer(
                    {
                        "type": "model",
                        "text": event.text,
                        "session_id": event.session_id,
                        "is_error": False,
                        "agent": agent,
                    }
                )
                continue
            if event.type == "error":
                continue  # held back until the caller knows whether it retries

            if event.type == "subagent" and event.raw and event.raw.get("task"):
                task = event.raw["task"]
                if native_agents is not None:
                    native_agents[task["id"]] = dict(task)
                writer({"type": "subagent", "text": "", "session_id": event.session_id,
                        "is_error": False, "agent": agent, "task": task})
                continue

            payload = {
                "type": event.type,
                "text": event.text,
                "session_id": event.session_id,
                "is_error": event.is_error,
                # Which CLI produced this turn, so the UI can label
                # the reply and mark where a handoff happened.
                "agent": agent,
            }
            if event.usage:
                payload["usage"] = event.usage
            if event.type == "result":
                payload["cost_usd"] = outcome.cost_usd
                payload["total_cost_usd"] = base_cost_usd + (outcome.cost_usd or 0.0)
                payload["turn_tokens"] = outcome.turn_tokens
                payload["total_tokens"] = base_tokens + outcome.turn_tokens
                # Held, not written: see _TurnOutcome.result_payload.
                outcome.result_payload = payload
                continue
            writer(payload)
        if not saw_result and not outcome.response_text and not outcome.error_text:
            outcome.error_text = "the CLI ended without producing an answer"
    except Exception as exc:  # noqa: BLE001 - a raised failure is still a turn
        # outcome, not something to lose: the caller logs it to the transcript
        # and either fails over or re-raises it unchanged.
        outcome.caught_exc = exc
        outcome.usage_limited = is_usage_limit(str(exc))
    finally:
        if agent == "codex" and usage_tracker is not None:
            # Codex prints tokens but not limits, so its real window
            # utilization is read back from the rollout log the turn just
            # wrote (packages/observability/codex_usage.py). In `finally`
            # because a turn that failed on a limit is exactly the one whose
            # numbers matter most.
            with contextlib.suppress(Exception):
                usage_tracker.record_reported_windows(
                    agent, read_codex_rate_limits(_codex_home_of(adapter))
                )
    return outcome


def _codex_home_of(adapter) -> Optional[str]:
    """Where that adapter's CLI keeps its sessions. Read from the adapter's own
    environment rather than the server's: a turn running inside a workspace
    container still writes to the shared state volume, but the path is the
    CLI's, not ours."""
    env = getattr(adapter, "env", None) or {}
    return env.get("CODEX_HOME")


async def _store_turn_or_log(pool, embedding_model, **fields) -> None:
    """Stores the turn, and says so in the log when it cannot.

    The row is what a reopened conversation is made of. When writing it
    failed, the only sign was a transcript that came back empty — the reply
    had already been sent, so nothing else complained."""
    try:
        await store_turn(pool, embedding_model, **fields)
    except asyncio.CancelledError:
        # A cancelled task raises at the first await inside its own finally,
        # so this is where a turn is lost when something stops it between
        # answering and being written down.
        logger.warning(
            "turn on %s was cancelled before its row was written",
            fields.get("thread_id"),
        )
        raise
    except Exception:  # noqa: BLE001 - the answer is already with the user
        logger.exception("failed to store turn on %s", fields.get("thread_id"))
        raise
    else:
        logger.info(
            "stored turn on %s (%d characters)",
            fields.get("thread_id"), len(fields.get("response") or ""),
        )


async def call_lead_agent(state: GraphState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    gate_session = configurable.get("gate_session")
    if gate_session is not None:
        gate_session.lead_scope = None
    pool = configurable["pool"]
    thread_id = configurable["thread_id"]
    workspace = await get_workspace(pool, thread_id) if configurable.get("workspace_dir") else None
    configurable["workspace_prompt"] = workspace.system_prompt if workspace else ""
    if gate_session is not None:
        gate_session.workspace_prompt = configurable["workspace_prompt"]
    embedding_model = configurable["embedding_model"]
    transcript = configurable["transcript"]

    writer = get_stream_writer()
    started_at = time.time()
    tracer = configurable.get("tracer") or NULL_TRACER
    usage_tracker = configurable.get("usage_tracker")
    trace = tracer.trace(
        name="turn",
        thread_id=thread_id,
        metadata={"requested_agent": state.get("agent")},
    )

    last_agent = state.get("last_agent")
    # Which CLI answers is a rule-set decision unless the client named one:
    # asking the user to pick on every message is a choice they cannot make
    # better than the rules can (packages/orchestrator/routing.py).
    usage_snapshot = usage_tracker.snapshot() if usage_tracker is not None else None
    route = decide(state.get("agent"), role=LEAD, usage=usage_snapshot)
    agent = route.agent
    lead_model = route.model
    lead_effort = route.effort
    if not route.explicit:
        writer(
            {
                "type": "route",
                "text": route.reason,
                "session_id": None,
                "is_error": False,
                "agent": agent,
                "model": lead_model,
            }
        )
    adapter = _resolve_adapter(
        configurable, agent, model=lead_model, effort=lead_effort
    )
    refresh_cli_sessions = state.get("instruction_version") != CLI_INSTRUCTIONS_VERSION
    session_ids: dict[str, Optional[str]] = (
        {} if refresh_cli_sessions else dict(state.get("session_ids") or {})
    )

    # A fresh CLI session still needs recent conversation context, even when
    # it is the same agent whose stale session was discarded.
    is_handoff = last_agent is not None and (last_agent != agent or refresh_cli_sessions)
    with tracer.span(trace, name="prompt.handoff" if is_handoff else "prompt.rag"):
        if is_handoff:
            # Handoff turn: the incoming CLI has no idea what the other one just
            # did, so it gets the thread's actual recent turns instead of the
            # similarity search. The two are deliberately not combined -- the
            # handoff brief already carries this thread's context, and stacking
            # semantically-matched turns on top of it only inflates a prompt that
            # ends up as a single argv element (see _MAX_MATCH_FIELD_CHARS).
            turns = await recent_turns(pool, thread_id=thread_id, limit=4)
            augmented_prompt = build_handoff_prompt(
                state["prompt"],
                incoming_agent=agent,
                previous_agent=last_agent,
                turns=turns,
            )
        else:
            matches = await search_similar_turns(
                pool, embedding_model, state["prompt"], limit=3
            )
            augmented_prompt = _build_augmented_prompt(state["prompt"], matches)

    # --- Planner-Worker (roadmap stage 5) --------------------------------
    # A worker needs its *own* adapter: the per-connection ones hold a single
    # subprocess handle, so running five turns through one object would have
    # them overwrite and terminate each other.
    worker_factory = configurable.get("worker_factory")
    subagent_results: list[dict] = []
    results: list = []
    turn_model: Optional[str] = None
    subagent_cost = 0.0
    subagent_tokens = 0
    lead_execution_scope = None
    planning_failed_over = False
    planning_failover_reason = ""
    planning_rung = None
    routed_agent = agent
    breaker_config: BreakerConfig = configurable.get("breaker_config") or BreakerConfig.from_env()
    breaker = BreakerState(
        rung_label=state.get("failover_rung"),
        tripped_at=state.get("failover_tripped_at") or 0.0,
        reason=state.get("failover_reason") or "",
    )
    if state.get("plan_mode", True) and worker_factory is not None:
        # What the browser shows while nothing else is happening. A turn can
        # spend a minute planning and another few running workers before a
        # single character of the answer exists, and an interface that says
        # nothing during that is indistinguishable from one that is broken.
        _status(writer, "planning", agent)
        # The planning call is a short JSON judgement, not the work, so it runs
        # at the cheapest effort the chosen CLI has unless configured
        # otherwise. The answer call that follows gets the rung's own effort
        # back.
        try:
            (plan, agent, lead_model, lead_effort,
             planning_failed_over, planning_failover_reason, planning_rung) = await _plan_with_failover(
                configurable, agent, lead_model, lead_effort, writer, tracer, trace,
                state["prompt"],
                start_rung=(breaker.rung_label if route.explicit
                            and breaker.active(breaker_config.cooldown_seconds) else None),
            )
        except _PlanningLimit:
            plan = Plan(note="계획을 건너뛰고 바로 답변합니다.")
        if agent != routed_agent:
            augmented_prompt = build_handoff_prompt(
                state["prompt"], incoming_agent=agent,
                previous_agent=last_agent or routed_agent,
                turns=await recent_turns(pool, thread_id=thread_id, limit=4),
            )
        writer(
            {
                "type": "plan",
                "text": plan.note,
                "session_id": None,
                "is_error": False,
                "agent": agent,
                "mode": plan.mode,
                "tasks": [_task_frame(t) for t in plan.tasks],
            }
        )

        if plan.delegating:
            _status(writer, "delegating", agent, count=len(plan.tasks))

            async def announce(result) -> None:
                writer(
                    {
                        "type": "subagent",
                        "text": result.error,
                        "session_id": None,
                        "is_error": result.status in ("error", "timeout"),
                        "agent": result.agent or result.task.agent,
                        "task": result.as_dict(),
                    }
                )

            workspace_dir = configurable.get("workspace_dir")
            # Per workspace, not per turn: two conversations open on the same
            # folder merge into the same git index.
            merge_lock = worktrees.merge_lock_for(workspace_dir or ".")
            # Read once, for the whole fan-out. The files the lead named are
            # excerpted here and put in front of every worker's instruction, so
            # five workers on the same job stop opening the same files to find
            # out what they are looking at
            # (packages/orchestrator/context.py). Excerpts come from the
            # workspace itself — what the lead saw — not from a worker's
            # worktree, which is a checkout of HEAD.
            context_block = shared_context.render(
                plan.context,
                workspace_dir,
                remembered=shared_context.recall(workspace_dir),
            )
            # Notes about the project, not about this request, outlive the turn.
            # Only when the lead said so; see SharedContext.remember.
            shared_context.remember(
                workspace_dir, plan.context, title=state["prompt"][:80]
            )
            # Who is alive, as the gate and the adapters report it. None in
            # tests that wire no gate session, which simply means no watchdog.
            activity = gate_session.activity if gate_session is not None else None
            # The live cards the browser is watching. A planned worker now gets
            # one too, so its own tool calls and narration land on its panel
            # instead of in the middle of the lead's transcript.
            cards = configurable.get("native_agents")

            async def run_worker(task, upstream=()):
                # Workers do the actual work, so they get the worker ladder —
                # and they share the thread's gate token, so every tool call
                # they make is screened exactly like the lead's would be. What
                # they no longer share is the *directory*: each gets its own
                # git worktree when the workspace is a repository, so two
                # tasks touching the same file cannot clobber each other.
                worker_route = decide(
                    task.agent, role=WORKER, usage=usage_snapshot
                )
                tree = (
                    await worktrees.create(workspace_dir, task.id, thread_id=thread_id)
                    if workspace_dir
                    else None
                )
                cwd = tree.path if tree is not None else workspace_dir
                # `upstream` is what the supervisor waited for *in this round*.
                # A task planned in a later round can depend on one that already
                # finished, so those results are carried in from `finished` —
                # otherwise the dependency would be silently dropped and the
                # worker would invent the handover.
                carried = [
                    finished[need].as_dict()
                    for need in task.needs
                    if need in finished and need not in {r.task.id for r in upstream}
                ]
                base_prompt = shared_context.attach(
                    build_handoff_prompt(
                        build_task_prompt(task, carried + [r.as_dict() for r in upstream]),
                        incoming_agent=worker_route.agent,
                        previous_agent="lead",
                        turns=await recent_turns(pool, thread_id=thread_id, limit=2),
                    ),
                    context_block,
                )

                prompt = base_prompt
                run = None
                checked = None
                merged = None
                card = None
                if cards is not None:
                    card = cards.setdefault(task.id, {
                        "id": task.id,
                        "title": task.title,
                        "agent": worker_route.agent,
                        "model": worker_route.model,
                        "status": "running",
                        "output": "",
                        "prompt": task.prompt,
                        "tools": [],
                        "blocks": [],
                    })
                try:
                    run, checked = await _work_the_task(
                        task, prompt, base_prompt, cwd, worker_route, worker_factory, writer,
                        card=card, tracker=activity,
                    )
                finally:
                    if tree is not None:
                        # In `finally`: a worker that raised, or a turn the
                        # user cancelled, used to leave its checkout and its
                        # branch behind — one leaked worktree per failure,
                        # accumulating in the workspace nobody asked to keep
                        # them.
                        try:
                            merged = await worktrees.merge(
                                tree, f"ANA {task.id}: {task.title}", lock=merge_lock
                            )
                        except Exception as exc:  # noqa: BLE001 - a merge that
                            # cannot even run is reported, not swallowed: the
                            # work is still on its branch and the user has to
                            # be told which one.
                            merged = worktrees.MergeResult(
                                merged=False,
                                reason=f"병합을 실행하지 못함: {exc}",
                                branch=tree.branch,
                            )
                        # A conflicted branch is the only copy of that work, so
                        # it outlives the worktree that made it. Removal itself
                        # is best-effort: failing to tidy up must not turn a
                        # finished task into a failed one.
                        with contextlib.suppress(Exception):
                            await worktrees.remove(
                                tree, keep_branch=not merged.merged
                            )
                if run is not None and run.output:
                    # Masked before it goes anywhere: the next task's prompt,
                    # the synthesis, the transcript and the turns table all
                    # read this string.
                    run.output = mask_secrets(run.output, configurable.get("secrets") or [])
                if checked is not None and run is not None:
                    run.extras["verification"] = checked.as_dict()
                if merged is not None and run is not None:
                    run.extras["merge"] = {
                        "merged": merged.merged,
                        "empty": merged.empty,
                        "branch": merged.branch,
                        "reason": merged.reason,
                    }
                if card is not None and run is not None:
                    # Carried onto the task result, so the record survives the
                    # turn: the card itself is cleared when the next one starts.
                    run.extras["tools"] = list(card.get("tools") or [])
                    run.extras["blocks"] = list(card.get("blocks") or [])
                if run is not None and not run.model:
                    # The CLI said nothing about its model (a pinned default,
                    # or a run that failed before reporting), so the task shows
                    # what the ladder asked for instead of nothing.
                    run.model = worker_route.model
                return run


            # Rounds, not one shot: the plan is a guess until something has
            # run. After each round the lead sees the results and may add the
            # step they revealed — a dependency nobody could see beforehand, a
            # failure that needs another approach, a follow-up the first plan
            # could not know about. Bounded by ANA_PLAN_ROUNDS, because a lead
            # that can always add one more task can also never finish.
            finished: dict = {}
            round_tasks = list(plan.tasks)
            planned_ids = {task.id for task in plan.tasks}
            all_tasks = list(plan.tasks)
            rounds = plan_rounds()

            for round_index in range(rounds):
                with tracer.span(
                    trace,
                    name="subagents",
                    metadata={"count": len(round_tasks), "round": round_index + 1},
                ):
                    round_results = await run_tasks(
                        round_tasks,
                        run_worker,
                        max_concurrency=max_concurrency(),
                        # Silence, not elapsed time: a worker three commands
                        # from finishing must not be killed for having taken a
                        # while, and one that died in its first second must not
                        # hold its slot for the rest of a fixed budget. The old
                        # wall clock is still available, and now off by default
                        # (see packages/orchestrator/activity.py).
                        idle_timeout_seconds=idle_timeout_seconds(),
                        timeout_seconds=wall_timeout_seconds(),
                        tracker=activity,
                        on_update=announce,
                    )
                results.extend(round_results)
                for result in round_results:
                    finished[result.task.id] = result

                if round_index + 1 >= rounds:
                    break

                _status(writer, "replanning", agent)
                (follow_up, agent, lead_model, lead_effort,
                 replan_failed_over, replan_reason, planning_rung) = await _plan_with_failover(
                    configurable, agent, lead_model, lead_effort, writer, tracer, trace,
                    state["prompt"], results=[r.as_dict() for r in results],
                    existing_ids=sorted(planned_ids), round_index=round_index + 1,
                    start_rung=planning_rung,
                )
                planning_failed_over = planning_failed_over or replan_failed_over
                planning_failover_reason = replan_reason or planning_failover_reason
                round_tasks = list(follow_up.tasks) if follow_up.delegating else []
                if not round_tasks:
                    break
                planned_ids.update(task.id for task in round_tasks)
                _status(writer, "delegating", agent, count=len(round_tasks))
                all_tasks.extend(round_tasks)
                writer(
                    {
                        "type": "plan",
                        "text": follow_up.note,
                        "session_id": None,
                        "is_error": False,
                        "agent": agent,
                        "mode": "delegate",
                        # The whole graph, not just the new tasks: the browser
                        # merges by id and keeps the statuses it already has.
                        "tasks": [_task_frame(t) for t in all_tasks],
                        "round": round_index + 2,
                    }
                )

            subagent_results = [r.as_dict() for r in results]
            # Per-task checks each passed on their own branch. This is the one
            # that catches two of them being fine separately and broken
            # together — run once, on the merged tree.
            if plan.verify and workspace_dir:
                turn_check = await verification.verify(plan.verify, workspace_dir)
                writer(
                    {
                        "type": "plan",
                        "text": "",
                        "session_id": None,
                        "is_error": turn_check.failed,
                        "agent": agent,
                        "mode": plan.mode,
                        "tasks": [_task_frame(t) for t in all_tasks],
                        "verification": turn_check.as_dict(),
                    }
                )
                subagent_results.append(
                    {
                        "id": "turn",
                        "title": "전체 검증",
                        "agent": "ana",
                        "status": "done" if turn_check.passed else "error",
                        "output": turn_check.output or turn_check.reason,
                        "verification": turn_check.as_dict(),
                    }
                )
            totals = summarize(results)
            subagent_cost = totals["cost_usd"]
            subagent_tokens = totals["tokens"]
            for result in results:
                record_subagent(result.agent or result.task.agent, result.status)
            # The lead answers from what the workers produced; this is the
            # prompt the normal (failover-protected) turn loop below runs.
            _status(writer, "synthesizing", agent)
            augmented_prompt = build_synthesis_prompt(state["prompt"], subagent_results)
        elif plan.mode == EXECUTE:
            lead_execution_scope = plan.scope
            augmented_prompt += (
                "\n\n[EXECUTION SCOPE] Complete this request yourself with tools. "
                f"Permission mode: {plan.scope.mode}. "
                f"Allowed workspace paths: {', '.join(plan.scope.paths) if plan.scope.paths else 'all'}. "
                "The gate also applies its normal rules. Answer the user in Korean."
            )

    if refresh_cli_sessions:
        session_id = None
    elif agent in session_ids:
        session_id = session_ids[agent]
    elif last_agent is None or last_agent == agent:
        # Nothing recorded for this agent yet, but the flat session_id field
        # may hold its id from before session_ids existed (or from a caller
        # that only ever passes the flat field). Only trust it when no *other*
        # agent ran last -- otherwise it belongs to that other CLI, and
        # resuming another CLI's session id is at best an error.
        session_id = state.get("session_id")
    else:
        session_id = None
    total_cost_usd = state.get("total_cost_usd", 0.0)
    total_tokens = state.get("total_tokens") or 0

    preferred_mode = configurable.get("claude_auth_mode")
    chain = build_chain(agent, preferred_mode)

    # A thread that already crossed its budget starts one rung down instead of
    # spending another turn proving the limit is still there.
    # Auto routing already chooses by each provider's current usage window.
    # A lifetime thread total must not silently replace that choice with the
    # next fixed failover rung (which may itself be nearly exhausted).
    budget_reason = (
        thread_budget_exceeded(breaker_config, total_cost_usd, total_tokens)
        if route.explicit else None
    )
    tried: list[str] = []
    start_index = 0
    if planning_rung and planning_failed_over:
        for index, rung in enumerate(chain):
            if rung.label == planning_rung:
                start_index = index
                break
    elif route.explicit and breaker.active(breaker_config.cooldown_seconds):
        # Still inside the cooldown: keep using what we fell back to, and skip
        # the rungs above it. Once the cooldown expires the next turn starts
        # from the preferred rung again — that is the automatic recovery.
        for index, rung in enumerate(chain):
            if rung.label == breaker.rung_label:
                start_index = index
                break
    elif budget_reason and len(chain) > 1:
        start_index = 1

    response_text = ""
    turn_tokens = 0
    cost_usd = None
    for_error_text = ""
    caught_exc: Optional[BaseException] = None
    used_rung = chain[start_index]
    failed_over = planning_failed_over or start_index > 0
    in_flight: Optional[_TurnOutcome] = None
    failover_reason = (
        breaker.reason
        if route.explicit and breaker.active(breaker_config.cooldown_seconds)
        else (planning_failover_reason or budget_reason or "")
    )

    # The last phase: from here the lead is writing, and the first text frame is
    # what replaces this on screen.
    _status(writer, "answering", agent)

    if gate_session is not None:
        gate_session.lead_scope = lead_execution_scope
    try:
        for rung in chain[start_index:]:
            tried.append(rung.label)
            used_rung = rung
            if rung.agent != agent:
                # Changing CLI mid-turn is a handoff like any other: the
                # incoming one has none of this thread's context.
                turns = await recent_turns(pool, thread_id=thread_id, limit=4)
                attempt_prompt = build_handoff_prompt(
                    augmented_prompt,
                    incoming_agent=rung.agent,
                    previous_agent=agent,
                    turns=turns,
                )
                attempt_session_id = session_ids.get(rung.agent)
            else:
                attempt_prompt = augmented_prompt
                attempt_session_id = session_id

            # Each rung brings its own model: the one picked for the rung that
            # just failed is meaningless to another CLI ("fable" is not a Codex
            # model, and passing it makes the retry fail for a new reason).
            if rung.agent == agent:
                rung_model, rung_effort = lead_model, lead_effort
            else:
                rung_model, rung_effort = settings_for(rung.agent)
            try:
                adapter = _resolve_adapter(
                    configurable,
                    rung.agent,
                    rung.auth_mode,
                    model=rung_model,
                    effort=rung_effort,
                )
            except Exception as exc:  # noqa: BLE001 - a rung that cannot even be
                # built is a rung that is not available: the Claude CLI's API
                # mode needs a key that may not be configured. Skip to the next
                # one instead of ending the turn on a setup problem.
                skip_to = next_rung(chain, tried)
                if skip_to is None:
                    caught_exc = exc
                    break
                failed_over = True
                failover_reason = str(exc)
                _failover_frame(
                    writer,
                    text=failover_reason,
                    agent=rung.agent,
                    from_rung=rung.label,
                    to_rung=skip_to.label,
                    session_id=session_id,
                    blocks=_timeline_blocks(configurable),
                )
                continue
            # Held in a name the finally block can still read if this attempt
            # is cancelled mid-stream.
            outcome = _TurnOutcome(session_id=attempt_session_id)
            in_flight = outcome
            if gate_session is not None:
                gate_session.current_agent = rung.agent
            with tracer.span(
                trace, name=f"cli.{rung.label}", metadata={"resumed": bool(attempt_session_id)}
            ):
                await _stream_turn(
                    adapter,
                    outcome=outcome,
                    prompt=attempt_prompt,
                    agent=rung.agent,
                    session_id=attempt_session_id,
                    writer=writer,
                    base_cost_usd=total_cost_usd,
                    base_tokens=total_tokens + turn_tokens,
                    rate_limit_utilization=breaker_config.rate_limit_utilization,
                    usage_tracker=usage_tracker,
                    timeline=configurable.get("timeline"),
                    native_agents=configurable.get("native_agents"),
                )

            response_text = outcome.response_text
            turn_model = outcome.model or turn_model
            session_id = outcome.session_id
            turn_tokens += outcome.turn_tokens
            cost_usd = outcome.cost_usd
            for_error_text = outcome.error_text
            caught_exc = outcome.caught_exc
            if outcome.session_id:
                session_ids[rung.agent] = outcome.session_id

            if not outcome.failed:
                if outcome.result_payload is not None:
                    writer(outcome.result_payload)
                break

            # Logged because this decision is invisible from the outside and
            # was wrong once already: a turn that ends on a limit message with
            # rungs left is a bug, not a user-visible event.
            logger.warning(
                "turn attempt failed rung=%s usage_limited=%s tried=%s chain=%s error=%r",
                rung.label,
                outcome.usage_limited,
                tried,
                [r.label for r in chain],
                (outcome.error_text or "")[:120],
            )
            # Any failed attempt moves down the chain, not only a usage limit or
            # a bad credential: a CLI that crashed or exited non-zero (Kimi does
            # this) is just as unavailable, and the next rung still gets the
            # handoff brief. That needs a different CLI to move to, though —
            # with one pre-built adapter every rung is the same process, and
            # retrying it under another name only repeats the failure.
            # Cancellation never reaches here: it is a BaseException and
            # leaves through the finally block.
            can_switch_cli = bool(
                configurable.get("adapter_factory") or configurable.get("adapters")
            )
            follow_up = next_rung(chain, tried) if (
                can_switch_cli
                or outcome.usage_limited
                or is_auth_failure(outcome.error_text)
            ) else None
            if follow_up is None:
                # Either an ordinary failure (report it as-is) or the chain is
                # exhausted (nothing left to try). Either way this attempt was
                # the last one, so its end-of-turn frame goes out now.
                if outcome.result_payload is not None:
                    writer(outcome.result_payload)
                if for_error_text:
                    writer(
                        {
                            "type": "error",
                            "text": for_error_text,
                            "session_id": session_id,
                            "is_error": True,
                            "agent": rung.agent,
                        }
                    )
                break

            failed_over = True
            failover_reason = outcome.error_text or "usage limit reached"
            _failover_frame(
                writer,
                text=failover_reason,
                agent=rung.agent,
                from_rung=rung.label,
                to_rung=follow_up.label,
                session_id=session_id,
                blocks=_timeline_blocks(configurable),
            )
            caught_exc = None  # recovered by moving down the chain
            for_error_text = ""
            response_text = ""  # never retain the failed CLI's partial answer
    finally:
        if gate_session is not None:
            gate_session.lead_scope = None
        # A cancelled attempt never gets to copy its fields out of the loop,
        # so read them straight off the in-flight outcome here.
        if in_flight is not None:
            response_text = response_text or in_flight.response_text
            session_id = session_id or in_flight.session_id
            cost_usd = cost_usd if cost_usd is not None else in_flight.cost_usd
        failure_reason = ""
        if caught_exc is not None or for_error_text:
            reason = for_error_text or str(caught_exc)
            # The user gets the notice below; the transcript and the turn row
            # keep the cause. Preserve any partial answer before the notice.
            failure_reason = reason
            notice = (
                "인증 오류로 요청을 완료하지 못했습니다. 다른 모델도 사용할 수 있는지 확인해 주세요."
                if is_auth_failure(reason) else
                "요청을 완료하지 못했습니다. 서버 오류 기록을 확인해 주세요."
            )
            final_response = f"{response_text}\n{notice}" if response_text else notice
        else:
            final_response = response_text
        # asyncio.CancelledError is a BaseException (not Exception, since
        # Python 3.8), so it is NOT caught by `except Exception` above — it
        # passes straight through to this `finally` block instead, exactly
        # like every normal client-disconnect teardown does (websocket.py's
        # `relay_task.cancel()`). Without this guard, a turn cancelled
        # before it produced any output (caught_exc is None and no text/
        # error text accumulated yet) would still write an empty-response
        # row to the transcript/turns table, needlessly bloating the corpus
        # with content-free rows. Only skip logging in that specific
        # nothing-happened case; an exception (caught_exc set) or an
        # error/partial-text turn still logs as before.
        # The reply the user already saw cannot be unsent, but the copies
        # that outlive it can be clean: a registered secret value is masked
        # before it reaches the transcript file and the turns table, which are
        # what later turns retrieve and what anyone reading the history sees.
        final_response = mask_secrets(final_response, configurable.get("secrets") or [])
        failure_reason = mask_secrets(failure_reason, configurable.get("secrets") or [])
        if caught_exc is None and not final_response:
            # The conversation's memory is this row. A turn that ends with
            # nothing to say is a real case — a cancel, mostly — but it used
            # to leave no trace of why the history has a gap in it.
            logger.warning(
                "storing nothing for turn on %s: no answer text and no error",
                thread_id,
            )
        if caught_exc is not None or final_response:
            transcript.append(
                session_id=session_id or "", prompt=state["prompt"], response=final_response,
                cost_usd=cost_usd, error=failure_reason or None,
            )
            await _store_turn_or_log(
                pool, embedding_model,
                thread_id=thread_id, session_id=session_id,
                prompt=state["prompt"], response=final_response, cost_usd=cost_usd,
                # The rung that actually answered, which is not the requested
                # agent when the breaker moved the turn.
                agent=used_rung.agent,
                # What the reply was made of besides text, so reopening the
                # conversation shows the work rather than only the answer.
                meta={
                    "rung": used_rung.label,
                    "model": turn_model,
                    # Present only on a failed turn; see failure_reason above.
                    **({"error": failure_reason} if failure_reason else {}),
                    "tokens": turn_tokens + subagent_tokens,
                    "subagents": _with_cards(
                        subagent_results, configurable.get("native_agents", {})
                    ),
                    "failed_over": failed_over,
                    "blocks": configurable["timeline"].blocks if configurable.get("timeline") else [],
                },
            )

        # Recorded here rather than after the try block: a client that
        # disconnects right after the result frame cancels this coroutine
        # while the tail (embedding + storage) is still running, and a turn
        # that happened must still be counted. asyncio.CancelledError is a
        # BaseException, so it shows up in sys.exc_info() here rather than in
        # the except clause above.
        cancelled = sys.exc_info()[0] is asyncio.CancelledError
        outcome_label = (
            "cancelled"
            if cancelled
            else ("error" if (caught_exc is not None or for_error_text) else "ok")
        )
        if usage_tracker is not None:
            # Estimated side of the status bar: what ANA has actually spent in
            # the rolling windows, per agent.
            # Cost travels with the tokens: a dollar-capped plan (TAPIE) can
            # only be measured against what the CLI said the turn cost.
            usage_tracker.record_tokens(
                used_rung.agent, turn_tokens, usd=float(cost_usd or 0.0)
            )
            for result in results:
                usage_tracker.record_tokens(
                    result.agent or result.task.agent,
                    result.tokens,
                    usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
                )
        record_turn(
            used_rung.agent,
            outcome=outcome_label,
            duration_seconds=time.time() - started_at,
            cost_usd=cost_usd or 0.0,
            tokens=turn_tokens,
        )
        with _suppress_tracing():
            trace.update(
                metadata={
                    "rung": used_rung.label,
                    "outcome": outcome_label,
                    "cost_usd": cost_usd,
                    "tokens": turn_tokens,
                    "failed_over": failed_over,
                }
            )
        tracer.flush()

    if caught_exc is not None:
        raise caught_exc

    if cost_usd is not None:
        total_cost_usd += cost_usd
    total_cost_usd += subagent_cost
    total_tokens += turn_tokens + subagent_tokens

    session_ids[used_rung.agent] = session_id

    return {
        "session_id": session_id,
        "session_ids": session_ids,
        "instruction_version": CLI_INSTRUCTIONS_VERSION,
        "agent": used_rung.agent,
        "last_agent": used_rung.agent,
        "response_text": final_response,
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        # Remembered so the next turn starts on the same rung until the
        # cooldown expires, then returns to the preferred one on its own.
        "failover_rung": used_rung.label if failed_over else None,
        "failover_tripped_at": (
            breaker.tripped_at
            if (failed_over and breaker.active(breaker_config.cooldown_seconds))
            else (time.time() if failed_over else 0.0)
        ),
        "failover_reason": failover_reason if failed_over else "",
    }


def build_graph(checkpointer) -> CompiledStateGraph:
    graph = StateGraph(GraphState)
    graph.add_node("call_lead_agent", call_lead_agent)
    graph.add_edge(START, "call_lead_agent")
    graph.add_edge("call_lead_agent", END)
    return graph.compile(checkpointer=checkpointer)
