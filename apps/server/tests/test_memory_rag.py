import pytest

from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import search_similar_turns, store_turn


@pytest.fixture(scope="module")
def embedding_model():
    return EmbeddingModel()


@pytest.mark.asyncio
async def test_store_then_search_finds_the_most_similar_turn(db_pool, embedding_model):
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="What's the capital of France?", response="Paris.",
        cost_usd=0.01,
    )
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="Write a haiku about the ocean", response="Waves crash on the shore...",
        cost_usd=0.02,
    )

    results = await search_similar_turns(db_pool, embedding_model, "What city is France's capital?", limit=1)

    assert len(results) == 1
    assert results[0]["prompt"] == "What's the capital of France?"
    assert results[0]["response"] == "Paris."
    assert 0.0 <= results[0]["distance"] < 1.0


@pytest.mark.asyncio
async def test_search_on_empty_table_returns_empty_list(db_pool, embedding_model):
    results = await search_similar_turns(db_pool, embedding_model, "anything", limit=3)
    assert results == []


@pytest.mark.asyncio
async def test_search_respects_limit(db_pool, embedding_model):
    for i in range(5):
        await store_turn(
            db_pool, embedding_model,
            thread_id="t1", session_id="s1",
            prompt=f"question number {i}", response=f"answer number {i}",
            cost_usd=None,
        )

    results = await search_similar_turns(db_pool, embedding_model, "question", limit=2)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_excludes_a_specific_unrelated_turn_past_the_distance_cutoff(
    db_pool, embedding_model
):
    """Regression test for the missing relevance threshold: search_similar_
    turns used to always return the top-`limit` nearest rows regardless of
    how (un)related they actually were, which is how an oversized/irrelevant
    stored turn could permanently poison a conversational topic (see
    graph.py's augmented-prompt argv-limit bug). Calibrated empirically with
    packages/memory/embeddings.py's bge-small-en-v1.5: the France/pizza pair
    below has cosine distance ~0.62, well past the default max_distance=0.4,
    while the France/France pair is ~0.16."""
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="What's the capital of France?", response="Paris.",
        cost_usd=0.01,
    )
    await store_turn(
        db_pool, embedding_model,
        thread_id="t1", session_id="s1",
        prompt="Recommend a good pizza dough recipe",
        response="Use 00 flour, water, salt, and yeast; knead for 10 minutes.",
        cost_usd=0.02,
    )

    results = await search_similar_turns(
        db_pool, embedding_model, "What city is France's capital?", limit=5
    )

    prompts = {r["prompt"] for r in results}
    assert "What's the capital of France?" in prompts
    assert "Recommend a good pizza dough recipe" not in prompts
