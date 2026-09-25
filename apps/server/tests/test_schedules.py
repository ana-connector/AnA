"""Scheduled turns: the cron expression, the store, and what is due.

The parser is the part worth testing hardest. Everything else in this feature
degrades visibly when it goes wrong -- a job that does not run is noticed --
but a cron field read one way rather than another is a job that runs at 3am
for a month before anybody looks.
"""
from datetime import datetime, timezone

import pytest

from packages.orchestrator.schedules import (
    CronError,
    Schedule,
    create_schedule,
    delete_schedule,
    due_schedules,
    list_schedules,
    next_run_at,
    parse_cron,
    record_run,
    run_schedules_migrations,
    set_enabled,
    update_schedule,
)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# --- the expression ----------------------------------------------------------


@pytest.mark.parametrize(
    "expression,after,expected",
    [
        # Every minute.
        ("* * * * *", "2026-09-25 09:15:30", "2026-09-25 09:16:00"),
        # A fixed time today, and the same time tomorrow once it has passed.
        ("30 9 * * *", "2026-09-25 09:00:00", "2026-09-25 09:30:00"),
        ("30 9 * * *", "2026-09-25 09:30:00", "2026-09-26 09:30:00"),
        # Steps and lists.
        ("*/15 * * * *", "2026-09-25 09:16:00", "2026-09-25 09:30:00"),
        ("0 9,18 * * *", "2026-09-25 10:00:00", "2026-09-25 18:00:00"),
        # Ranges, and a weekday field: 2026-09-25 is a Friday, so the next
        # Monday is the 28th.
        ("0 8 * * 1-5", "2026-09-25 09:00:00", "2026-09-28 08:00:00"),
        # Day of month, rolling into the next month.
        ("0 0 1 * *", "2026-09-25 09:00:00", "2026-10-01 00:00:00"),
        # Sunday is both 0 and 7.
        ("0 0 * * 0", "2026-09-25 09:00:00", "2026-09-27 00:00:00"),
        ("0 0 * * 7", "2026-09-25 09:00:00", "2026-09-27 00:00:00"),
    ],
)
def test_next_run_at(expression, after, expected):
    assert next_run_at(expression, _at(after)) == _at(expected)


def test_a_second_never_lands_on_the_same_minute_twice():
    """The tick runs every minute and asks for "the next run after now"; a
    parser that answered with the current minute would fire forever."""
    now = _at("2026-09-25 09:16:00")

    assert next_run_at("* * * * *", now) == _at("2026-09-25 09:17:00")


@pytest.mark.parametrize(
    "expression",
    ["", "* * * *", "* * * * * *", "61 * * * *", "* 25 * * *", "abc * * * *", "*/0 * * * *"],
)
def test_a_bad_expression_is_refused(expression):
    with pytest.raises(CronError):
        parse_cron(expression)


def test_day_of_month_and_weekday_are_a_union_like_cron():
    """Cron's own oddity: with both set, either matching is enough. Reading it
    as "and" would silently never fire for most pairs."""
    # The 1st, or any Monday.
    assert next_run_at("0 0 1 * 1", _at("2026-09-25 09:00:00")) == _at("2026-09-28 00:00:00")


# --- the store ---------------------------------------------------------------


