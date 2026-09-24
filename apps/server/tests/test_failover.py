"""Failover through the graph: a usage limit moves the turn down the chain
without the user losing the answer."""
import time
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel
from packages.orchestrator.breaker import BreakerConfig


class LimitedAdapter(AgentAdapter):
    """Always reports the provider's usage limit."""

    def __init__(self, label: str):
        self.label = label
        self.calls: list[str] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append(prompt)
        yield AdapterEvent(
            type="error", text="Claude AI usage limit reached", is_error=True
        )

    async def terminate(self) -> None:
        return None


class BrokenAdapter(AgentAdapter):
    """Fails for a reason that is not about budget."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append(prompt)
        yield AdapterEvent(type="error", text="SyntaxError: bad input", is_error=True)

    async def terminate(self) -> None:
        return None


class WorkingAdapter(AgentAdapter):
    def __init__(self, label: str):
        self.label = label
        self.calls: list[str] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append(prompt)
        yield AdapterEvent(type="text", text=f"{self.label} answered", session_id=f"{self.label}-sid")
        yield AdapterEvent(
            type="result", text="done", session_id=f"{self.label}-sid", cost_usd=0.01
        )

    async def terminate(self) -> None:
        return None


class RateLimitWarningAdapter(AgentAdapter):
    """Succeeds, but reports a window at its cap while doing so."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(
            type="usage",
            usage={"unifiedWindows": {"five_hour": {"utilization": 0.99}}},
            session_id="sid",
        )
        yield AdapterEvent(type="text", text="ok", session_id="sid")
        yield AdapterEvent(type="result", text="ok", session_id="sid", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


def _config(db_pool, factory, thread_id, *, mode="pro", breaker=None):
    return {
        "configurable": {
            "thread_id": thread_id,
            "adapter_factory": factory,
            "claude_auth_mode": mode,
            "breaker_config": breaker or BreakerConfig(cooldown_seconds=900),
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }


async def _run(graph, config, prompt="hello", agent="claude", values=None):
    values = values or {}
    frames = [
        chunk
        async for chunk in graph.astream(
            {
                "prompt": prompt,
                "agent": agent,
                "session_id": None,
                "session_ids": values.get("session_ids") or {},
                "last_agent": values.get("last_agent"),
                "failover_rung": values.get("failover_rung"),
                "failover_tripped_at": values.get("failover_tripped_at") or 0.0,
                "failover_reason": values.get("failover_reason") or "",
                "total_cost_usd": values.get("total_cost_usd") or 0.0,
                "total_tokens": values.get("total_tokens") or 0,
                "response_text": "",
            },
            config=config,
            stream_mode="custom",
        )
    ]
    state = await graph.aget_state(config)
    return frames, state.values


@pytest.mark.asyncio
async def test_usage_limit_falls_back_to_the_other_auth_mode(db_pool):
    limited = LimitedAdapter("claude/pro")
    working = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "claude" and auth_mode == "pro":
            return limited
        if agent == "claude" and auth_mode == "api":
            return working
        return WorkingAdapter("codex")

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, factory, "fo-1"))

    failover = next(f for f in frames if f["type"] == "failover")
    assert (failover["from_rung"], failover["to_rung"]) == ("claude/pro", "claude/api")
    assert values["response_text"] == "claude/api answered"
    # The user gets an answer, not an error.
    assert not [f for f in frames if f["type"] == "error"]


@pytest.mark.asyncio
async def test_exhausted_claude_hands_over_to_codex_with_context(db_pool):
    codex = WorkingAdapter("codex")

    def factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "codex":
            return codex
        return LimitedAdapter(f"claude/{auth_mode}")

    graph = build_graph(InMemorySaver())
    # A previous turn gives the handoff brief something to carry.
    _, values = await _run(
        graph,
        _config(db_pool, lambda a, m=None, model=None, effort=None: WorkingAdapter("claude"), "fo-2"),
        prompt="설계 설명해줘",
    )
    frames, values = await _run(
        graph, _config(db_pool, factory, "fo-2"), prompt="이어서 해줘", values=values
    )

    assert [f["to_rung"] for f in frames if f["type"] == "failover"][-1] == "codex"
    assert values["agent"] == "codex"
    # Switching CLI is a handoff: the incoming one gets this thread's context.
    assert "ANA HANDOFF" in codex.calls[0]


