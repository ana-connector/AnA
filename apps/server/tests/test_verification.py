"""The verification loop: ANA checks the work instead of taking the worker's
word for it."""
import pytest

from packages.orchestrator import verification
from packages.orchestrator.planner import parse_plan
from packages.orchestrator.supervisor import DONE, ERROR, TaskResult, WorkerRun, run_tasks


async def test_a_passing_command_passes(tmp_path):
    result = await verification.verify("true", str(tmp_path))

    assert result.ran and result.passed
    assert not result.failed


async def test_a_failing_command_carries_its_output(tmp_path):
    result = await verification.verify("echo boom >&2; exit 3", str(tmp_path))

    assert result.failed
    assert "boom" in result.output
    assert "3" in result.reason


async def test_a_command_the_rules_forbid_never_runs(tmp_path):
    """`verify` is model-written text heading for a shell. It goes through the
    same rule set as any agent command, so a plan cannot smuggle one past the
    gate."""
    canary = tmp_path / "canary"
    result = await verification.verify(f"rm -rf {canary}", str(tmp_path))

    assert not result.ran
    assert "거버넌스" in result.reason


async def test_an_ask_verdict_is_refused_too(tmp_path):
    """No human is watching a verification command, so "ask the user" is not
    an answer this path can use."""
    result = await verification.verify("curl https://example.com", str(tmp_path))

    assert not result.ran


async def test_a_hanging_command_is_killed(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("ANA_VERIFY_TIMEOUT_SECONDS", "1")
    started = time.monotonic()

    result = await verification.verify("sleep 20", str(tmp_path))

    assert result.ran and not result.passed
    assert "초" in result.reason
    # And it really stopped waiting — the point of the timeout is that a
    # wedged check does not hold the turn for its own duration.
    assert time.monotonic() - started < 10


def test_the_retry_prompt_carries_the_evidence():
    result = verification.VerificationResult(
        ran=True, passed=False, command="pytest -q", output="E assert 1 == 2",
        reason="종료 코드 1",
    )

    retried = verification.retry_prompt("원래 작업", result)

    assert retried.startswith("원래 작업")
    assert "pytest -q" in retried and "assert 1 == 2" in retried


def test_a_plan_can_name_the_command_that_proves_the_task():
    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"title":"고치기","prompt":"고쳐라",'
        '"verify":"pytest -q"},{"title":"조사","prompt":"읽어라"}]}'
    )

    assert plan.tasks[0].verify == "pytest -q"
    assert plan.tasks[1].verify == ""  # prose tasks have nothing to check


async def test_a_failed_check_fails_the_task_however_the_worker_wrote_it_up():
    """The worker's summary is a claim; the check is the fact."""
    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"title":"t","prompt":"p","verify":"pytest -q"}]}'
    )

    async def run_worker(task, upstream=()):
        return WorkerRun(
            output="다 고쳤습니다",
            extras={
                "verification": {
                    "ran": True,
                    "passed": False,
                    "command": "pytest -q",
                    "reason": "종료 코드 1",
                }
            },
        )

    results = await run_tasks(plan.tasks, run_worker)

    assert results[0].status == ERROR
    assert "검증 실패" in results[0].error


async def test_a_conflicted_merge_fails_the_task_and_names_the_branch():
    plan = parse_plan('{"mode":"delegate","tasks":[{"title":"t","prompt":"p"}]}')

    async def run_worker(task, upstream=()):
        return WorkerRun(
            output="했습니다",
            extras={"merge": {"merged": False, "branch": "ana/th-t1", "reason": "conflict"}},
        )

    results = await run_tasks(plan.tasks, run_worker)

    assert results[0].status == ERROR
    assert "ana/th-t1" in results[0].error


async def test_a_passing_check_leaves_the_task_done():
    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"title":"t","prompt":"p","verify":"true"}]}'
    )

    async def run_worker(task, upstream=()):
        return WorkerRun(
            output="ok",
            extras={"verification": {"ran": True, "passed": True, "command": "true"}},
        )

    results = await run_tasks(plan.tasks, run_worker)

    assert results[0].status == DONE
    assert results[0].as_dict()["verification"]["passed"] is True


def test_a_plan_can_carry_a_check_for_the_merged_tree():
    """Per-task checks each pass on their own branch; this is the one that
    catches two of them being fine separately and broken together."""
    plan = parse_plan(
        '{"mode":"delegate","verify":"docker compose config -q",'
        '"tasks":[{"id":"a","title":"t","prompt":"p"}]}'
    )

    assert plan.verify == "docker compose config -q"


def test_a_direct_plan_has_no_turn_check():
    assert parse_plan('{"mode":"direct"}').verify == ""


def test_a_task_can_name_a_skill_and_gets_its_procedure():
    from packages.orchestrator.planner import build_task_prompt

    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"id":"a","title":"리뷰","prompt":"이 diff를 봐라",'
        '"skill":"code-review"}]}'
    )
    prompt = build_task_prompt(plan.tasks[0], [])

    assert plan.tasks[0].skill == "code-review"
    assert "[스킬: code-review]" in prompt
    assert "이 diff를 봐라" in prompt
    # The skill's own input placeholders never reach the agent verbatim.
    assert "{{" not in prompt


def test_a_skill_the_library_does_not_have_is_dropped():
    """A name the model invented would otherwise reach the worker as "follow
    the X procedure" with no X to follow."""
    plan = parse_plan(
        '{"mode":"delegate","tasks":[{"id":"a","title":"t","prompt":"p","skill":"made-up"}]}'
    )

    assert plan.tasks[0].skill == ""
