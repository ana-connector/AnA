import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async

EMBEDDING_DIMENSIONS = 384

MIGRATIONS = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS turns (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    session_id TEXT,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    cost_usd DOUBLE PRECISION,
    embedding VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turns_embedding_idx ON turns
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- Which agent CLI produced the turn ("claude" | "codex"). Added after the
-- table shipped, so it is an ALTER rather than a column in the CREATE above;
-- existing rows predate dual-agent support and default to 'claude'.
ALTER TABLE turns ADD COLUMN IF NOT EXISTS agent TEXT NOT NULL DEFAULT 'claude';
-- What the reply was made of besides its text: the sub-agent fan-out, and
-- (joined in on read) the tool calls. Stored so reopening a conversation shows
-- the same thing it showed while it ran, instead of text with the work removed.
ALTER TABLE turns ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{{}}'::jsonb;
-- Handoff briefs read the newest turns of one thread (see
-- packages/orchestrator/handoff.py), which is a different access pattern
-- from the embedding index and needs its own.
CREATE INDEX IF NOT EXISTS turns_thread_created_idx ON turns (thread_id, created_at DESC);
"""


async def create_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    async def configure(conn):
        await register_vector_async(conn)

    # register_vector_async requires the `vector` type to already exist in
    # the target database (it looks it up via TypeInfo.fetch and raises
    # "vector type not found in the database" otherwise). Since the pool
    # below eagerly opens min_size connections through `configure` before
    # run_migrations() ever runs, we must ensure the extension exists here
    # first using a standalone bootstrap connection, or pool.open() deadlocks
    # against a fresh database and times out.
    #
    # The bootstrap connection is retried briefly: the official Postgres
    # image starts a short-lived, Unix-socket-only server to run init
    # scripts, then restarts as the real TCP-listening server. A caller
    # (e.g. a test fixture) that considers Postgres "ready" via pg_isready
    # against that transient socket can race the final server's bind on the
    # host-mapped TCP port, so the first real TCP connection attempt here
    # can occasionally see a reset connection.
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            async with await psycopg.AsyncConnection.connect(dsn) as bootstrap_conn:
                await bootstrap_conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await bootstrap_conn.commit()
            break
        except psycopg.OperationalError as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    else:
        raise last_error

    pool = AsyncConnectionPool(
        dsn, min_size=min_size, max_size=max_size, configure=configure, open=False
    )
    await pool.open(wait=True)
    return pool


async def run_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(MIGRATIONS)
