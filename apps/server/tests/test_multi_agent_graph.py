"""The delegate path through the graph: plan, fan out, synthesize."""
import asyncio
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel
from packages.observability import metrics

PLAN_JSON = (
    '{"mode": "delegate", "tasks": ['
    '{"title": "구현", "agent": "codex", "prompt": "어댑터 구현"},'
    '{"title": "리뷰", "agent": "claude", "prompt": "설계 리뷰"}]}'
)


class ScriptedAdapter(AgentAdapter):
    """Answers with whatever the script says, recording the prompts it saw."""

    def __init__(self, replies: list[str], *, label: str = "lead"):
        self.replies = list(replies)
        self.label = label
        self.prompts: list[str] = []
        self.terminated = 0

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else f"{self.label} ok"
        yield AdapterEvent(type="text", text=reply, session_id=f"{self.label}-sid")
        yield AdapterEvent(
            type="result",
            text=reply,
            session_id=f"{self.label}-sid",
            cost_usd=0.01,
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    async def terminate(self) -> None:
        self.terminated += 1


class FailingWorker(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="error", text="worker died", is_error=True)

    async def terminate(self) -> None:
        return None


def _config(db_pool, lead, worker_factory, thread_id):
    return {
        "configurable": {
            "thread_id": thread_id,
            "adapter": lead,
            "worker_factory": worker_factory,
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }


async def _run(graph, config, *, prompt="큰 작업 해줘", plan_mode=True):
    frames = [
        chunk
        async for chunk in graph.astream(
            {
                "prompt": prompt,
                "agent": "claude",
                "plan_mode": plan_mode,
                "session_id": None,
                "response_text": "",
            },
            config=config,
            stream_mode="custom",
        )
    ]
    state = await graph.aget_state(config)
    return frames, state.values


@pytest.mark.asyncio
async def test_plan_mode_fans_out_and_synthesizes(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "최종 종합 답변"])
    workers: list[ScriptedAdapter] = []

    def worker_factory(agent: str, model=None):
        worker = ScriptedAdapter([f"{agent} 결과"], label=agent)
        workers.append(worker)
        return worker

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-1"))

    plan_frame = next(f for f in frames if f["type"] == "plan")
    assert [t["agent"] for t in plan_frame["tasks"]] == ["codex", "claude"]
    assert len(workers) == 2

    # The lead's second call is the synthesis, and it carries both results.
    synthesis_prompt = lead.prompts[1]
    assert "codex 결과" in synthesis_prompt
    assert "claude 결과" in synthesis_prompt
    assert values["response_text"] == "최종 종합 답변"


@pytest.mark.asyncio
async def test_subagent_frames_track_each_task(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "done"])

    def worker_factory(agent: str, model=None):
        return ScriptedAdapter([f"{agent} 결과"], label=agent)

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, lead, worker_factory, "ma-2"))

    subagent_frames = [f for f in frames if f["type"] == "subagent"]
    statuses = [(f["task"]["id"], f["task"]["status"]) for f in subagent_frames]

    assert ("t1", "running") in statuses and ("t1", "done") in statuses
    assert ("t2", "running") in statuses and ("t2", "done") in statuses
    # The selector can show each worker's own result after it finishes.
    assert all(f["task"]["output"] == "" for f in subagent_frames if f["task"]["status"] == "running")
    assert {f["task"]["id"]: f["task"]["output"] for f in subagent_frames if f["task"]["status"] == "done"} == {
        "t1": "codex 결과",
        "t2": "claude 결과",
    }


@pytest.mark.asyncio
async def test_a_failing_worker_still_produces_an_answer(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "일부 실패를 포함한 답변"])

    def worker_factory(agent: str, model=None):
        return FailingWorker() if agent == "codex" else ScriptedAdapter(["ok"], label=agent)

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-3"))

    failed = [f for f in frames if f["type"] == "subagent" and f["task"]["status"] == "error"]
    assert failed
    assert values["response_text"] == "일부 실패를 포함한 답변"
    # The synthesis prompt must show which task failed rather than hiding it.
    assert "error" in lead.prompts[1]


@pytest.mark.asyncio
async def test_direct_plan_runs_a_single_turn(db_pool):
    lead = ScriptedAdapter(['{"mode": "direct"}', "바로 답변"])
    called = []

    def worker_factory(agent: str, model=None):  # pragma: no cover - must not be called
        called.append(agent)
        raise AssertionError("no delegation expected")

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-4"))

    assert next(f for f in frames if f["type"] == "plan")["mode"] == "direct"
    assert called == []
    assert values["response_text"] == "바로 답변"


@pytest.mark.asyncio
async def test_plan_mode_off_skips_planning_entirely(db_pool):
    lead = ScriptedAdapter(["평범한 답변"])

    def worker_factory(agent: str, model=None):  # pragma: no cover - must not be called
        raise AssertionError("no delegation expected")

    graph = build_graph(InMemorySaver())
    frames, values = await _run(
        graph, _config(db_pool, lead, worker_factory, "ma-5"), plan_mode=False
    )

    # One CLI call, no plan frame: planning costs a call, so it stays opt-in.
    assert len(lead.prompts) == 1
    assert not [f for f in frames if f["type"] == "plan"]
    assert values["response_text"] == "평범한 답변"


@pytest.mark.asyncio
async def test_worker_costs_are_added_to_the_thread_totals(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "done"])

    def worker_factory(agent: str, model=None):
        return ScriptedAdapter(["ok"], label=agent)

    graph = build_graph(InMemorySaver())
    _, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-6"))

    # Synthesis turn (0.01) + two workers (0.01 each). The planning call's cost
    # is not counted: it runs outside the measured turn.
    assert values["total_cost_usd"] == pytest.approx(0.03)
    assert values["total_tokens"] == 45


@pytest.mark.asyncio
async def test_workers_are_terminated_after_the_fan_out(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "done"])
    workers: list[ScriptedAdapter] = []

    def worker_factory(agent: str, model=None):
        worker = ScriptedAdapter(["ok"], label=agent)
        workers.append(worker)
        return worker

    graph = build_graph(InMemorySaver())
    await _run(graph, _config(db_pool, lead, worker_factory, "ma-7"))

    assert all(w.terminated >= 1 for w in workers)


@pytest.mark.asyncio
async def test_subagent_metrics_are_recorded(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, "done"])

    def worker_factory(agent: str, model=None):
        return ScriptedAdapter(["ok"], label=agent)

    before = metrics.subagents_total.labels(agent="codex", status="done")._value.get()
    graph = build_graph(InMemorySaver())
    await _run(graph, _config(db_pool, lead, worker_factory, "ma-8"))

    assert (
        metrics.subagents_total.labels(agent="codex", status="done")._value.get()
        == before + 1
    )


@pytest.mark.asyncio
async def test_without_a_worker_factory_planning_is_skipped(db_pool):
    """Callers that never wire workers (tests, the single-agent path) keep the
    old behavior even if a client asks for plan mode."""
    lead = ScriptedAdapter(["직접 답변"])
    config = {
        "configurable": {
            "thread_id": "ma-9",
            "adapter": lead,
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, config)

    assert not [f for f in frames if f["type"] == "plan"]
    assert values["response_text"] == "직접 답변"
