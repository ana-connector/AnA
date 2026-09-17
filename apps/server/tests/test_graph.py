from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel
from packages.memory.rag import search_similar_turns, store_turn


class FakeAdapter(AgentAdapter):
    def __init__(self, prompts_seen: list[str]):
        self.prompts_seen = prompts_seen

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.prompts_seen.append(prompt)
        yield AdapterEvent(type="text", text="hi there", session_id="cli-sid-1")
        yield AdapterEvent(type="result", text="hi there", session_id="cli-sid-1", cost_usd=0.01)

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_graph_streams_events_and_persists_state(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    prompts_seen: list[str] = []
    adapter = FakeAdapter(prompts_seen)
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "adapter": adapter,
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": Mock(),
        }
    }

    events = [
        chunk
        async for chunk in graph.astream(
            {"prompt": "hello", "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        )
    ]

    assert {"type": "text", "text": "hi there"} in events
    assert any(e["type"] == "result" for e in events)

    state = await graph.aget_state(config)
    assert state.values["session_id"] == "cli-sid-1"

    stored = await search_similar_turns(db_pool, embedding_model, "hello", limit=1)
    assert stored[0]["prompt"] == "hello"
    assert stored[0]["response"] == "hi there"


@pytest.mark.asyncio
async def test_graph_prepends_similar_past_turns_to_the_prompt(db_pool):
    embedding_model = EmbeddingModel()
    await store_turn(
        db_pool, embedding_model,
        thread_id="unrelated", session_id=None,
        prompt="What's the capital of France?", response="Paris.",
        cost_usd=None,
    )

    graph = build_graph(InMemorySaver())
    prompts_seen: list[str] = []
    adapter = FakeAdapter(prompts_seen)
    config = {
        "configurable": {
            "thread_id": "thread-2",
            "adapter": adapter,
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": Mock(),
        }
    }

    async for _ in graph.astream(
        {"prompt": "What city is France's capital?", "session_id": None, "response_text": ""},
        config=config,
        stream_mode="custom",
    ):
        pass

    assert len(prompts_seen) == 1
    assert "Paris" in prompts_seen[0]
    assert "What city is France's capital?" in prompts_seen[0]


@pytest.mark.asyncio
async def test_graph_state_survives_a_fresh_checkpointer_instance(db_pool):
    """Simulates a server restart: a brand new InMemorySaver would NOT
    survive this (it's process-local), so this test uses two separate
    build_graph() calls sharing the SAME checkpointer instance to prove
    thread-scoped state round-trips through it — the real AsyncPostgresSaver
    variant of this guarantee is covered by Task 5's lifespan test against
    real Postgres."""
    from langgraph.checkpoint.memory import InMemorySaver

    shared_checkpointer = InMemorySaver()
    embedding_model = EmbeddingModel()
    config = {"configurable": {"thread_id": "thread-3"}}

    graph_a = build_graph(shared_checkpointer)
    async for _ in graph_a.astream(
        {"prompt": "hi", "session_id": None, "response_text": ""},
        config={
            **config,
            "configurable": {
                **config["configurable"],
                "adapter": FakeAdapter([]),
                "pool": db_pool,
                "embedding_model": embedding_model,
                "transcript": Mock(),
            },
        },
        stream_mode="custom",
    ):
        pass

    graph_b = build_graph(shared_checkpointer)
    state = await graph_b.aget_state(config)
    assert state.values["session_id"] == "cli-sid-1"


class ErrorOnlyAdapter(AgentAdapter):
    """Yields only a type="error" AdapterEvent (no result) — simulates a
    non-zero CLI exit. Unlike RaisingAdapter below, this is a well-formed
    event, not a raised exception."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(
            type="error", text="claude: command not found", session_id="poisoned-sid", is_error=True
        )

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_error_only_turn_still_reaches_transcript_and_does_not_poison_session_id(db_pool):
    """Carries forward test_session_manager.py's two error-path guarantees
    (I1: a turn that ends in an error must still reach the transcript; I2:
    session_id is never adopted from anything but a "result" event) into
    graph.py's call_lead_agent, which has zero coverage of either despite
    reimplementing both."""
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    transcript = Mock()
    config = {
        "configurable": {
            "thread_id": "thread-error-only",
            "adapter": ErrorOnlyAdapter(),
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": transcript,
        }
    }

    async for _ in graph.astream(
        {"prompt": "hello error", "session_id": None, "response_text": ""},
        config=config,
        stream_mode="custom",
    ):
        pass

    # (a) transcript.append was called with the error's text as the response
    transcript.append.assert_called_once()
    _, kwargs = transcript.append.call_args
    assert kwargs["response"] == "claude: command not found"
    assert kwargs["prompt"] == "hello error"

    # (b) a row was stored for that turn
    stored = await search_similar_turns(db_pool, embedding_model, "hello error", limit=1)
    assert stored[0]["prompt"] == "hello error"
    assert stored[0]["response"] == "claude: command not found"

    # (c) session_id is NOT poisoned by the error event's session_id
    state = await graph.aget_state(config)
    assert state.values["session_id"] is None


class RaisingAdapter(AgentAdapter):
    """run_stream raises immediately instead of yielding a well-formed
    type="error" event — the scenario the websocket layer's own
    RaisingAdapter test exercises one layer up."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        raise RuntimeError("boom: adapter exploded")
        yield  # pragma: no cover

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_mid_turn_exception_still_reaches_transcript_before_propagating(db_pool):
    """A raised exception from adapter.run_stream (as opposed to a
    type="error" event) must still produce a transcript/turns record before
    propagating -- previously the append/store_turn calls sat after the
    async-for loop entirely, so an exception lost both records."""
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    transcript = Mock()
    config = {
        "configurable": {
            "thread_id": "thread-raises",
            "adapter": RaisingAdapter(),
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": transcript,
        }
    }

    with pytest.raises(RuntimeError, match="boom"):
        async for _ in graph.astream(
            {"prompt": "hello raises", "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        ):
            pass

    transcript.append.assert_called_once()
    _, kwargs = transcript.append.call_args
    assert "boom" in kwargs["response"]
    assert kwargs["prompt"] == "hello raises"

    stored = await search_similar_turns(db_pool, embedding_model, "hello raises", limit=1)
    assert stored[0]["prompt"] == "hello raises"
    assert "boom" in stored[0]["response"]