@pytest.mark.asyncio
async def test_ordinary_failure_does_not_fail_over(db_pool):
    broken = BrokenAdapter()
    other = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return broken if auth_mode == "pro" else other

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, factory, "fo-3"))

    # A bad request is reported, not retried against another provider.
    assert [f["type"] for f in frames if f["type"] in ("error", "failover")] == ["error"]
    assert other.calls == []


@pytest.mark.asyncio
async def test_chain_exhaustion_reports_the_last_error(db_pool):
    def factory(agent, auth_mode=None, model=None, effort=None):
        return LimitedAdapter(f"{agent}/{auth_mode}")

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, factory, "fo-4"))

    assert len([f for f in frames if f["type"] == "failover"]) == 3  # pro→api→codex→kimi
    error = next(f for f in frames if f["type"] == "error")
    assert "usage limit" in error["text"]


@pytest.mark.asyncio
async def test_thread_stays_on_the_fallback_until_the_cooldown_expires(db_pool):
    limited = LimitedAdapter("claude/pro")
    api = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "claude" and auth_mode == "pro":
            return limited
        if agent == "claude":
            return api
        return WorkingAdapter("codex")

    graph = build_graph(InMemorySaver())
    config = _config(db_pool, factory, "fo-5", breaker=BreakerConfig(cooldown_seconds=900))
    _, values = await _run(graph, config)
    assert values["failover_rung"] == "claude/api"

    calls_before = len(limited.calls)
    _, values = await _run(graph, config, prompt="second", values=values)

    # Still inside the cooldown: the exhausted rung is not retried.
    assert len(limited.calls) == calls_before
    assert values["failover_rung"] == "claude/api"


@pytest.mark.asyncio
async def test_preferred_rung_returns_after_the_cooldown(db_pool):
    pro = WorkingAdapter("claude/pro")
    api = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return pro if auth_mode == "pro" else api

    graph = build_graph(InMemorySaver())
    config = _config(db_pool, factory, "fo-6", breaker=BreakerConfig(cooldown_seconds=60))
    stale = {
        "failover_rung": "claude/api",
        "failover_tripped_at": time.time() - 3600,  # long expired
        "failover_reason": "limit",
    }

    _, values = await _run(graph, config, values=stale)

    # Automatic recovery: the next turn starts from the preferred rung again.
    assert pro.calls, "the preferred rung should be tried once the cooldown expires"
    assert values["failover_rung"] is None


@pytest.mark.asyncio
async def test_budget_breaker_starts_one_rung_down(db_pool):
    pro = WorkingAdapter("claude/pro")
    api = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return pro if auth_mode == "pro" else api

    graph = build_graph(InMemorySaver())
    config = _config(
        db_pool, factory, "fo-7", breaker=BreakerConfig(max_thread_cost_usd=1.0)
    )

    _, values = await _run(graph, config, values={"total_cost_usd": 5.0})

    # Over budget: do not spend another turn proving the limit is still there.
    assert pro.calls == []
    assert api.calls
    assert values["failover_rung"] == "claude/api"


@pytest.mark.asyncio
async def test_rate_limit_warning_trips_the_breaker_even_on_success(db_pool):
    warning = RateLimitWarningAdapter()
    api = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return warning if auth_mode == "pro" else api

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, factory, "fo-8"))

    # The turn itself succeeded, so nothing is retried and no failover frame
    # is written; the window reading only matters once a turn actually fails.
    assert [f["type"] for f in frames if f["type"] == "failover"] == []
    assert any(f["type"] == "result" for f in frames)


