import asyncio
import sys
from pathlib import Path

import pytest

from packages.adapters.claude_code import ClaudeCodeAdapter

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_claude.py")


async def _collect(stream):
    return [event async for event in stream]


def test_parse_event_assistant_text():
    event = ClaudeCodeAdapter._parse_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        session_id="sid",
    )
    assert event is not None
    assert event.type == "text"
    assert event.text == "hi"


def test_parse_event_result():
    event = ClaudeCodeAdapter._parse_event(
        {
            "type": "result",
            "result": "hi",
            "session_id": "sid-from-event",
            "total_cost_usd": 0.01,
            "is_error": False,
        },
        session_id="sid",
    )
    assert event is not None
    assert event.type == "result"
    assert event.session_id == "sid-from-event"
    assert event.cost_usd == 0.01
    assert event.is_error is False


def test_parse_event_rate_limit():
    event = ClaudeCodeAdapter._parse_event(
        {"type": "rate_limit_event", "rate_limit_info": {}}, session_id="sid"
    )
    assert event is not None
    assert event.type == "usage"


def test_parse_event_system_is_ignored():
    event = ClaudeCodeAdapter._parse_event({"type": "system", "subtype": "init"}, session_id="sid")
    assert event is None


@pytest.mark.asyncio
async def test_run_stream_yields_text_then_result():
    adapter = ClaudeCodeAdapter(
        workspace_dir="/home/hello/workspace",
        env={"PATH": "/usr/bin:/bin"},
        executable=sys.executable,
    )
    # fake_claude.py ignores argv, so passing it as the first arg after the
    # interpreter works: executable=python, and we monkeypatch args via subclassing
    adapter._extra_args_prefix = [FAKE_CLI]  # see implementation note below

    events = [event async for event in adapter.run_stream("ping")]

    text_events = [e for e in events if e.type == "text"]
    result_events = [e for e in events if e.type == "result"]
    usage_events = [e for e in events if e.type == "usage"]

    assert [e.text for e in text_events] == ["pong"]
    assert len(result_events) == 1
    assert result_events[0].cost_usd == 0.07
    assert len(usage_events) == 1


@pytest.mark.asyncio
async def test_run_stream_drains_stderr_concurrently_without_deadlock():
    # Regression test: fake_claude.py writes >64KiB to stderr in between
    # stdout events. If run_stream() only read stderr after the stdout loop
    # finished, the child would block writing stderr (pipe full) and never
    # reach the remaining stdout events, hanging this test forever. The
    # bounded wait_for ensures a regression fails fast instead of hanging
    # the whole suite.
    adapter = ClaudeCodeAdapter(
        workspace_dir="/home/hello/workspace",
        env={"PATH": "/usr/bin:/bin", "FAKE_CLAUDE_BIG_STDERR": "1"},
        executable=sys.executable,
    )
    adapter._extra_args_prefix = [FAKE_CLI]

    events = await asyncio.wait_for(_collect(adapter.run_stream("ping")), timeout=10)

    text_events = [e for e in events if e.type == "text"]
    result_events = [e for e in events if e.type == "result"]

    assert [e.text for e in text_events] == ["pong"]
    assert len(result_events) == 1
    assert result_events[0].cost_usd == 0.07


@pytest.mark.asyncio
async def test_terminate_without_running_process_is_a_noop():
    adapter = ClaudeCodeAdapter(workspace_dir="/home/hello/workspace", env={})
    await adapter.terminate()  # must not raise


def test_build_args_omits_model_and_effort_when_unset():
    adapter = ClaudeCodeAdapter(workspace_dir="/home/hello/workspace", env={})
    args, _ = adapter._build_args("hi", session_id=None)
    assert "--model" not in args
    assert "--effort" not in args


def test_build_args_includes_model_and_effort_when_set():
    adapter = ClaudeCodeAdapter(
        workspace_dir="/home/hello/workspace", env={}, model="opus", effort="high"
    )
    args, _ = adapter._build_args("hi", session_id=None)
    assert "--model" in args
    assert args[args.index("--model") + 1] == "opus"
    assert "--effort" in args
    assert args[args.index("--effort") + 1] == "high"
