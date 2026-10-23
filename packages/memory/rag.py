import asyncio
import json
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from packages.memory.embeddings import EmbeddingModel


async def store_turn(
    pool: AsyncConnectionPool,
    embedding_model: EmbeddingModel,
    *,
    thread_id: str,
    session_id: Optional[str],
    prompt: str,
    response: str,
    cost_usd: Optional[float],
    agent: str = "claude",
    meta: Optional[dict] = None,
) -> None:
    # Off the event loop: the embedding is ONNX inference, hundreds of
    # milliseconds of pure CPU, and running it inline stops the server dead —
    # every socket, every gate decision, and the connection pool's own
    # housekeeping with them. On a two-core machine that was long enough for
    # the pool to fail to hand out a connection on the next line, which lost
    # the turn that had just been answered.
    embedding = await asyncio.to_thread(embedding_model.embed_one, f"{prompt}\n{response}")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO turns
                (thread_id, session_id, prompt, response, cost_usd, embedding, agent, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                thread_id, session_id, prompt, response, cost_usd, embedding, agent,
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )


async def thread_history(
    pool: AsyncConnectionPool,
    *,
    thread_id: str,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> list[dict]:
    """Returns one thread's turns oldest-first, for redrawing the chat after a
    browser reload. The server-side conversation already survives a refresh
    (CLI sessions + LangGraph checkpoints); this is what lets the *screen*
    survive it too.

    Newest `limit` rows are selected and then reversed, so a long thread
    shows its most recent turns rather than its first ones.

    `before_id` pages backwards: the newest turn older than that id, which is
    how "이전 대화 더 보기" asks for the page above the one on screen. A
    keyset, not an offset, because turns keep arriving at the other end while
    someone reads."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT prompt, response, agent, cost_usd, created_at, meta, id
            FROM turns
            WHERE thread_id = %s AND (%s::bigint IS NULL OR id < %s::bigint)
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (thread_id, before_id, before_id, limit),
        )
        rows = await cur.fetchall()
    return [
        {
            "prompt": r[0],
            "response": r[1],
            "agent": r[2],
            "cost_usd": r[3],
            "created_at": r[4].isoformat(),
            "meta": r[5] or {},
            "id": r[6],
        }
        for r in reversed(rows)
    ]


async def thread_turn(
    pool: AsyncConnectionPool, *, thread_id: str, turn_id: int
) -> Optional[dict]:
    """One turn, whole. What the transcript asks for when someone opens a tool
    call the history payload only carried a label for."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT prompt, response, agent, cost_usd, created_at, meta, id
            FROM turns
            WHERE thread_id = %s AND id = %s
            """,
            (thread_id, turn_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "prompt": row[0],
        "response": row[1],
        "agent": row[2],
        "cost_usd": row[3],
        "created_at": row[4].isoformat(),
        "meta": row[5] or {},
        "id": row[6],
    }


async def thread_totals(pool: AsyncConnectionPool, *, thread_id: str) -> dict:
    """What the whole thread cost, whichever page of it is on screen.

    Summed in SQL rather than over the returned turns: the transcript is
    paginated now, and a conversation that reopens showing its last ten turns
    must still report what all of it cost."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0),
                   -- Guarded cast: meta is JSONB written by several code
                   -- paths over time, and one row with a non-numeric
                   -- `tokens` would otherwise fail the whole sum.
                   COALESCE(SUM(
                     CASE WHEN meta->>'tokens' ~ '^[0-9]+$'
                          THEN (meta->>'tokens')::bigint ELSE 0 END
                   ), 0),
                   COUNT(*)
            FROM turns
            WHERE thread_id = %s
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
    return {
        "cost_usd": float(row[0] or 0.0),
        "tokens": int(row[1] or 0),
        "turn_count": int(row[2] or 0),
    }


async def list_threads(pool: AsyncConnectionPool, *, limit: int = 50) -> list[dict]:
    """Conversations that have at least one recorded turn, newest first.

    Built from `turns` rather than from the checkpoint tables: a thread the
    user can meaningfully return to is one that produced something, and the
    first prompt is what makes it recognizable in a list."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT t.thread_id,
                   COUNT(*) AS turn_count,
                   MAX(t.created_at) AS last_at,
                   (ARRAY_AGG(t.prompt ORDER BY t.created_at, t.id))[1] AS first_prompt,
                   (ARRAY_AGG(t.agent ORDER BY t.created_at DESC, t.id DESC))[1] AS last_agent
            FROM turns t
            GROUP BY t.thread_id
            ORDER BY MAX(t.created_at) DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {
            "thread_id": r[0],
            "turn_count": r[1],
            "last_at": r[2].isoformat(),
            "title": (r[3] or "").strip().splitlines()[0][:80] if r[3] else "",
            "last_agent": r[4],
        }
        for r in rows
    ]


async def recent_turns(
    pool: AsyncConnectionPool,
    *,
    thread_id: str,
    limit: int = 4,
) -> list[dict]:
    """Returns the newest `limit` turns of one thread, oldest-first, for
    building an agent handoff brief (packages/orchestrator/handoff.py).

    This is deliberately *not* the similarity search above: a handoff needs
    what just happened in this conversation regardless of how semantically
    close it is to the new prompt, which is exactly what the relevance
    threshold in search_similar_turns filters out."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT prompt, response, agent
            FROM turns
            WHERE thread_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (thread_id, limit),
        )
        rows = await cur.fetchall()
    return [
        {"prompt": r[0], "response": r[1], "agent": r[2]} for r in reversed(rows)
    ]


