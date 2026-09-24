"""Supervises the worker fan-out.

Owns the four things that make parallel sub-agents safe to run: a concurrency
cap, a per-task timeout, error isolation — one worker failing is a task
result, not a turn failure — and the order the plan asked for. Cancellation
propagates: a cancelled turn kills every worker subprocess it started, because
an orphaned CLI keeps working (and billing) against a conversation nobody is
watching.

Tasks form a graph, not a single parallel round. Each task names the tasks it
`needs`; a task starts when all of them are done, tasks with nothing to wait
for start together, and a task whose dependency failed is *blocked* rather
than run against a half-finished tree. Without this, a plan like
"design → frontend, backend, then deploy both" had no way to be expressed, and
the lead's only compliant answer was one task doing everything — which is what
it did.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from packages.orchestrator.planner import PlanTask

RUNNING = "running"
DONE = "done"
ERROR = "error"
TIMEOUT = "timeout"
# Never started: something it waited for did not finish.
BLOCKED = "blocked"

DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass
class TaskResult:
    task: PlanTask
    status: str = RUNNING
    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    # Which model actually answered this task, as the worker's CLI reported it
    # (falling back to the model the ladder asked for). The product name —
    # "claude", "codex" — says less than which model did the work, and a plan
    # routes its tasks to different ones on purpose.
    model: Optional[str] = None
    # What ANA checked after the worker said it was done, and what happened to
    # its branch. Both are None when the task asked for neither.
    verification: Optional[dict] = None
    merge: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "id": self.task.id,
            "title": self.task.title,
            "prompt": self.task.prompt,
            "agent": self.task.agent,
            # Shown in the UI next to the task: which permissions this
            # sub-agent was given is part of what happened, not a detail.
            "scope": self.task.scope.as_dict(),
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "model": self.model,
            "verification": self.verification,
            "merge": self.merge,
        }


@dataclass
class WorkerRun:
    """What one worker produced, as the runner reports it back."""

    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    # What the CLI said it was running, when it said anything.
    model: Optional[str] = None
    adapter: object = None
    # Set when the worker itself reported a failure rather than raising.
    failed: bool = False
    extras: dict = field(default_factory=dict)


# The second argument is the finished results this task waited for, so a
# worker can be given what the previous step produced.
RunWorker = Callable[..., Awaitable[WorkerRun]]
OnUpdate = Callable[[TaskResult], Awaitable[None]]


async def run_tasks(
    tasks: list[PlanTask],
    run_worker: RunWorker,
    *,
    max_concurrency: int = 5,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    on_update: Optional[OnUpdate] = None,
) -> list[TaskResult]:
    """Runs every task, at most `max_concurrency` at a time.

    Returns one result per task, in the order the tasks were planned, whatever
    happened to each — the synthesis step needs to see the failures too."""
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    results = [TaskResult(task=task) for task in tasks]
    by_id = {result.task.id: result for result in results}
    # An edge to a task that is not in this plan cannot ever be satisfied;
    # dropping it is kinder than blocking the task forever, and parse_plan
    # already resolved the ids it could.
    waiting_on = {
        result.task.id: {need for need in result.task.needs if need in by_id}
        for result in results
    }
    _break_cycles(waiting_on)

    async def notify(result: TaskResult) -> None:
        if on_update is not None:
            await on_update(result)

    async def run_one(result: TaskResult) -> None:
        async with semaphore:
            # Reported inside the semaphore, so "running" in the UI means
            # actually running rather than queued behind the cap.
            result.status = RUNNING
            await notify(result)
            try:
                upstream = [by_id[need] for need in waiting_on[result.task.id]]
                run = await asyncio.wait_for(
                    run_worker(result.task, upstream), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                result.status = TIMEOUT
                result.error = f"{timeout_seconds:.0f}s 안에 끝나지 않음"
            except asyncio.CancelledError:
                # The turn is being torn down; leave the result as-is and let
                # the cancellation propagate to the gather below.
                raise
            except Exception as exc:  # noqa: BLE001 - one task's failure is a
                # result, not the turn's failure.
                result.status = ERROR
                result.error = str(exc)
            else:
                result.output = run.output
                result.cost_usd = run.cost_usd
                result.tokens = run.tokens
                result.model = run.model or result.model
                result.verification = run.extras.get("verification")
                result.merge = run.extras.get("merge")
                if run.failed or run.error:
                    result.status = ERROR
                    result.error = run.error or "서브에이전트가 실패를 보고함"
                elif result.verification and result.verification.get("ran") and not result.verification.get("passed"):
                    # The worker's own summary does not decide this: ANA ran
                    # the check and it failed, so the task failed.
                    result.status = ERROR
                    result.error = (
                        f"검증 실패: {result.verification.get('command', '')} — "
                        f"{result.verification.get('reason', '')}"
                    )
                elif result.merge and not result.merge.get("merged", True):
                    result.status = ERROR
                    result.error = (
                        f"병합 충돌: 작업 결과는 {result.merge.get('branch', '')} 브랜치에 남아 있다"
                    )
                else:
                    result.status = DONE
            await notify(result)

    pending = {result.task.id for result in results}
    running: dict[str, asyncio.Task] = {}
    try:
        while pending or running:
            # Everything whose dependencies have all finished well.
            ready = [
                task_id
                for task_id in list(pending)
                if all(by_id[need].status == DONE for need in waiting_on[task_id])
            ]
            blocked = [
                task_id
                for task_id in list(pending)
                if task_id not in ready
                and any(
                    by_id[need].status in (ERROR, TIMEOUT, BLOCKED)
                    for need in waiting_on[task_id]
                )
            ]
            for task_id in blocked:
                pending.discard(task_id)
                result = by_id[task_id]
                failed = [
                    need
                    for need in waiting_on[task_id]
                    if by_id[need].status in (ERROR, TIMEOUT, BLOCKED)
                ]
                result.status = BLOCKED
                # Named, not summarized: "blocked" without the reason sends
                # the reader hunting through four other task cards.
                result.error = (
                    "선행 작업이 끝나지 않아 시작하지 않음: "
                    + ", ".join(f"{need}({by_id[need].task.title})" for need in failed)
                )
                await notify(result)
            for task_id in ready:
                pending.discard(task_id)
                running[task_id] = asyncio.create_task(run_one(by_id[task_id]))
            if not running:
                if ready or blocked:
                    # Marking one task blocked can block the tasks waiting on
                    # *it*; go round again before deciding nothing is left.
                    continue
                for task_id in list(pending):
                    pending.discard(task_id)
                    result = by_id[task_id]
                    result.status = BLOCKED
                    result.error = "선행 작업이 끝나지 않아 시작하지 않음"
                    await notify(result)
                break
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task_id, task in list(running.items()):
                if task in done:
                    running.pop(task_id)
                    task.result()  # re-raise cancellation / programming errors
    except BaseException:
        for task in running.values():
            task.cancel()
        if running:
            await asyncio.gather(*running.values(), return_exceptions=True)
        raise
    return results


def _break_cycles(waiting_on: dict[str, set[str]]) -> None:
    """Drops the edges that make a cycle, so a plan that names a loop still
    runs instead of deadlocking.

    A cycle is a mistake in a model-written plan, not a situation to honor: the
    tasks in it would wait for each other forever, and the turn would hang
    until its timeout with nothing to show."""
    settled: set[str] = set()
    for task_id in list(waiting_on):
        stack = [task_id]
        seen: set[str] = set()
        while stack:
            current = stack[-1]
            if current in settled:
                stack.pop()
                continue
            remaining = waiting_on.get(current, set()) - settled
            cyclic = remaining & seen
            if cyclic:
                waiting_on[current] -= cyclic
                remaining = waiting_on.get(current, set()) - settled
            if not remaining:
                settled.add(current)
                seen.discard(current)
                stack.pop()
                continue
            seen.add(current)
            stack.append(next(iter(remaining)))


def summarize(results: list[TaskResult]) -> dict:
    """Counts for the turn-level frame and for metrics."""
    return {
        "total": len(results),
        "done": sum(1 for r in results if r.status == DONE),
        "blocked": sum(1 for r in results if r.status == BLOCKED),
        "failed": sum(1 for r in results if r.status in (ERROR, TIMEOUT)),
        "cost_usd": sum(r.cost_usd for r in results),
        "tokens": sum(r.tokens for r in results),
    }