@pytest.fixture
async def schedules_pool(db_pool):
    await run_schedules_migrations(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM schedules")
    return db_pool


@pytest.mark.asyncio
async def test_a_schedule_round_trips(schedules_pool):
    created = await create_schedule(
        schedules_pool,
        workspace_id="ws-1",
        name="아침 브리핑",
        cron="30 9 * * *",
        prompt="어제 변경사항 요약해줘",
        created_by="user",
    )

    rows = await list_schedules(schedules_pool)

    assert [r.id for r in rows] == [created.id]
    assert rows[0].name == "아침 브리핑"
    assert rows[0].enabled is True
    # Scheduled the moment it is stored, so the list can say when it next runs
    # without anyone asking the parser again.
    assert rows[0].next_run_at is not None


@pytest.mark.asyncio
async def test_an_invalid_expression_never_reaches_the_table(schedules_pool):
    with pytest.raises(CronError):
        await create_schedule(
            schedules_pool,
            workspace_id="ws-1",
            name="깨진 것",
            cron="언제나",
            prompt="아무거나",
            created_by="user",
        )

    assert await list_schedules(schedules_pool) == []


@pytest.mark.asyncio
async def test_only_due_and_enabled_schedules_come_back(schedules_pool):
    due = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="지금", cron="* * * * *",
        prompt="지금", created_by="user",
    )
    later = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="나중", cron="0 0 1 1 *",
        prompt="나중", created_by="user",
    )
    paused = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="꺼둠", cron="* * * * *",
        prompt="꺼둠", created_by="user",
    )
    await set_enabled(schedules_pool, paused.id, False)

    ready = await due_schedules(schedules_pool, now=_at("2100-01-01 00:00:00"))

    ids = [r.id for r in ready]
    assert due.id in ids
    assert later.id in ids  # 2100 is past its next run too
    assert paused.id not in ids


@pytest.mark.asyncio
async def test_a_run_moves_the_schedule_forward(schedules_pool):
    schedule = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="매분", cron="* * * * *",
        prompt="자동", created_by="user",
    )

    await record_run(schedules_pool, schedule.id, ok=True, detail="", now=_at("2026-09-25 09:16:00"))
    rows = await list_schedules(schedules_pool)

    assert rows[0].last_run_at == _at("2026-09-25 09:16:00")
    assert rows[0].next_run_at == _at("2026-09-25 09:17:00")
    assert rows[0].last_status == "ok"


@pytest.mark.asyncio
async def test_a_failed_run_is_recorded_and_still_reschedules(schedules_pool):
    """A job that failed once is not a job that should stop: the reason is
    kept where the list shows it, and the next run stands."""
    schedule = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="매분", cron="* * * * *",
        prompt="자동", created_by="user",
    )

    await record_run(
        schedules_pool, schedule.id, ok=False, detail="workspace is gone",
        now=_at("2026-09-25 09:16:00"),
    )
    rows = await list_schedules(schedules_pool)

    assert rows[0].last_status == "error"
    assert rows[0].last_detail == "workspace is gone"
    assert rows[0].next_run_at == _at("2026-09-25 09:17:00")


@pytest.mark.asyncio
async def test_editing_the_expression_reschedules(schedules_pool):
    schedule = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="매분", cron="* * * * *",
        prompt="자동", created_by="user",
    )
    before = (await list_schedules(schedules_pool))[0].next_run_at

    await update_schedule(schedules_pool, schedule.id, cron="0 0 1 1 *")
    after = (await list_schedules(schedules_pool))[0].next_run_at

    assert after > before


@pytest.mark.asyncio
async def test_deleting_removes_it(schedules_pool):
    schedule = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="임시", cron="* * * * *",
        prompt="자동", created_by="user",
    )

    assert await delete_schedule(schedules_pool, schedule.id) is True
    assert await list_schedules(schedules_pool) == []
    assert await delete_schedule(schedules_pool, schedule.id) is False


@pytest.mark.asyncio
async def test_schedules_can_be_listed_for_one_workspace(schedules_pool):
    await create_schedule(
        schedules_pool, workspace_id="ws-1", name="하나", cron="* * * * *",
        prompt="a", created_by="user",
    )
    await create_schedule(
        schedules_pool, workspace_id="ws-2", name="둘", cron="* * * * *",
        prompt="b", created_by="agent",
    )

    assert [r.name for r in await list_schedules(schedules_pool, workspace_id="ws-2")] == ["둘"]
    assert isinstance((await list_schedules(schedules_pool))[0], Schedule)


# --- the tick ----------------------------------------------------------------


class _Runner:
    def __init__(self, busy: bool = False):
        self.busy = busy
        self.submitted: list[tuple] = []

    async def submit(self, prompt, agent, plan_mode):
        self.submitted.append((prompt, agent, plan_mode))


