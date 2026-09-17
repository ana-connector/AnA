import pytest


@pytest.mark.asyncio
async def test_run_migrations_creates_turns_table(db_pool):
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'turns' ORDER BY column_name"
        )
        columns = {row[0] for row in await cur.fetchall()}
    assert {"id", "thread_id", "session_id", "prompt", "response", "cost_usd", "embedding", "created_at"} <= columns


@pytest.mark.asyncio
async def test_run_migrations_is_idempotent(db_pool):
    from packages.memory.db import run_migrations

    await run_migrations(db_pool)  # second call must not raise
    await run_migrations(db_pool)


@pytest.mark.asyncio
async def test_pool_connection_can_insert_and_read_a_vector(db_pool):
    async with db_pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO turns (thread_id, session_id, prompt, response, cost_usd, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ("t1", "s1", "hi", "hello", 0.01, [0.1] * 384),
        )
        cur = await conn.execute("SELECT prompt, response FROM turns WHERE thread_id = %s", ("t1",))
        row = await cur.fetchone()
    assert row == ("hi", "hello")
