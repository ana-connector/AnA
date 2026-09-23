"""Metrics exposition and the tracing wrapper."""
from typing import AsyncIterator, Optional
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from apps.server.app.graph import build_graph
from apps.server.app.main import app
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.memory.embeddings import EmbeddingModel
from packages.observability import metrics
from packages.observability.tracing import NullSpan, Tracer


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


class WorkingAdapter(AgentAdapter):
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


class LimitedAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="error", text="usage limit reached", is_error=True)

    async def terminate(self) -> None:
        return None


def test_render_returns_openmetrics_body():
    body, content_type = metrics.render()

    assert b"# HELP" in body
    # Classic text format, not OpenMetrics: serving generate_latest() output
    # under the OpenMetrics type makes Prometheus reject the scrape with
    # "data does not end with # EOF".
    assert content_type.startswith("text/plain")


def test_record_turn_increments_counters_and_histogram():
    before = _counter_value(metrics.turns_total, agent="claude", outcome="ok")

    metrics.record_turn(
        "claude", outcome="ok", duration_seconds=3.0, cost_usd=0.5, tokens=120
    )

    assert _counter_value(metrics.turns_total, agent="claude", outcome="ok") == before + 1
    assert _counter_value(metrics.turn_cost_usd_total, agent="claude") >= 0.5
    assert _counter_value(metrics.turn_tokens_total, agent="claude") >= 120


def test_gate_decision_without_a_rule_uses_a_bounded_label():
    metrics.record_gate_decision("allow", "rule", None)

    # A missing rule id must not become an empty or unbounded label value.
    assert _counter_value(
        metrics.gate_decisions_total, verdict="allow", decided_by="rule", rule_id="none"
    ) >= 1


@pytest.mark.asyncio
async def test_a_turn_updates_metrics_end_to_end(db_pool):
    graph = build_graph(InMemorySaver())
    config = {
        "configurable": {
            "thread_id": "obs-1",
            "adapter": WorkingAdapter(),
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }
    before = _counter_value(metrics.turns_total, agent="claude", outcome="ok")

    async for _ in graph.astream(
        {"prompt": "hello", "session_id": None, "response_text": ""},
        config=config,
        stream_mode="custom",
    ):
        pass

    assert _counter_value(metrics.turns_total, agent="claude", outcome="ok") == before + 1


@pytest.mark.asyncio
async def test_failover_is_counted(db_pool):
    graph = build_graph(InMemorySaver())

    def factory(agent, auth_mode=None, model=None, effort=None):
        return LimitedAdapter() if auth_mode == "pro" else WorkingAdapter()

    config = {
        "configurable": {
            "thread_id": "obs-2",
            "adapter_factory": factory,
            "claude_auth_mode": "pro",
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }
    before = _counter_value(
        metrics.failovers_total, from_rung="claude/pro", to_rung="claude/api"
    )

    async for _ in graph.astream(
        {"prompt": "hello", "agent": "claude", "session_id": None, "response_text": ""},
        config=config,
        stream_mode="custom",
    ):
        pass

    assert (
        _counter_value(metrics.failovers_total, from_rung="claude/pro", to_rung="claude/api")
        == before + 1
    )


def test_tracer_is_a_no_op_without_configuration(monkeypatch):
    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)

    tracer = Tracer.from_env()

    assert tracer.enabled is False
    trace = tracer.trace(name="turn", thread_id="t")
    with tracer.span(trace, name="cli.claude") as span:
        assert isinstance(span, NullSpan)
    tracer.flush()  # must not raise


def test_tracer_records_spans_with_a_client():
    calls = []

    class FakeSpan:
        def end(self, **kwargs):
            calls.append(("end", kwargs))

    class FakeTrace:
        def span(self, **kwargs):
            calls.append(("span", kwargs))
            return FakeSpan()

        def update(self, **kwargs):
            calls.append(("update", kwargs))

    class FakeClient:
        def trace(self, **kwargs):
            calls.append(("trace", kwargs))
            return FakeTrace()

        def flush(self):
            calls.append(("flush", {}))

    tracer = Tracer(FakeClient())
    trace = tracer.trace(name="turn", thread_id="t-1", metadata={"a": 1})
    with tracer.span(trace, name="cli.claude/pro"):
        pass
    tracer.flush()

    kinds = [c[0] for c in calls]
    assert kinds == ["trace", "span", "end", "flush"]
    assert calls[0][1]["session_id"] == "t-1"
    assert calls[1][1]["name"] == "cli.claude/pro"
    # The span records how long the section took.
    assert "duration_seconds" in calls[2][1]["metadata"]


def test_a_broken_tracer_never_breaks_a_turn():
    class ExplodingClient:
        def trace(self, **kwargs):
            raise RuntimeError("langfuse is down")

        def flush(self):
            raise RuntimeError("still down")

    tracer = Tracer(ExplodingClient())

    trace = tracer.trace(name="turn", thread_id="t")
    with tracer.span(trace, name="cli.claude"):
        pass
    tracer.flush()  # must not raise


@pytest.fixture
def client(db_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def test_metrics_endpoint_serves_prometheus_exposition(client):
    metrics.record_turn("codex", outcome="ok", duration_seconds=1.0)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "ana_turns_total" in response.text
    assert "ana_active_sessions" in response.text


@pytest.mark.asyncio
async def test_cancelled_turn_is_still_counted(db_pool):
    """A client that disconnects right after the result frame cancels the tail
    of the node (embedding + storage). The turn still happened, so it is still
    counted — with its own outcome label."""
    import asyncio as aio

    class StallingAdapter(AgentAdapter):
        async def run_stream(self, prompt, session_id=None):
            yield AdapterEvent(type="text", text="partial", session_id="sid")
            await aio.sleep(30)

        async def terminate(self):
            return None

    graph = build_graph(InMemorySaver())
    config = {
        "configurable": {
            "thread_id": "obs-3",
            "adapter": StallingAdapter(),
            "pool": db_pool,
            "embedding_model": EmbeddingModel(),
            "transcript": Mock(),
        }
    }
    before = _counter_value(metrics.turns_total, agent="claude", outcome="cancelled")

    async def run():
        async for _ in graph.astream(
            {"prompt": "hello", "session_id": None, "response_text": ""},
            config=config,
            stream_mode="custom",
        ):
            pass

    task = aio.create_task(run())
    await aio.sleep(0.2)
    task.cancel()
    with pytest.raises(aio.CancelledError):
        await task

    assert (
        _counter_value(metrics.turns_total, agent="claude", outcome="cancelled") == before + 1
    )