@pytest.mark.asyncio
async def test_a_due_schedule_submits_its_prompt(schedules_pool, monkeypatch):
    from types import SimpleNamespace

    from apps.server.app import scheduler

    schedule = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="아침", cron="* * * * *",
        prompt="어제 요약", created_by="user",
    )
    runner = _Runner()

    async def fake_ensure_runner(app, workspace_id):
        assert workspace_id == "ws-1"
        return runner

    monkeypatch.setattr(
        "apps.server.app.websocket.ensure_runner", fake_ensure_runner, raising=False
    )
    app = SimpleNamespace(state=SimpleNamespace(pool=schedules_pool))

    started = await scheduler.run_due(app, now=_at("2100-01-01 00:00:00"))

    assert started == [schedule.id]
    # "auto" and planning on: a scheduled turn is an ordinary one.
    assert runner.submitted == [("어제 요약", "auto", True)]
    assert (await list_schedules(schedules_pool))[0].last_status == "ok"


@pytest.mark.asyncio
async def test_a_busy_conversation_is_skipped_not_queued(schedules_pool, monkeypatch):
    """Two copies of the same job running at once is how a schedule becomes an
    incident. The slot is left alone so the next tick tries again."""
    from types import SimpleNamespace

    from apps.server.app import scheduler

    await create_schedule(
        schedules_pool, workspace_id="ws-1", name="아침", cron="* * * * *",
        prompt="어제 요약", created_by="user",
    )
    runner = _Runner(busy=True)

    async def fake_ensure_runner(app, workspace_id):
        return runner

    monkeypatch.setattr(
        "apps.server.app.websocket.ensure_runner", fake_ensure_runner, raising=False
    )
    app = SimpleNamespace(state=SimpleNamespace(pool=schedules_pool))

    started = await scheduler.run_due(app, now=_at("2100-01-01 00:00:00"))

    assert started == []
    assert runner.submitted == []
    # Still due, so the next tick picks it up again.
    assert await due_schedules(schedules_pool, now=_at("2100-01-01 00:00:00"))


@pytest.mark.asyncio
async def test_one_broken_job_does_not_stop_the_others(schedules_pool, monkeypatch):
    from types import SimpleNamespace

    from apps.server.app import scheduler

    broken = await create_schedule(
        schedules_pool, workspace_id="gone", name="깨진 것", cron="* * * * *",
        prompt="아무거나", created_by="user",
    )
    ok = await create_schedule(
        schedules_pool, workspace_id="ws-1", name="멀쩡", cron="* * * * *",
        prompt="요약", created_by="user",
    )
    runner = _Runner()

    async def fake_ensure_runner(app, workspace_id):
        if workspace_id == "gone":
            raise RuntimeError("no such workspace")
        return runner

    monkeypatch.setattr(
        "apps.server.app.websocket.ensure_runner", fake_ensure_runner, raising=False
    )
    app = SimpleNamespace(state=SimpleNamespace(pool=schedules_pool))

    started = await scheduler.run_due(app, now=_at("2100-01-01 00:00:00"))

    assert started == [ok.id]
    rows = {row.id: row for row in await list_schedules(schedules_pool)}
    assert rows[broken.id].last_status == "error"
    assert "no such workspace" in rows[broken.id].last_detail
    # And it is rescheduled rather than abandoned.
    assert rows[broken.id].next_run_at is not None


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("* * * * *", "매분"),
        ("0 * * * *", "매시 0분"),
        ("30 9 * * *", "매일 09:30"),
        ("0 9 * * 1-5", "월화수목금요일 09:00"),
        ("0 0 1 * *", "매월 1일 00:00"),
        # A restricted month has no phrase, so it stays the expression rather
        # than being described as something it is not.
        ("0 4 1 1 *", "0 4 1 1 *"),
        ("0 0 1 * 1", "0 0 1 * 1"),
    ],
)
def test_describe_says_what_the_expression_means(expression, expected):
    from packages.orchestrator.schedules import describe

    assert describe(expression) == expected
