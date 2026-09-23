"""Supervises the worker fan-out.

Owns the three things that make parallel sub-agents safe to run: a concurrency
cap, a per-task timeout, and error isolation — one worker failing is a task
result, not a turn failure. Cancellation propagates: a cancelled turn kills
every worker subprocess it started, because an orphaned CLI keeps working (and
billing) against a conversation nobody is watching.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from packages.orchestrator.planner import PlanTask

RUNNING = "running"
DONE = "done"
ERROR = "error"
TIMEOUT = "timeout"

DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass
class TaskResult:
    task: PlanTask
    status: str = RUNNING
    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.task.id,
            "title": self.task.title,
            "prompt": self.task.prompt,
            "agent": self.task.agent,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
        }


@dataclass
class WorkerRun:
    """What one worker produced, as the runner reports it back."""

    output: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens: int = 0
    adapter: object = None
    # Set when the worker itself reported a failure rather than raising.
    failed: bool = False
    extras: dict = field(default_factory=dict)


RunWorker = Callable[[PlanTask], Awaitable[WorkerRun]]
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
                run = await asyncio.wait_for(
                    run_worker(result.task), timeout=timeout_seconds
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
                if run.failed or run.error:
                    result.status = ERROR
                    result.error = run.error or "서브에이전트가 실패를 보고함"
                else:
                    result.status = DONE
            await notify(result)

    await asyncio.gather(*(run_one(result) for result in results))
    return results


def summarize(results: list[TaskResult]) -> dict:
    """Counts for the turn-level frame and for metrics."""
    return {
        "total": len(results),
        "done": sum(1 for r in results if r.status == DONE),
        "failed": sum(1 for r in results if r.status in (ERROR, TIMEOUT)),
        "cost_usd": sum(r.cost_usd for r in results),
        "tokens": sum(r.tokens for r in results),
    }
