"""Dual-agent routing and context handoff in the graph
(docs/plans/2026-09-22-dual-cli-orchestration.md)."""
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel


class RecordingAdapter(AgentAdapter):
    """Records every (prompt, session_id) it is handed, and reports a session
    id of its own so per-agent session continuity is observable."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[str, Optional[str]]] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append((prompt, session_id))
        sid = f"{self.name}-sid"
        yield AdapterEvent(type="text", text=f"{self.name} says hi", session_id=sid)
        yield AdapterEvent(type="result", text=f"{self.name} says hi", session_id=sid)

    async def terminate(self) -> None:
        return None


def _config(db_pool, embedding_model, adapters, thread_id):
    return {
        "configurable": {
            "thread_id": thread_id,
            "adapters": adapters,
            "pool": db_pool,
            "embedding_model": embedding_model,
            "transcript": Mock(),
        }
    }


async def _run(graph, config, prompt, agent, values=None):
    values = values or {}
    async for _ in graph.astream(
        {
            "prompt": prompt,
            "agent": agent,
            "session_id": values.get("session_id"),
            "session_ids": values.get("session_ids") or {},
            "last_agent": values.get("last_agent"),
            "response_text": "",
        },
        config=config,
        stream_mode="custom",
    ):
        pass
    state = await graph.aget_state(config)
    return state.values


@pytest.mark.asyncio
async def test_graph_routes_turn_to_selected_agent(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-1")

    values = await _run(graph, config, "hello", "codex")

    assert adapters["codex"].calls, "the selected agent must run the turn"
    assert adapters["claude"].calls == []
    assert values["last_agent"] == "codex"
    assert values["session_ids"]["codex"] == "codex-sid"


@pytest.mark.asyncio
async def test_each_agent_resumes_its_own_session(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-2")

    values = await _run(graph, config, "first", "claude")
    values = await _run(graph, config, "second", "codex", values)
    values = await _run(graph, config, "third", "claude", values)

    # claude's second turn resumes claude's own session id, never codex's.
    assert adapters["claude"].calls[0][1] is None
    assert adapters["claude"].calls[1][1] == "claude-sid"
    # codex's only turn started fresh even though claude already had a session.
    assert adapters["codex"].calls[0][1] is None
    assert values["session_ids"] == {"claude": "claude-sid", "codex": "codex-sid"}


@pytest.mark.asyncio
async def test_agent_switch_hands_over_recent_context(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-3")

    values = await _run(graph, config, "설계를 설명해줘", "claude")
    await _run(graph, config, "이어서 구현해줘", "codex", values)

    handoff_prompt = adapters["codex"].calls[0][0]
    assert "핸드오프" in handoff_prompt
    assert "claude" in handoff_prompt
    # The brief carries the previous turn's actual prompt and response.
    assert "설계를 설명해줘" in handoff_prompt
    assert "claude says hi" in handoff_prompt
    assert "이어서 구현해줘" in handoff_prompt


@pytest.mark.asyncio
async def test_same_agent_turn_gets_no_handoff_block(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-4")

    values = await _run(graph, config, "first", "claude")
    await _run(graph, config, "second", "claude", values)

    assert "핸드오프" not in adapters["claude"].calls[1][0]


@pytest.mark.asyncio
async def test_result_events_carry_the_agent_name(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-5")

    events = [
        chunk
        async for chunk in graph.astream(
            {
                "prompt": "hello",
                "agent": "codex",
                "session_id": None,
                "session_ids": {},
                "last_agent": None,
                "response_text": "",
            },
            config=config,
            stream_mode="custom",
        )
    ]

    result = next(e for e in events if e["type"] == "result")
    assert result["agent"] == "codex"


@pytest.mark.asyncio
async def test_unknown_agent_falls_back_to_default(db_pool):
    embedding_model = EmbeddingModel()
    graph = build_graph(InMemorySaver())
    adapters = {"claude": RecordingAdapter("claude"), "codex": RecordingAdapter("codex")}
    config = _config(db_pool, embedding_model, adapters, "agents-thread-6")

    values = await _run(graph, config, "hello", "nonexistent-agent")

    assert adapters["claude"].calls, "unknown agent must normalize to the default"
    assert values["last_agent"] == "claude"
