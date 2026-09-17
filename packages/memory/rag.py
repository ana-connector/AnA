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
) -> None:
    embedding = embedding_model.embed_one(f"{prompt}\n{response}")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO turns (thread_id, session_id, prompt, response, cost_usd, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (thread_id, session_id, prompt, response, cost_usd, embedding),
        )


async def search_similar_turns(
    pool: AsyncConnectionPool,
    embedding_model: EmbeddingModel,
    query: str,
    *,
    limit: int = 3,
) -> list[dict]:
    embedding = embedding_model.embed_one(query)
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
            ORDER BY distance
            LIMIT %s
            """,
            (embedding, limit),
        )
        rows = await cur.fetchall()
    return [{"prompt": r[0], "response": r[1], "distance": r[2]} for r in rows]
