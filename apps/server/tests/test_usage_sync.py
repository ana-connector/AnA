"""The usage sync button: Codex off its log, Claude by one short probe turn."""
import asyncio
import time

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app import usage_sync
from apps.server.app.main import app
from apps.server.app.usage_sync import sync_claude, sync_codex, sync_usage
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.observability.limits import UsageTracker


class FakeProbe(AgentAdapter):
    """A CLI that answers the probe, optionally with a rate-limit event."""

    def __init__(self, *, reported=None, is_error=False, text="ok", raises=None):
        self.reported = reported
        self.is_error = is_error
        self.text = text
        self.raises = raises
        self.prompts: list[str] = []
        self.terminated = 0

    async def run_stream(self, prompt, session_id=None):
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        if self.reported is not None:
            yield AdapterEvent(type="usage", text="", usage=self.reported)
        yield AdapterEvent(
            type="result", text=self.text, is_error=self.is_error,
            cost_usd=0.002, usage={"input_tokens": 120, "output_tokens": 3},
        )

    async def terminate(self):
        self.terminated += 1


class Hanging(AgentAdapter):
    async def run_stream(self, prompt, session_id=None):
        await asyncio.Event().wait()
        yield AdapterEvent(type="result", text="unreachable")

    async def terminate(self):
        return None


# --- codex: free, off its own log ------------------------------------------

def test_codex_windows_come_off_the_rollout_log(monkeypatch):
    tracker = UsageTracker()
    # In the future: a window whose reset instant has passed is a window that
    # has reset, and the tracker drops it (see limits._reported_window).
    resets_at = time.time() + 3600
    monkeypatch.setattr(
        usage_sync, "read_codex_rate_limits",
        lambda: {"five_hour": {"utilization": 0.42, "resets_at": resets_at}},
    )

    result = sync_codex(tracker)

    assert result == {"agent": "codex", "refreshed": True, "source": "rollout log",
                      "cost_usd": 0.0, "tokens": 0}
    window = next(
        w for w in tracker.snapshot()["codex"]["windows"] if w["key"] == "five_hour"
    )
    assert window["utilization"] == pytest.approx(0.42)
    assert window["source"] == "reported"


def test_a_codex_that_never_ran_is_not_an_error(monkeypatch):
    monkeypatch.setattr(usage_sync, "read_codex_rate_limits", lambda: {})

    result = sync_codex(UsageTracker())

    assert result["refreshed"] is False
    assert "롤아웃 로그" in result["reason"]


def test_an_unreadable_log_is_reported_not_raised(monkeypatch):
    def boom():
        raise OSError("permission denied")

    monkeypatch.setattr(usage_sync, "read_codex_rate_limits", boom)

    result = sync_codex(UsageTracker())

    assert result["refreshed"] is False
    assert "permission denied" in result["reason"]


# --- claude: one short probe turn ------------------------------------------

async def test_the_probe_collects_the_rate_limit_event(monkeypatch):
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)
    tracker = UsageTracker()
    probe = FakeProbe(reported={"windows": {"five_hour": {"utilization": 0.77}}})

    result = await sync_claude(tracker, lambda: probe)

    assert result["refreshed"] is True
    assert result["source"] == "probe turn"
    assert probe.prompts == [usage_sync.PROBE_PROMPT]
    assert probe.terminated == 1
    window = next(
        w for w in tracker.snapshot()["claude"]["windows"] if w["key"] == "five_hour"
    )
    assert window["utilization"] == pytest.approx(0.77)
    assert window["source"] == "reported"


async def test_what_the_probe_spends_is_recorded_like_any_other_turn(monkeypatch):
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)
    tracker = UsageTracker()

    result = await sync_claude(
        tracker, lambda: FakeProbe(reported={"windows": {"five_hour": {"utilization": 0.1}}})
    )

    assert result["tokens"] == 123
    assert result["cost_usd"] == pytest.approx(0.002)
    five_hour = next(
        w for w in tracker.snapshot()["claude"]["windows"] if w["key"] == "five_hour"
    )
    assert five_hour["tokens"] == 123


async def test_a_probe_that_reported_nothing_says_so(monkeypatch):
    """The user paid for a request; they are entitled to know it changed nothing."""
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)

    result = await sync_claude(UsageTracker(), lambda: FakeProbe(reported=None))

    assert result["refreshed"] is False
    assert result["reason"]
    assert result["tokens"] == 123  # still counted


async def test_a_limit_message_becomes_the_reason(monkeypatch):
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)

    result = await sync_claude(
        UsageTracker(),
        lambda: FakeProbe(reported=None, is_error=True,
                          text="You've hit your session limit · resets 3:20am (UTC)"),
    )

    assert result["refreshed"] is False
    assert "session limit" in result["reason"]


async def test_a_hanging_probe_gives_up_instead_of_holding_the_request(monkeypatch):
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)
    monkeypatch.setenv("ANA_USAGE_PROBE_TIMEOUT_SECONDS", "0.2")

    result = await sync_claude(UsageTracker(), lambda: Hanging())

    assert result["refreshed"] is False
    assert "응답이 없었습니다" in result["reason"]


async def test_a_cli_that_cannot_be_built_is_a_reason_not_a_crash(monkeypatch):
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)

    def broken():
        raise RuntimeError("no credentials")

    result = await sync_claude(UsageTracker(), broken)

    assert result == {"agent": "claude", "refreshed": False, "reason": "no credentials"}


async def test_the_probe_can_be_turned_off_entirely(monkeypatch):
    monkeypatch.setenv("ANA_USAGE_PROBE", "off")
    called = []

    result = await sync_claude(UsageTracker(), lambda: called.append(1))

    assert result["refreshed"] is False
    assert "ANA_USAGE_PROBE=off" in result["reason"]
    assert called == []  # nothing was spent


async def test_codex_is_synced_even_when_the_claude_probe_fails(monkeypatch):
    """Cheapest first: a probe that hangs must not cost the free half."""
    monkeypatch.delenv("ANA_USAGE_PROBE", raising=False)
    monkeypatch.setenv("ANA_USAGE_PROBE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(
        usage_sync, "read_codex_rate_limits",
        lambda: {"five_hour": {"utilization": 0.5}},
    )
    tracker = UsageTracker()

    results = await sync_usage(tracker, lambda: Hanging())

    assert [r["agent"] for r in results] == ["codex", "claude"]
    assert results[0]["refreshed"] is True
    assert results[1]["refreshed"] is False


# --- the endpoint ----------------------------------------------------------

@pytest_asyncio.fixture
async def prepared_pool(db_pool):
    return db_pool


@pytest.fixture
def client(prepared_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_USAGE_PROBE", "off")  # no CLI in the test environment
    with TestClient(app) as test_client:
        yield test_client


def test_the_endpoint_returns_both_halves_and_the_fresh_snapshot(client, monkeypatch):
    monkeypatch.setattr(
        usage_sync, "read_codex_rate_limits",
        lambda: {"five_hour": {"utilization": 0.33}},
    )

    body = client.post("/usage/sync").json()

    assert [entry["agent"] for entry in body["synced"]] == ["codex", "claude"]
    assert body["synced"][0]["refreshed"] is True
    assert body["synced"][1]["refreshed"] is False  # probe off
    window = next(
        w for w in body["agents"]["codex"]["windows"] if w["key"] == "five_hour"
    )
    assert window["utilization"] == pytest.approx(0.33)
