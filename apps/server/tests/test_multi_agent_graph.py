"""The delegate path through the graph: plan, fan out, synthesize."""
import asyncio
from typing import AsyncIterator, Optional
from types import SimpleNamespace
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

# The lead is asked once more after a round finishes, so every scripted lead
# answers the re-planning question too (see planner.build_replan_prompt).
DONE_JSON = '{"mode": "done"}'



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


class LimitedWorker(AgentAdapter):
    async def run_stream(self, prompt: str, session_id=None):
        yield AdapterEvent(type="error", text="You've hit your session limit", is_error=True)

    async def terminate(self) -> None:
        return None


class HangingPlanner(AgentAdapter):
    def __init__(self):
        self.terminated = 0

    async def run_stream(self, prompt: str, session_id=None):
        await asyncio.Event().wait()
        yield AdapterEvent(type="text", text="unreachable")

    async def terminate(self) -> None:
        self.terminated += 1


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
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "최종 종합 답변"])
    workers: list[ScriptedAdapter] = []

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        worker = ScriptedAdapter([f"{agent} 결과"], label=agent)
        workers.append(worker)
        return worker

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-1"))

    plan_frame = next(f for f in frames if f["type"] == "plan")
    assert [t["agent"] for t in plan_frame["tasks"]] == ["codex", "claude"]
    assert len(workers) == 2

    # The lead's second call is the synthesis, and it carries both results.
    synthesis_prompt = lead.prompts[2]
    assert "codex 결과" in synthesis_prompt
    assert "claude 결과" in synthesis_prompt
    assert values["response_text"] == "최종 종합 답변"


@pytest.mark.asyncio
async def test_auto_worker_falls_back_after_session_limit(db_pool):
    lead = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"구현","prompt":"implement"}]}',
        DONE_JSON, "완료",
    ])
    attempted = []

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        attempted.append((agent, model))
        return LimitedWorker() if len(attempted) == 1 else ScriptedAdapter(["성공"], label=agent)

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-limit"))
    assert len(attempted) == 2
    assert attempted[0][1] != attempted[1][1]
    assert any(f["type"] == "failover" and f["task_id"] == "t1" for f in frames)
    result = next(f["task"] for f in frames if f["type"] == "subagent" and f["task"]["status"] == "done")
    assert result["agent"] == attempted[1][0]
    assert result["model"] == attempted[1][1]
    assert values["response_text"] == "완료"


@pytest.mark.asyncio
async def test_planning_limit_switches_lead_before_creating_workers(db_pool):
    codex = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"수정","prompt":"edit files"}]}',
        DONE_JSON, "작업 완료",
    ], label="codex")
    workers = []

    def adapter_factory(agent, auth_mode=None, model=None, effort=None):
        return codex if agent == "codex" else LimitedWorker()

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        workers.append(agent)
        return ScriptedAdapter(["수정됨"], label=agent)

    config = _config(db_pool, codex, worker_factory, "ma-plan-limit")
    config["configurable"]["adapter_factory"] = adapter_factory
    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, config)

    assert workers
    assert any(f["type"] == "failover" and f["to_rung"] == "codex" for f in frames)
    assert values["response_text"] == "작업 완료"
    assert values["failover_rung"] == "codex"


@pytest.mark.asyncio
async def test_stalled_planner_times_out_and_switches_lead(db_pool, monkeypatch):
    monkeypatch.setenv("ANA_PLANNER_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ANA_PLANNER_TOTAL_TIMEOUT_SECONDS", "5")
    hanging = HangingPlanner()
    codex = ScriptedAdapter(['{"mode":"direct"}', "바로 답변"], label="codex")

    def adapter_factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "codex":
            return codex
        return hanging if agent == "claude" else LimitedWorker()

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        raise AssertionError("direct plan must not create a worker")

    config = _config(db_pool, hanging, worker_factory, "ma-plan-timeout")
    config["configurable"]["adapter_factory"] = adapter_factory
    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, config)

    assert hanging.terminated == 1
    assert any(f["type"] == "failover" and "planning produced nothing" in f["text"] for f in frames)
    assert values["response_text"] == "바로 답변"


@pytest.mark.asyncio
async def test_synthesis_failover_keeps_worker_results(db_pool):
    claude = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"수정","prompt":"edit files"}]}',
        DONE_JSON, "You've hit your session limit · resets 8:10am (UTC)",
    ])
    codex = ScriptedAdapter(["작업 결과를 확인했습니다"], label="codex")

    def adapter_factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "codex":
            return codex
        return claude if agent == "claude" else LimitedWorker()

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        return ScriptedAdapter(["파일 수정 완료"], label=agent)

    config = _config(db_pool, claude, worker_factory, "ma-synthesis-limit")
    config["configurable"]["adapter_factory"] = adapter_factory
    graph = build_graph(InMemorySaver())
    _, values = await _run(graph, config)

    assert "파일 수정 완료" in codex.prompts[0]
    assert "Sub-agent results:" in codex.prompts[0]
    assert values["response_text"] == "작업 결과를 확인했습니다"


