"""Audit log: append-only enforcement lives in the database, not in code."""
import psycopg
import pytest
import pytest_asyncio

from packages.governance.audit import recent_events, record_event, run_audit_migrations


@pytest_asyncio.fixture
async def audit_pool(db_pool):
    # No cleanup statement: the table refuses DELETE and TRUNCATE by design,
    # so each test scopes its assertions to its own thread_id instead.
    await run_audit_migrations(db_pool)
    return db_pool


@pytest.mark.asyncio
async def test_record_event_returns_row_id(audit_pool):
    event_id = await record_event(
        audit_pool,
        thread_id="t1", agent="claude", cli_session_id="sid",
        tool_name="Bash", tool_input={"command": "rm -rf /"},
        verdict="deny", decided_by="rule",
        rule_id="fs-recursive-force-delete", category="filesystem-destruction",
        reason="matched",
    )
    assert event_id > 0


@pytest.mark.asyncio
async def test_events_come_back_newest_first_with_input(audit_pool):
    for command in ["first", "second"]:
        await record_event(
            audit_pool, thread_id="t2", agent="codex", cli_session_id=None,
            tool_name="Bash", tool_input={"command": command},
            verdict="allow", decided_by="rule",
        )

    events = await recent_events(audit_pool, thread_id="t2")

    assert [e["tool_input"]["command"] for e in events] == ["second", "first"]
    assert events[0]["agent"] == "codex"
    assert events[0]["verdict"] == "allow"


@pytest.mark.asyncio
async def test_events_can_be_filtered_by_thread(audit_pool):
    await record_event(
        audit_pool, thread_id="t3", agent=None, cli_session_id=None,
        tool_name="Bash", tool_input={}, verdict="allow", decided_by="rule",
    )
    await record_event(
        audit_pool, thread_id="other", agent=None, cli_session_id=None,
        tool_name="Bash", tool_input={}, verdict="allow", decided_by="rule",
    )

    events = await recent_events(audit_pool, thread_id="t3")

    assert len(events) == 1
    assert events[0]["thread_id"] == "t3"


@pytest.mark.asyncio
async def test_update_is_refused_by_the_database(audit_pool, postgres_dsn):
    await record_event(
        audit_pool, thread_id="t4", agent="claude", cli_session_id=None,
        tool_name="Bash", tool_input={"command": "x"}, verdict="deny", decided_by="rule",
    )

    # The point of the trigger: an attacker (or a buggy migration) with normal
    # write access still cannot rewrite history.
    with psycopg.connect(postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit_events SET verdict = 'allow' WHERE thread_id = 't4'")


@pytest.mark.asyncio
async def test_delete_is_refused_by_the_database(audit_pool, postgres_dsn):
    await record_event(
        audit_pool, thread_id="t5", agent="claude", cli_session_id=None,
        tool_name="Bash", tool_input={"command": "x"}, verdict="deny", decided_by="rule",
    )

    with psycopg.connect(postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM audit_events WHERE thread_id = 't5'")


@pytest.mark.asyncio
async def test_truncate_is_refused_by_the_database(audit_pool, postgres_dsn):
    await record_event(
        audit_pool, thread_id="t6", agent=None, cli_session_id=None,
        tool_name="Bash", tool_input={}, verdict="allow", decided_by="rule",
    )

    # TRUNCATE skips row-level triggers, so without its own guard one
    # statement would empty the whole log.
    with psycopg.connect(postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE audit_events")


@pytest.mark.asyncio
async def test_migrations_are_idempotent(audit_pool):
    # Runs on every server start.
    await run_audit_migrations(audit_pool)
    await run_audit_migrations(audit_pool)