async def search_similar_turns(
    pool: AsyncConnectionPool,
    embedding_model: EmbeddingModel,
    query: str,
    *,
    limit: int = 3,
    max_distance: float = 0.4,
) -> list[dict]:
    """Returns up to `limit` past turns whose cosine distance to `query` is
    strictly less than `max_distance`. Without this cutoff, this function
    always returned the top-`limit` nearest rows regardless of how (un)related
    they actually were -- on a small/early corpus that means truly unrelated
    turns get returned as "relevant" context, and since an oversized returned
    row stays the nearest neighbor for every future semantically-similar
    prompt, an irrelevant match is a self-perpetuating problem, not a one-off.
    0.4 is a reasonable default for bge-small-en-v1.5 (empirically: paired
    Q/A on the same topic lands around 0.15-0.25 cosine distance, unrelated
    topics land around 0.45-0.65 -- see apps/server/tests/test_memory_rag.py)."""
    embedding = await asyncio.to_thread(embedding_model.embed_one, query)
    async with pool.connection() as conn:
        # The turns_embedding_idx ivfflat index (packages/memory/db.py) is
        # created with lists=100 on the *empty* table at migration time, so
        # its list centroids are degenerate for small/test-sized tables.
        # With the pgvector default of ivfflat.probes=1, an index scan then
        # probes essentially one (meaningless) cluster and can silently miss
        # rows that are the true nearest neighbors -- reproduced directly via
        # EXPLAIN ANALYZE: "Index Scan ... rows=0" against a 2-row table.
        # Setting probes to the index's full list count forces an exhaustive
        # scan of all lists, which is correct regardless of how degenerate
        # the centroids are, at the cost of the ANN speedup on large tables.
        await conn.execute("SET LOCAL ivfflat.probes = 100")
        cur = await conn.execute(
            """
            SELECT prompt, response, embedding <=> %s::vector AS distance
            FROM turns
            WHERE embedding <=> %s::vector < %s
            ORDER BY distance
            LIMIT %s
            """,
            (embedding, embedding, max_distance, limit),
        )
        rows = await cur.fetchall()
    return [{"prompt": r[0], "response": r[1], "distance": r[2]} for r in rows]