@pytest.mark.asyncio
async def test_replanning_limit_can_assign_another_worker(db_pool):
    claude = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"id":"t1","title":"검토","prompt":"review"}]}',
        "You've hit your session limit · resets 8:10am (UTC)",
    ])
    codex = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"id":"t2","title":"수정","needs":["t1"],"prompt":"edit"}]}',
        "두 작업 완료",
    ], label="codex")
    worker_prompts = []

    def adapter_factory(agent, auth_mode=None, model=None, effort=None):
        return codex if agent == "codex" else claude if agent == "claude" else LimitedWorker()

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        worker = ScriptedAdapter(["검토 완료" if not worker_prompts else "수정 완료"], label=agent)
        worker_prompts.append(worker)
        return worker

    config = _config(db_pool, claude, worker_factory, "ma-replan-limit")
    config["configurable"]["adapter_factory"] = adapter_factory
    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, config)

    done = [f["task"] for f in frames if f["type"] == "subagent" and f["task"]["status"] == "done"]
    assert [task["id"] for task in done] == ["t1", "t2"]
    assert "검토 완료" in worker_prompts[1].prompts[0]
    assert values["response_text"] == "두 작업 완료"


@pytest.mark.asyncio
async def test_worker_tokens_are_attributed_to_the_worker_model(db_pool):
    lead = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"검토","agent":"kimi","prompt":"review"}]}',
        DONE_JSON, "완료",
    ])
    tracker = Mock()
    tracker.snapshot.return_value = None

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        return ScriptedAdapter(["검토 완료"], label=agent)

    config = _config(db_pool, lead, worker_factory, "ma-kimi-usage")
    config["configurable"]["usage_tracker"] = tracker
    graph = build_graph(InMemorySaver())
    await _run(graph, config)

    # Cost rides along with the tokens now, for the dollar-capped plans.
    tracker.record_tokens.assert_any_call("kimi", 15, usd=0.01)
    tracker.record_tokens.assert_any_call("claude", 15, usd=0.01)


@pytest.mark.asyncio
async def test_bad_verify_command_does_not_repeat_worker(db_pool, tmp_path):
    lead = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"구현","prompt":"build site",'
        '"verify":"Verify that the site works"}]}',
        DONE_JSON, "검증 명령 오류",
    ])
    workers = []

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        worker = ScriptedAdapter(["구현 완료"], label=agent)
        workers.append(worker)
        return worker

    config = _config(db_pool, lead, worker_factory, "ma-bad-check")
    config["configurable"]["workspace_dir"] = str(tmp_path)
    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, config)

    assert len(workers) == 1
    result = next(f["task"] for f in frames if f["type"] == "subagent" and f["task"]["status"] == "error")
    assert result["verification"]["retryable"] is False


@pytest.mark.asyncio
async def test_subagent_frames_track_each_task(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "done"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
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
async def test_worker_final_result_replaces_intermediate_text_for_lead(db_pool):
    complete = "BEGIN\n" + "final" * 1000 + "\nEND"
    lead = ScriptedAdapter([
        '{"mode":"delegate","tasks":[{"title":"보고","prompt":"report"}]}',
        DONE_JSON, "완료",
    ])

    class Worker(AgentAdapter):
        async def run_stream(self, prompt, session_id=None):
            yield AdapterEvent(type="text", text="intermediate progress")
            yield AdapterEvent(type="result", text=complete)

        async def terminate(self):
            return None

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, lead, lambda *args, **kwargs: Worker(), "ma-final-result"))
    done = next(f["task"] for f in frames if f["type"] == "subagent" and f["task"]["status"] == "done")
    assert done["output"] == complete
    assert complete in lead.prompts[-1]
    assert "intermediate progress" not in lead.prompts[-1]


