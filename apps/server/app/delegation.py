"""Run lead-requested tasks through the worker routing ladder."""
import asyncio
import contextlib
import uuid
from typing import Awaitable, Callable, Optional

from apps.server.app.graph import _run_worker, attribute
from packages.governance.secrets import mask as mask_secrets
from packages.governance.subagent_scope import Scope
from packages.observability.metrics import record_failover
from packages.orchestrator.activity import (
    ActivityTracker,
    IdleTimeout,
    WallTimeout,
    idle_timeout_seconds,
    supervise,
    wall_timeout_seconds,
)
from packages.orchestrator import context as shared_context
from packages.orchestrator import timeline as timeline_blocks
from packages.orchestrator import worktrees
from packages.orchestrator.breaker import (
    is_auth_failure,
    is_recoverable_failure,
    is_usage_limit,
)
from packages.orchestrator.cooldown import label_for, shared as shared_cooldowns, usable
from packages.orchestrator.routing import WORKER, decide, load_routing_table


class DelegationManager:
    """One worker per request in a turn, even if an MCP caller times out.

    MCP clients have their own tool-call deadline. A long worker cannot be
    awaited in one call: when that deadline fires, the lead may retry while
    the first worker is still running. Keep the task on the server and return
    a stable ID after a short wait so later calls observe the same result.
    """

    def __init__(self, active: set[asyncio.Task], poll_seconds: float = 15.0):
        self.active = active
        self.poll_seconds = poll_seconds
        self.by_id: dict[str, asyncio.Task] = {}
        self.by_request: dict[tuple[int, str, str], str] = {}

    async def request(
        self, generation: int, title: str, prompt: str,
        spawn: Callable[[], Awaitable[dict]], task_id: str = "",
    ) -> dict:
        if task_id:
            task = self.by_id.get(task_id)
            if task is None:
                return {"status": "error", "error": "알 수 없는 하위 작업 ID입니다."}
        else:
            key = (generation, title, prompt)
            task_id = self.by_request.get(key) or uuid.uuid4().hex
            task = self.by_id.get(task_id)
            if task is None:
                task = asyncio.create_task(spawn(), name=f"delegate:{task_id}")
                self.by_request[key] = task_id
                self.by_id[task_id] = task
                self.active.add(task)
                task.add_done_callback(self.active.discard)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), self.poll_seconds)
        except asyncio.TimeoutError:
            return {"status": "running", "task_id": task_id}
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            return {"status": "error", "task_id": task_id, "error": "하위 작업이 중지됐습니다."}
        except Exception as exc:
            return {"status": "error", "task_id": task_id, "error": str(exc)}
        return {**result, "task_id": task_id}


async def run_routed_task(
    title: str,
    prompt: str,
    *,
    worker_factory: Callable,
    workspace_dir: str,
    usage_tracker,
    native_agents: dict,
    send: Callable[[dict], Awaitable[None]],
    secrets: list | None = None,
    activity: Optional[ActivityTracker] = None,
    thread_id: str = "",
) -> dict:
    agent_id = f"routed:{uuid.uuid4().hex[:12]}"
    # A delegated worker has no plan, so it has no plan-level context — but the
    # workspace's own notes are about the repository, not about one turn, and
    # they are exactly what stops this worker re-reading what an earlier one
    # already worked out (packages/orchestrator/context.py).
    prompt = shared_context.attach(
        prompt,
        shared_context.render(None, workspace_dir, remembered=shared_context.recall(workspace_dir)),
    )
    selected = decide(None, role=WORKER, usage=usage_tracker.snapshot())
    rungs = load_routing_table().rungs_for(WORKER)
    selected_index = next((i for i, rung in enumerate(rungs)
                           if (rung.agent, rung.model) == (selected.agent, selected.model)), 0)
    # A rung that just refused for lack of capacity is skipped rather than
    # asked again per task: one spent 5-hour window used to be rediscovered by
    # every worker that followed (packages/orchestrator/cooldown.py).
    routes = usable(rungs[selected_index:])
    cooldowns = shared_cooldowns()
    task = {"id": f"native:{agent_id}", "title": title, "agent": selected.agent,
            "model": selected.model, "status": "running", "output": "", "prompt": prompt,
            "tools": [], "blocks": []}
    native_agents[agent_id] = task

    async def announce() -> None:
        await send({"type": "subagent", "text": "", "session_id": None,
                    "is_error": task["status"] == "error",
                    "agent": task["agent"], "task": dict(task)})

    # The card should exist while the worker's worktree is being prepared,
    # not only after its CLI has started or completed.
    await announce()

    # Same isolation a planned worker gets (apps/server/app/graph.py): its own
    # git worktree on its own branch, merged back when it finishes. Until now a
    # delegated worker edited the session workspace directly, so it and the lead
    # — which keeps working while it waits on the tool result — wrote to one
    # directory and one git index.
    tree = (
        await worktrees.create(workspace_dir, agent_id, thread_id=thread_id)
        if workspace_dir
        else None
    )
    cwd = tree.path if tree is not None else workspace_dir
    try:
        result = await _run_ladder(
            routes, task, prompt, cwd,
            worker_factory=worker_factory, usage_tracker=usage_tracker,
            announce=announce, secrets=secrets, activity=activity,
            agent_id=agent_id, cooldowns=cooldowns,
        )
    finally:
        if tree is not None:
            # In `finally`, like the fan-out's: a worker that raised, or a turn
            # the user cancelled, would otherwise leave its checkout and its
            # branch behind in the workspace.
            merged = await _merge_back(tree, title, workspace_dir)
            with contextlib.suppress(Exception):
                await worktrees.remove(tree, keep_branch=not merged.merged)
    if tree is not None:
        _report_merge(task, result, merged)
        await announce()
    return result


