"""Supervised fan-out: concurrency cap, error isolation, timeout, cancellation."""
import asyncio

import pytest

from packages.orchestrator.planner import PlanTask
from packages.orchestrator.supervisor import (
    DONE,
    ERROR,
    RUNNING,
    TIMEOUT,
    WorkerRun,
    run_tasks,
    summarize,
)


def _tasks(count: int) -> list[PlanTask]:
    return [
        PlanTask(id=f"t{i}", title=f"task {i}", prompt=f"do {i}", agent="codex")
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_all_tasks_run_and_report_output():
    async def run_worker(task):
        return WorkerRun(output=f"{task.id} done", cost_usd=0.01, tokens=10)

    results = await run_tasks(_tasks(3), run_worker)

    assert [r.status for r in results] == [DONE, DONE, DONE]
    assert [r.output for r in results] == ["t0 done", "t1 done", "t2 done"]
    assert summarize(results) == {
        "total": 3,
        "done": 3,
        "failed": 0,
        "cost_usd": pytest.approx(0.03),
        "tokens": 30,
    }


@pytest.mark.asyncio
async def test_concurrency_is_capped():
    active = 0
    peak = 0

    async def run_worker(task):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return WorkerRun(output="ok")

    await run_tasks(_tasks(8), run_worker, max_concurrency=3)

    # The roadmap's cap exists to stop resource blowups; queued tasks still run.
    assert peak <= 3


@pytest.mark.asyncio
async def test_one_failing_task_does_not_sink_the_others():
    async def run_worker(task):
        if task.id == "t1":
            raise RuntimeError("worker exploded")
        return WorkerRun(output="ok")

    results = await run_tasks(_tasks(3), run_worker)

    assert [r.status for r in results] == [DONE, ERROR, DONE]
    assert "exploded" in results[1].error
    assert summarize(results)["failed"] == 1


@pytest.mark.asyncio
async def test_worker_reported_failure_is_an_error_result():
    async def run_worker(task):
        return WorkerRun(output="partial", failed=True, error="usage limit reached")

    results = await run_tasks(_tasks(1), run_worker)

    assert results[0].status == ERROR
    assert results[0].error == "usage limit reached"
    # Whatever it produced before failing is still available to the synthesis.
    assert results[0].output == "partial"


@pytest.mark.asyncio
async def test_slow_task_times_out_without_blocking_the_rest():
    async def run_worker(task):
        if task.id == "t0":
            await asyncio.sleep(5)
        return WorkerRun(output="fast")

    results = await run_tasks(_tasks(2), run_worker, timeout_seconds=0.2)

    assert results[0].status == TIMEOUT
    assert results[1].status == DONE


@pytest.mark.asyncio
async def test_updates_are_announced_for_each_state_change():
    seen: list[tuple[str, str]] = []

    async def on_update(result):
        seen.append((result.task.id, result.status))

    async def run_worker(task):
        return WorkerRun(output="ok")

    await run_tasks(_tasks(2), run_worker, on_update=on_update)

    # running then a terminal state, per task — that is what the UI draws.
    assert seen.count(("t0", RUNNING)) == 1
    assert seen.count(("t0", DONE)) == 1
    assert seen.count(("t1", DONE)) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_to_workers():
    started = asyncio.Event()
    cancelled = []

    async def run_worker(task):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(task.id)
            raise
        return WorkerRun()

    task = asyncio.create_task(run_tasks(_tasks(2), run_worker, max_concurrency=2))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # An orphaned CLI keeps working (and billing) against a conversation
    # nobody is watching.
    assert cancelled


@pytest.mark.asyncio
async def test_empty_task_list_is_a_no_op():
    async def run_worker(task):  # pragma: no cover - must not be called
        raise AssertionError("no tasks to run")

    assert await run_tasks([], run_worker) == []