@pytest.mark.asyncio
async def test_a_failing_worker_still_produces_an_answer(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "일부 실패를 포함한 답변"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        return FailingWorker() if agent == "codex" else ScriptedAdapter(["ok"], label=agent)

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-3"))

    failed = [f for f in frames if f["type"] == "subagent" and f["task"]["status"] == "error"]
    assert failed
    assert values["response_text"] == "일부 실패를 포함한 답변"
    # The synthesis prompt must show which task failed rather than hiding it.
    assert "error" in lead.prompts[2]


@pytest.mark.asyncio
async def test_direct_plan_runs_a_single_turn(db_pool):
    lead = ScriptedAdapter(['{"mode": "direct"}', "바로 답변"])
    called = []

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):  # pragma: no cover - must not be called
        called.append(agent)
        raise AssertionError("no delegation expected")

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-4"))

    assert next(f for f in frames if f["type"] == "plan")["mode"] == "direct"
    assert called == []
    assert values["response_text"] == "바로 답변"


@pytest.mark.asyncio
async def test_execute_plan_uses_lead_without_worker_and_revokes_scope(db_pool):
    gate_session = SimpleNamespace(lead_scope=None)

    class ScopedLead(ScriptedAdapter):
        async def run_stream(self, prompt, session_id=None):
            if len(self.prompts) == 1:
                assert gate_session.lead_scope is not None
                assert gate_session.lead_scope.mode == "read"
            async for event in super().run_stream(prompt, session_id):
                yield event

    lead = ScopedLead(['{"mode":"execute","scope":{"mode":"read"}}', "확인했습니다"])

    def worker_factory(*args, **kwargs):
        raise AssertionError("execute must use the lead")

    config = _config(db_pool, lead, worker_factory, "ma-execute")
    config["configurable"]["gate_session"] = gate_session
    frames, values = await _run(build_graph(InMemorySaver()), config)

    assert next(f for f in frames if f["type"] == "plan")["mode"] == "execute"
    assert values["response_text"] == "확인했습니다"
    assert "[EXECUTION SCOPE]" in lead.prompts[1]
    assert gate_session.lead_scope is None


@pytest.mark.asyncio
async def test_plan_mode_off_skips_planning_entirely(db_pool):
    lead = ScriptedAdapter(["평범한 답변"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):  # pragma: no cover - must not be called
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
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "done"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        return ScriptedAdapter(["ok"], label=agent)

    graph = build_graph(InMemorySaver())
    _, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-6"))

    # Synthesis turn (0.01) + two workers (0.01 each). The planning call's cost
    # is not counted: it runs outside the measured turn.
    assert values["total_cost_usd"] == pytest.approx(0.03)
    assert values["total_tokens"] == 45


@pytest.mark.asyncio
async def test_workers_are_terminated_after_the_fan_out(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "done"])
    workers: list[ScriptedAdapter] = []

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        worker = ScriptedAdapter(["ok"], label=agent)
        workers.append(worker)
        return worker

    graph = build_graph(InMemorySaver())
    await _run(graph, _config(db_pool, lead, worker_factory, "ma-7"))

    assert all(w.terminated >= 1 for w in workers)


@pytest.mark.asyncio
async def test_subagent_metrics_are_recorded(db_pool):
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "done"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
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


@pytest.mark.asyncio
async def test_the_lead_can_add_tasks_once_the_first_round_came_back(db_pool, monkeypatch):
    """The plan is a guess until something has run. Between rounds the lead is
    shown the results and may add the step they revealed — here a third task
    that depends on one that already finished."""
    follow_up = (
        '{"mode": "delegate", "tasks": ['
        '{"id": "t3", "title": "마이그레이션", "agent": "codex", "needs": ["t1"],'
        ' "prompt": "add the migration the first task turned out to need"}]}'
    )
    lead = ScriptedAdapter([PLAN_JSON, follow_up, DONE_JSON, "최종 답변"])
    worker_prompts: list[str] = []

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        class Recording(ScriptedAdapter):
            async def run_stream(self, prompt: str, session_id=None):
                worker_prompts.append(prompt)
                async for event in super().run_stream(prompt, session_id):
                    yield event

        return Recording([f"{agent} 결과"], label=agent)

    monkeypatch.setenv("ANA_PLAN_ROUNDS", "3")
    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-rounds"))

    ids = {f["task"]["id"] for f in frames if f["type"] == "subagent"}
    assert "t3" in ids
    # The added task's prompt carries what the task it waited for produced,
    # even though that task ran in an earlier round.
    third = next(p for p in worker_prompts if "migration" in p)
    assert "UPSTREAM RESULTS" in third
    # And the synthesis sees all three results, not just the first round's.
    assert "마이그레이션" in lead.prompts[-1]
    assert values["response_text"] == "최종 답변"


