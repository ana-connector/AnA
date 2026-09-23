"""Task dependencies: the plan is a graph, not one parallel round.

The shape that motivated this: "plan(t1) → backend(t2) / design(t3) →
frontend(t4) → deploy(t5)". With independent-only tasks the lead's single
compliant answer was one task doing everything, which is exactly what it
produced.
"""
import asyncio

import pytest

from packages.orchestrator.planner import parse_plan
from packages.orchestrator.supervisor import (
    BLOCKED,
    DONE,
    ERROR,
    WorkerRun,
    run_tasks,
    summarize,
)

PIPELINE = """
{"mode": "delegate", "tasks": [
  {"id": "plan",     "title": "계획",   "prompt": "p"},
  {"id": "backend",  "title": "백엔드", "prompt": "p", "needs": ["plan"]},
  {"id": "design",   "title": "디자인", "prompt": "p", "needs": ["plan"]},
  {"id": "frontend", "title": "프론트", "prompt": "p", "needs": ["design"]},
  {"id": "deploy",   "title": "배포",   "prompt": "p", "needs": ["backend", "frontend"]}
]}
"""


def _pipeline():
    return parse_plan(PIPELINE, limit=10).tasks


def test_the_plan_keeps_the_order_the_model_asked_for():
    tasks = _pipeline()

    assert [t.needs for t in tasks] == [(), ("t1",), ("t1",), ("t3",), ("t2", "t4")]


def test_needs_are_resolved_to_anas_own_ids():
    """The model numbers its tasks however it likes; ANA renumbers them, and a
    dependency wired to the model's name would point at the wrong task."""
    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"id":"alpha","title":"a","prompt":"p"},'
        '{"id":"beta","title":"b","prompt":"p","needs":["alpha"]}]}'
    )

    assert plan.tasks[1].needs == ("t1",)


@pytest.mark.parametrize(
    "needs",
    [["nobody"], ["t2"], [3, None], "not-a-list"],
)
def test_an_unresolvable_dependency_is_dropped(needs):
    """A task that waits for something that does not exist would wait for
    ever; dropping the edge runs it instead."""
    import json

    plan = parse_plan(
        json.dumps(
            {"mode": "delegate", "tasks": [{"id": "only", "title": "t", "prompt": "p", "needs": needs}]}
        )
    )

    assert plan.tasks[0].needs == ()


async def test_dependencies_run_in_order_and_siblings_run_together():
    tasks = _pipeline()
    started: list[str] = []
    running: set[str] = set()
    overlapped: set[frozenset] = set()

    async def run_worker(task, upstream):
        started.append(task.id)
        running.add(task.id)
        if len(running) > 1:
            overlapped.add(frozenset(running))
        await asyncio.sleep(0.05)
        running.discard(task.id)
        return WorkerRun(output=f"{task.id} done")

    results = await run_tasks(tasks, run_worker)

    assert all(r.status == DONE for r in results)
    # t1 before everything, t4 after t3, t5 last.
    assert started[0] == "t1"
    assert started.index("t4") > started.index("t3")
    assert started[-1] == "t5"
    # The two branches really did overlap.
    assert any({"t2", "t3"} <= set(pair) for pair in overlapped)


async def test_a_task_receives_what_its_dependencies_produced():
    tasks = _pipeline()
    seen: dict[str, list[str]] = {}

    async def run_worker(task, upstream):
        seen[task.id] = sorted(result.task.id for result in upstream)
        return WorkerRun(output=f"{task.id} output")

    await run_tasks(tasks, run_worker)

    assert seen["t1"] == []
    assert seen["t4"] == ["t3"]
    assert seen["t5"] == ["t2", "t4"]


async def test_a_failed_dependency_blocks_what_waited_for_it():
    """Running the next step against a half-finished tree is worse than not
    running it: the blocked task says which task stopped it."""
    tasks = _pipeline()

    async def run_worker(task, upstream):
        if task.id == "t3":  # design fails
            return WorkerRun(output="", error="디자인 실패", failed=True)
        return WorkerRun(output="ok")

    results = await run_tasks(tasks, run_worker)
    by_id = {r.task.id: r for r in results}

    assert by_id["t3"].status == ERROR
    assert by_id["t4"].status == BLOCKED  # frontend waited for design
    assert "t3" in by_id["t4"].error
    assert by_id["t5"].status == BLOCKED  # deploy waited for frontend
    # The independent branch still ran.
    assert by_id["t2"].status == DONE
    assert summarize(results)["blocked"] == 2


async def test_a_cycle_does_not_hang_the_turn():
    """A loop is a mistake in a model-written plan, not a situation to honor:
    honoring it means waiting for the turn timeout with nothing to show."""
    plan = parse_plan(
        '{"mode":"delegate","tasks":['
        '{"id":"a","title":"a","prompt":"p","needs":["b"]},'
        '{"id":"b","title":"b","prompt":"p","needs":["a"]}]}'
    )

    async def run_worker(task, upstream):
        return WorkerRun(output="ok")

    results = await asyncio.wait_for(run_tasks(plan.tasks, run_worker), timeout=5)

    assert [r.status for r in results] == [DONE, DONE]


async def test_the_concurrency_cap_still_holds_across_the_graph():
    plan = parse_plan(
        '{"mode":"delegate","tasks":['
        '{"id":"a","title":"a","prompt":"p"},{"id":"b","title":"b","prompt":"p"},'
        '{"id":"c","title":"c","prompt":"p"},{"id":"d","title":"d","prompt":"p"}]}'
    )
    peak = 0
    running = 0

    async def run_worker(task, upstream):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return WorkerRun(output="ok")

    await run_tasks(plan.tasks, run_worker, max_concurrency=2)

    assert peak <= 2
