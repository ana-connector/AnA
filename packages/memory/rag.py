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
            WHERE embedding <=> %s::vector < %s
            ORDER BY distance
            LIMIT %s
            """,
            (embedding, embedding, max_distance, limit),
        )
        rows = await cur.fetchall()
    return [{"prompt": r[0], "response": r[1], "distance": r[2]} for r in rows]