@pytest.mark.asyncio
async def test_rounds_are_bounded(db_pool, monkeypatch):
    """A lead that can always add one more task can also never finish, so the
    number of rounds is capped: with two, the fan-out runs twice and then the
    answer is written whatever the lead would have added next."""
    more = (
        '{"mode": "delegate", "tasks": ['
        '{"title": "또 하나", "agent": "codex", "prompt": "and another"}]}'
    )
    # Would ask for more work again if it were asked again; the cap is what
    # stops it, so exactly one re-planning call happens.
    lead = ScriptedAdapter([PLAN_JSON, more, "최종 답변"])
    runs: list[str] = []

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        runs.append(agent)
        return ScriptedAdapter([f"{agent} 결과"], label=agent)

    monkeypatch.setenv("ANA_PLAN_ROUNDS", "2")
    graph = build_graph(InMemorySaver())
    _, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-bounded"))

    # Two from the first plan, one from the single extra round. Not four.
    assert len(runs) == 3
    assert values["response_text"] == "최종 답변"


@pytest.mark.asyncio
async def test_one_round_keeps_the_old_shape(db_pool, monkeypatch):
    """ANA_PLAN_ROUNDS=1: plan, run, answer — no re-planning call at all."""
    lead = ScriptedAdapter([PLAN_JSON, "최종 답변"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        return ScriptedAdapter([f"{agent} 결과"], label=agent)

    monkeypatch.setenv("ANA_PLAN_ROUNDS", "1")
    graph = build_graph(InMemorySaver())
    _, values = await _run(graph, _config(db_pool, lead, worker_factory, "ma-one-round"))

    assert len(lead.prompts) == 2  # the plan and the synthesis, nothing between
    assert values["response_text"] == "최종 답변"


@pytest.mark.asyncio
async def test_the_turn_reports_the_phase_it_is_in(db_pool):
    """A turn can spend a minute planning and another few running workers before
    any of the answer exists. The browser has nothing to show during that unless
    the server says which phase it is in."""
    lead = ScriptedAdapter([PLAN_JSON, DONE_JSON, "최종 답변"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        return ScriptedAdapter([f"{agent} 결과"], label=agent)

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, lead, worker_factory, "ma-status"))

    phases = [f["text"] for f in frames if f["type"] == "status"]

    # Planning, then the fan-out, then writing the answer — in that order.
    assert phases[0] == "planning"
    assert "delegating" in phases
    assert phases.index("delegating") < phases.index("synthesizing")
    assert phases[-1] == "answering"


@pytest.mark.asyncio
async def test_a_direct_turn_still_says_it_is_answering(db_pool):
    lead = ScriptedAdapter(['{"mode": "direct"}', "바로 답변"])

    def worker_factory(agent: str, model=None, effort=None, scope=None, worker_workspace=None):
        raise AssertionError("no delegation expected")

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, lead, worker_factory, "ma-status-direct"))

    phases = [f["text"] for f in frames if f["type"] == "status"]
    assert phases == ["planning", "answering"]


@pytest.mark.asyncio
async def test_a_streaming_planner_is_not_cut_off_at_the_timeout(db_pool, monkeypatch):
    """The planning budget is silence, not wall clock.

    A lead that is still streaming its plan is working; the old wall-clock cap
    threw the plan away one chunk before it landed."""
    monkeypatch.setenv("ANA_PLANNER_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ANA_PLANNER_TOTAL_TIMEOUT_SECONDS", "30")

    class SlowButTalkingPlanner(AgentAdapter):
        """Takes several times the silence budget, but never goes quiet for it."""

        def __init__(self):
            self.terminated = 0

        async def run_stream(self, prompt: str, session_id=None):
            for chunk in ('{"mode":', '"direct"', "}"):
                await asyncio.sleep(0.6)
                yield AdapterEvent(type="text", text=chunk, session_id="lead-sid")
            yield AdapterEvent(type="result", text="", session_id="lead-sid", cost_usd=0.0)

        async def terminate(self) -> None:
            self.terminated += 1

    planner = SlowButTalkingPlanner()
    answer = ScriptedAdapter(["바로 답변"], label="lead")
    calls = {"n": 0}

    def adapter_factory(agent, auth_mode=None, model=None, effort=None):
        # The planning call first, the answer second: both are the lead.
        calls["n"] += 1
        return planner if calls["n"] == 1 else answer

    def worker_factory(agent, model=None, effort=None, scope=None, worker_workspace=None):
        raise AssertionError("direct plan must not create a worker")

    config = _config(db_pool, planner, worker_factory, "ma-plan-streaming")
    config["configurable"]["adapter_factory"] = adapter_factory
    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, config)

    # 1.8s of streaming against a 1s budget, and it was left alone.
    assert planner.terminated == 0
    assert not [f for f in frames if "planning produced nothing" in (f.get("text") or "")]
    # The plan it streamed was used, so the turn answered rather than failing over.
    assert values["response_text"]
    assert not [f for f in frames if f["type"] == "failover"]
