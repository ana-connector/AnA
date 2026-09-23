"""Cost and token reporting to the client (graph result frames)."""
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import _tokens_from_usage, build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.orchestrator.timeline import TurnTimeline
from packages.memory.rag import thread_history
from packages.memory.embeddings import EmbeddingModel


class ClaudeLikeAdapter(AgentAdapter):
    """Reports a dollar cost plus token usage on the result event, like the
    `claude` CLI does."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text="hi", session_id="sid")
        yield AdapterEvent(
            type="result",
            text="hi",
            session_id="sid",
            cost_usd=0.02,
            usage={"input_tokens": 100, "output_tokens": 20},
        )

    async def terminate(self) -> None:
        return None


class CodexLikeAdapter(AgentAdapter):
    """Reports tokens on a separate usage event and no cost at all, like the
    `codex` CLI does."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text="hi", session_id="sid")
        yield AdapterEvent(
            type="usage",
            session_id="sid",
            usage={
                "input_tokens": 300,
                "cached_input_tokens": 250,
                "output_tokens": 50,
            },
        )
        yield AdapterEvent(type="result", text="hi", session_id="sid", cost_usd=None)

    async def terminate(self) -> None:
        return None


def test_tokens_from_usage_sums_input_and_output():
    assert _tokens_from_usage({"input_tokens": 10, "output_tokens": 5}) == 15


def test_tokens_from_usage_ignores_cache_breakdown():
    # cached/cache-write counts are a breakdown *of* input_tokens; adding
    # them would double-count.
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 90,
        "cache_write_input_tokens": 10,
        "output_tokens": 5,
    }
    assert _tokens_from_usage(usage) == 105


def test_tokens_from_usage_handles_missing_and_non_token_payloads():
    assert _tokens_from_usage(None) == 0
    assert _tokens_from_usage({}) == 0
    # The Claude CLI's rate_limit_event reports utilization, not tokens.
    assert _tokens_from_usage({"unifiedWindows": {"five_hour": {"utilization": 0.1}}}) == 0


def _config(db_pool, adapter, thread_id):
    return {
        "configurable": {
            "thread_id": thread_id,
            "adapter": adapter,
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }


async def _result_frame(graph, config, prompt="hello"):
    frames = [
        chunk
        async for chunk in graph.astream(
            {"prompt": prompt, "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        )
    ]
    return next(f for f in frames if f["type"] == "result")


@pytest.mark.asyncio
async def test_result_frame_reports_cost_and_tokens(db_pool):
    graph = build_graph(InMemorySaver())
    config = _config(db_pool, ClaudeLikeAdapter(), "usage-1")

    result = await _result_frame(graph, config)

    assert result["cost_usd"] == 0.02
    assert result["total_cost_usd"] == 0.02
    assert result["turn_tokens"] == 120
    assert result["total_tokens"] == 120


@pytest.mark.asyncio
async def test_turn_saves_ordered_blocks_for_history(db_pool):
    graph = build_graph(InMemorySaver())
    config = _config(db_pool, ClaudeLikeAdapter(), "ordered-history")
    timeline = TurnTimeline()
    config["configurable"]["timeline"] = timeline

    await _result_frame(graph, config)

    turns = await thread_history(db_pool, thread_id="ordered-history")
    assert turns[0]["meta"]["blocks"] == [{"kind": "text", "text": "hi"}]


@pytest.mark.asyncio
async def test_totals_accumulate_across_turns(db_pool):
    graph = build_graph(InMemorySaver())
    config = _config(db_pool, ClaudeLikeAdapter(), "usage-2")

    await _result_frame(graph, config, "first")
    second = await _result_frame(graph, config, "second")

    assert second["total_cost_usd"] == pytest.approx(0.04)
    assert second["total_tokens"] == 240


@pytest.mark.asyncio
async def test_codex_style_turn_reports_tokens_without_cost(db_pool):
    graph = build_graph(InMemorySaver())
    config = _config(db_pool, CodexLikeAdapter(), "usage-3")

    frames = [
        chunk
        async for chunk in graph.astream(
            {"prompt": "hello", "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        )
    ]

    usage_frame = next(f for f in frames if f["type"] == "usage")
    result = next(f for f in frames if f["type"] == "result")

    # The raw per-CLI payload is forwarded as-is for display.
    assert usage_frame["usage"]["output_tokens"] == 50
    assert result["cost_usd"] is None
    assert result["total_cost_usd"] == 0.0
    assert result["total_tokens"] == 350