async def _merge_back(tree, title: str, workspace_dir: str) -> worktrees.MergeResult:
    try:
        return await worktrees.merge(
            tree,
            f"ANA delegate: {title}",
            # Per workspace: the lead's own fan-out merges into the same index.
            lock=worktrees.merge_lock_for(workspace_dir or "."),
        )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed: the work
        # is still on its branch and whoever reads the result has to be told
        # which one.
        return worktrees.MergeResult(
            merged=False, reason=f"병합을 실행하지 못함: {exc}", branch=tree.branch
        )


def _report_merge(task: dict, result: dict, merged: worktrees.MergeResult) -> None:
    """Puts the merge outcome where the lead will actually read it.

    The delegate tool hands the lead one string (packages/mcp/servers/delegate.py),
    so a conflict that only lands in the task record is a conflict the lead never
    hears about — and it would then answer the user as though the work had been
    applied."""
    task["merge"] = {
        "merged": merged.merged, "empty": merged.empty,
        "branch": merged.branch, "reason": merged.reason,
    }
    if merged.merged:
        return
    notice = (
        f"[ANA] 이 작업의 변경은 병합되지 못했습니다. 작업 내용은 "
        f"{merged.branch} 브랜치에 있습니다: {merged.reason}"
    )
    result["output"] = f"{result.get('output') or ''}\n\n{notice}".strip()
    task["output"] = result["output"]


async def _run_ladder(
    routes, task: dict, prompt: str, cwd: str, *, worker_factory, usage_tracker,
    announce, secrets, activity, agent_id: str, cooldowns,
) -> dict:
    """Runs one delegated task down the worker ladder, in `cwd`.

    Split out so the caller's `finally` can always merge and remove the
    worktree — the same shape `_work_the_task` has in the fan-out."""
    for index, route in enumerate(routes):
        task.update(agent=route.agent, model=route.model, status="running", output="",
                    blocks=[])
        await announce()
        idle_for = 0.0
        ran_for = 0.0
        try:
            adapter = worker_factory(route.agent, route.model, route.effort, Scope(), cwd)
            attribute(adapter, agent_id)
            if activity is not None:
                activity.start(agent_id)
            async def report_text(text: str) -> None:
                timeline_blocks.append_text(task.setdefault("blocks", []), text)
                await announce()
            # Watched rather than timed: the lead used to wait a fixed 30
            # minutes on this call, which cut working workers off mid-task and
            # waited the full half hour on ones that had already died quietly.
            run = await supervise(
                _run_worker(
                    adapter,
                    prompt,
                    # Streamed to the card as it arrives, so the worker's own
                    # narration is visible while it works and survives a run
                    # that never reaches a result.
                    on_text=report_text,
                    tracker=activity,
                    key=agent_id,
                ),
                tracker=activity,
                key=agent_id,
                idle_timeout=idle_timeout_seconds(),
                wall_timeout=wall_timeout_seconds(),
            )
            error = run.error or ""
            output = run.output or ""
            failed = run.failed
            usage_tracker.record_tokens(route.agent, run.tokens, usd=float(run.cost_usd or 0.0))
        except IdleTimeout as idle:
            idle_for = idle.idle_for
            error, output, failed = (
                f"하위 에이전트가 {idle.idle_for:.0f}초 동안 아무 활동이 없어 중단했습니다.",
                _text_so_far(task),
                True,
            )
        except WallTimeout as wall:
            ran_for = wall.ran_for
            error, output, failed = (
                f"하위 에이전트가 {wall.ran_for:.0f}초 제한을 넘겨 중단했습니다.",
                _text_so_far(task),
                True,
            )
        except Exception as exc:  # A worker failure can still move to the next rung.
            error, output, failed = str(exc), _text_so_far(task), True
        finally:
            if activity is not None:
                activity.forget(agent_id)
        error = mask_secrets(error, secrets or [])
        output = mask_secrets(output, secrets or [])
        if not failed:
            task.update(status="done", output=output, model=run.model or route.model)
            await announce()
            return {"status": "done", "output": output, "agent": route.agent}
        combined = f"{error}\n{output}"
        if is_usage_limit(combined) or is_auth_failure(combined):
            # About the account, not this task: the next worker should start one
            # rung further down instead of finding out the same way.
            cooldowns.trip(label_for(route.agent, route.model), reason=error[:200])
        if index + 1 < len(routes) and is_recoverable_failure(combined):
            next_route = routes[index + 1]
            record_failover(f"{route.agent}/{route.model}",
                            f"{next_route.agent}/{next_route.model}")
            continue
        # A worker that was given up on usually left work behind — the first one
        # ANA cut off had already committed — so whatever it did say goes back to
        # the lead with the reason. Reporting only "failed" is what made the lead
        # redo the work by hand.
        task.update(
            status="timeout" if (idle_for or ran_for) else "error",
            output=output,
            error=error or "하위 작업을 완료하지 못했습니다.",
        )
        await announce()
        return {
            "status": "error",
            "error": error or task["error"],
            "output": output,
        }
    task.update(status="error", error="사용 가능한 하위 에이전트가 없습니다.")
    await announce()
    return {"status": "error", "error": task["error"]}


def _text_so_far(task: dict) -> str:
    return "".join(
        block.get("text", "")
        for block in task.get("blocks") or []
        if block.get("kind") == "text"
    )