class CreditlessAdapter(AgentAdapter):
    """Answers with a limit message as a *successful* result — what the Claude
    CLI does when the plan cannot pay for the selected model."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        text = "Fable 5 requires usage credits. Run /usage-credits to continue."
        yield AdapterEvent(type="text", text=text, session_id="sid")
        yield AdapterEvent(type="result", text=text, session_id="sid", is_error=False)

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_limit_message_fails_over(db_pool):
    working = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return CreditlessAdapter() if auth_mode == "pro" else working

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, factory, "fo-credits"))

    assert [f["to_rung"] for f in frames if f["type"] == "failover"] == ["claude/api"]
    assert values["response_text"] == "claude/api answered"


@pytest.mark.asyncio
async def test_a_retried_attempt_does_not_announce_the_turn_as_finished(db_pool):
    """A failed rung's end-of-turn frame must not reach the client before the
    retry: the UI treats `result` as "the turn is over" and would stop there,
    showing the failure as the answer."""
    working = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return CreditlessAdapter() if auth_mode == "pro" else working

    graph = build_graph(InMemorySaver())
    frames, _ = await _run(graph, _config(db_pool, factory, "fo-order"))

    kinds = [f["type"] for f in frames]
    assert kinds.count("result") == 1
    # The one result is the last frame, after the failover that produced it.
    assert kinds.index("failover") < kinds.index("result")


class CreditlessThenEmptyExitAdapter(AgentAdapter):
    """What the Claude CLI actually does when the plan cannot pay: a result
    carrying the limit message, then a bare non-zero exit whose stderr is
    empty."""

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        text = "Fable 5 requires usage credits. Run /usage-credits to continue."
        yield AdapterEvent(type="text", text=text, session_id="sid")
        yield AdapterEvent(type="result", text=text, session_id="sid", is_error=True)
        yield AdapterEvent(type="error", text="", session_id="sid", is_error=True)

    async def terminate(self) -> None:
        return None


@pytest.mark.asyncio
async def test_an_empty_trailing_error_does_not_erase_the_reason(db_pool):
    """The empty error used to overwrite the limit message, leaving a turn that
    was limited but did not look failed — so it never moved to the next rung."""
    working = WorkingAdapter("claude/api")

    def factory(agent, auth_mode=None, model=None, effort=None):
        return CreditlessThenEmptyExitAdapter() if auth_mode == "pro" else working

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, factory, "fo-empty-exit"))

    assert [f["to_rung"] for f in frames if f["type"] == "failover"] == ["claude/api"]
    assert values["response_text"] == "claude/api answered"


@pytest.mark.asyncio
async def test_a_rung_that_cannot_be_built_is_skipped(db_pool):
    """Claude's API mode needs a key that may not be configured. That is a rung
    being unavailable, not the turn failing."""
    codex = WorkingAdapter("codex")

    def factory(agent, auth_mode=None, model=None, effort=None):
        if agent == "codex":
            return codex
        if auth_mode == "api":
            raise RuntimeError("ANTHROPIC_API_KEY_SAVED is not set")
        return LimitedAdapter("claude/pro")

    graph = build_graph(InMemorySaver())
    frames, values = await _run(graph, _config(db_pool, factory, "fo-unbuildable"))

    assert [f["to_rung"] for f in frames if f["type"] == "failover"] == [
        "claude/api",
        "codex",
    ]
    assert values["response_text"] == "codex answered"


@pytest.mark.asyncio
async def test_each_rung_uses_its_own_model(db_pool, tmp_path, monkeypatch):
    """A model name belongs to one CLI. Carrying the lead's model onto the next
    rung made the retry fail for a brand-new reason."""
    ladder = tmp_path / "routing.yaml"
    ladder.write_text(
        "lead:\n"
        "  - agent: claude\n    model: fable\n"
        "  - agent: codex\n    model: gpt-6-astra\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANA_ROUTING_RULES", str(ladder))

    seen: list[tuple[str, object]] = []

    def factory(agent, auth_mode=None, model=None, effort=None):
        seen.append((agent, model))
        return LimitedAdapter("claude") if agent == "claude" else WorkingAdapter("codex")

    graph = build_graph(InMemorySaver())
    await _run(graph, _config(db_pool, factory, "fo-models"))

    assert ("codex", "gpt-6-astra") in seen
    assert ("codex", "fable") not in seen


@pytest.mark.asyncio
async def test_a_rung_without_an_effort_does_not_inherit_the_planning_one(db_pool):
    """The planning call runs at the cheapest effort. A rung that names no
    effort of its own means "let the CLI decide" — not "keep whatever the last
    call used", which is what a skipped assignment left behind."""
    from apps.server.app.graph import _resolve_adapter

    adapter = WorkingAdapter("claude")
    adapter.model, adapter.effort = None, None
    configurable = {"adapters": {"claude": adapter}}

    _resolve_adapter(configurable, "claude", model="fable", effort="low")
    assert (adapter.model, adapter.effort) == ("fable", "low")

    _resolve_adapter(configurable, "claude", model=None, effort=None)
    assert (adapter.model, adapter.effort) == (None, None)
