"""Plan parsing: a model's JSON arrives wrapped, fenced, or malformed."""
import pytest

from packages.orchestrator.planner import (
    DELEGATE,
    DIRECT,
    build_planning_prompt,
    build_replan_prompt,
    build_task_prompt,
    build_synthesis_prompt,
    max_tasks,
    parse_plan,
)

DELEGATION = """{
  "mode": "delegate",
  "tasks": [
    {"title": "어댑터 구현", "agent": "codex", "prompt": "CodexAdapter를 구현해라"},
    {"title": "설계 리뷰", "agent": "claude", "prompt": "설계를 리뷰해라"}
  ]
}"""


def test_direct_plan():
    plan = parse_plan('{"mode": "direct"}')

    assert plan.mode == DIRECT
    assert plan.delegating is False


def test_delegation_with_tasks():
    plan = parse_plan(DELEGATION)

    assert plan.mode == DELEGATE
    assert plan.delegating is True
    assert [t.agent for t in plan.tasks] == ["codex", "claude"]
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert plan.tasks[0].title == "어댑터 구현"


def test_json_inside_a_fenced_block_with_prose():
    text = f"알겠습니다. 계획입니다:\n```json\n{DELEGATION}\n```\n필요하면 조정하죠."

    assert parse_plan(text).delegating is True


def test_unparseable_answer_falls_back_to_direct():
    plan = parse_plan("죄송하지만 JSON을 못 만들겠습니다")

    # A planning step that fails costs latency, never the turn.
    assert plan.delegating is False
    assert "단독 처리" in plan.note


def test_empty_answer_falls_back_to_direct():
    assert parse_plan("").delegating is False
    assert parse_plan(None).delegating is False  # type: ignore[arg-type]


def test_delegation_without_tasks_is_direct():
    plan = parse_plan('{"mode": "delegate", "tasks": []}')

    assert plan.delegating is False
    assert "작업이 없어" in plan.note


def test_unknown_agent_normalizes():
    plan = parse_plan(
        '{"mode": "delegate", "tasks": [{"title": "x", "agent": "gpt-9", "prompt": "do it"}]}'
    )

    # Model-invented agent names must not select a subprocess that does not
    # exist.
    assert plan.tasks[0].agent == "auto"


def test_worker_without_agent_uses_auto():
    plan = parse_plan('{"mode": "delegate", "tasks": [{"title": "x", "prompt": "do it"}]}')
    assert plan.tasks[0].agent == "auto"
    assert '"agent"' not in build_planning_prompt("do it")


def test_tasks_without_a_prompt_are_dropped():
    plan = parse_plan(
        '{"mode": "delegate", "tasks": ['
        '{"title": "no prompt", "agent": "codex"},'
        '{"title": "ok", "agent": "codex", "prompt": "work"}]}'
    )

    assert [t.title for t in plan.tasks] == ["ok"]
    assert "제외" in plan.note


def test_task_count_is_capped():
    tasks = ",".join(
        f'{{"title": "t{i}", "agent": "codex", "prompt": "p{i}"}}' for i in range(12)
    )
    plan = parse_plan(f'{{"mode": "delegate", "tasks": [{tasks}]}}', limit=5)

    # The cap is also the concurrency limit: planning more only queues them.
    assert len(plan.tasks) == 5
    assert "제외" in plan.note


def test_the_plan_cap_and_the_concurrency_cap_are_separate(monkeypatch):
    """They stopped being the same number when tasks gained dependencies: a
    five-stage pipeline is five tasks that run two at a time, and capping the
    plan at the concurrency limit truncated exactly the plans worth making."""
    from packages.orchestrator.planner import max_concurrency

    monkeypatch.setenv("ANA_MAX_PLAN_TASKS", "3")
    monkeypatch.setenv("ANA_MAX_SUBAGENTS", "2")
    assert max_tasks() == 3
    assert max_concurrency() == 2

    monkeypatch.setenv("ANA_MAX_PLAN_TASKS", "nonsense")
    monkeypatch.setenv("ANA_MAX_SUBAGENTS", "nonsense")
    assert max_tasks() == 10
    assert max_concurrency() == 5  # the roadmap's resource cap, unchanged


@pytest.mark.parametrize("payload", ['{"mode": "delegate", "tasks": "nope"}', "[1,2,3]"])
def test_malformed_shapes_are_direct(payload):
    assert parse_plan(payload).delegating is False


def test_planning_prompt_states_the_cap_and_the_request():
    prompt = build_planning_prompt("리팩터링 해줘", limit=4)

    # The prompt itself is English (it is read by a model, not a person); the
    # user's request inside it is whatever they typed.
    assert "At most 4 tasks" in prompt
    assert "리팩터링 해줘" in prompt
    assert '{"mode": "direct"}' in prompt


def test_synthesis_prompt_includes_failures():
    prompt = build_synthesis_prompt(
        "원래 요청",
        [
            {"title": "A", "agent": "codex", "status": "done", "output": "결과 A"},
            {"title": "B", "agent": "claude", "status": "timeout", "output": ""},
        ],
    )

    assert "원래 요청" in prompt
    assert "결과 A" in prompt
    # The lead must be able to tell the user what did not finish.
    assert "timeout" in prompt


def test_complete_worker_result_reaches_replan_dependents_and_synthesis():
    result_text = "BEGIN\n" + "내역" * 3000 + "\nEND"
    result = {
        "id": "t1", "title": "긴 보고", "agent": "codex",
        "status": "done", "output": result_text,
    }
    task = parse_plan(
        '{"mode":"delegate","tasks":[{"title":"후속 작업","prompt":"continue"}]}'
    ).tasks[0]

    prompts = [
        build_replan_prompt("요청", [result], ["t1"]),
        build_task_prompt(task, [result]),
        build_synthesis_prompt("요청", [result]),
    ]
    assert all(result_text in prompt for prompt in prompts)
