"""The Kimi Code CLI adapter: argv, event mapping, and the result it has to
synthesize because the CLI never sends one."""
import json
import os
import sys
from pathlib import Path

import pytest

from packages.adapters.kimi import KimiAdapter

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_kimi.py")


def _adapter(tmp_path, **kwargs) -> KimiAdapter:
    adapter = KimiAdapter(
        workspace_dir=str(tmp_path),
        env={**os.environ, "FAKE_KIMI_SESSION": "session_x"},
        executable=sys.executable,
        **kwargs,
    )
    adapter._extra_args_prefix = [FAKE_CLI]
    return adapter


async def _collect(stream):
    return [event async for event in stream]


def test_argv_uses_prompt_mode_and_stream_json(tmp_path):
    args = _adapter(tmp_path)._build_args("안녕", None)

    assert args[-1] == "안녕"
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
    # -p last before the prompt: the CLI rejects --yolo/--auto with it, and
    # print mode already runs under the `auto` permission policy.
    assert "-p" in args
    assert "--session" not in args


def test_resuming_passes_the_session_id(tmp_path):
    args = _adapter(tmp_path)._build_args("안녕", "session_abc")

    assert args[args.index("--session") + 1] == "session_abc"


def test_model_and_system_prompt(tmp_path):
    args = _adapter(tmp_path, model="kimi-code/k3", system_prompt="역할 지시")._build_args(
        "안녕", None
    )

    assert args[args.index("--model") + 1] == "kimi-code/k3"
    # No --append-system-prompt on this CLI, so the instructions ride in front
    # of the prompt as they do for Codex.
    assert args[-1].startswith("[시스템 지시]\n역할 지시")
    assert args[-1].endswith("안녕")


async def test_text_and_synthesized_result(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KIMI_TEXT", "안녕하세요")
    adapter = _adapter(tmp_path)

    events = await _collect(adapter.run_stream("안녕"))

    kinds = [event.type for event in events]
    assert kinds == ["text", "result"]
    assert events[0].text == "안녕하세요"
    # The CLI ends the stream without a summary; the adapter builds the result
    # the graph records the turn from.
    assert events[1].text == "안녕하세요"
    assert events[1].session_id == "session_x"
    assert events[1].cost_usd is None  # no per-turn price is reported


async def test_tool_events_are_left_to_the_gate(tmp_path, monkeypatch):
    """Tool calls arrive on the stream too, but the PreToolUse hook already
    reports every call to the gate — surfacing both would show each call
    twice."""
    monkeypatch.setenv("FAKE_KIMI_TOOL_CALL", "1")
    adapter = _adapter(tmp_path)

    events = await _collect(adapter.run_stream("ls 해줘"))

    assert [event.type for event in events] == ["text", "result"]


async def test_a_failed_run_reports_stderr_and_no_result(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KIMI_EXIT", "1")
    monkeypatch.setenv("FAKE_KIMI_STDERR", "boom")
    adapter = _adapter(tmp_path)

    events = await _collect(adapter.run_stream("안녕"))

    assert events[-1].type == "error"
    assert "boom" in events[-1].text
    assert not any(event.type == "result" for event in events)


async def test_usage_comes_from_the_session_log(tmp_path, monkeypatch):
    """kimi -p reports no usage at all, so the tokens are read back out of the
    session's wire log once the turn is over."""
    home = tmp_path / "kimi_home"
    session_dir = home / "sessions" / "wd_x" / "session_x" / "agents" / "main"
    session_dir.mkdir(parents=True)
    (session_dir / "wire.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "usage": {
                        "inputOther": 1058,
                        "output": 69,
                        "inputCacheRead": 18944,
                        "inputCacheCreation": 0,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "sessionId": "session_x",
                "sessionDir": str(home / "sessions" / "wd_x" / "session_x"),
                "workDir": str(tmp_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = _adapter(tmp_path)
    adapter.env["KIMI_CODE_HOME"] = str(home)

    events = await _collect(adapter.run_stream("안녕"))

    usage = events[-1].usage
    assert usage["input_tokens"] == 1058
    assert usage["output_tokens"] == 69
    # Cache counts are a breakdown of the input; counting them would double.
    assert usage["cache_read_input_tokens"] == 18944


async def test_a_missing_session_log_is_not_an_error(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.env["KIMI_CODE_HOME"] = str(tmp_path / "nowhere")

    events = await _collect(adapter.run_stream("안녕"))

    assert events[-1].type == "result"
    assert events[-1].usage is None


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"role": "assistant", "content": "hi"}, "text"),
        ({"role": "assistant", "tool_calls": [{"function": {"name": "Bash"}}]}, None),
        ({"role": "tool", "content": "out"}, None),
        ({"role": "meta", "type": "system.version", "version": "2.0.2"}, None),
    ],
)
def test_event_mapping(event, expected):
    parsed, _ = KimiAdapter._parse_event(event, "session_x")

    assert (parsed.type if parsed else None) == expected


def test_the_session_id_arrives_with_the_resume_hint():
    """Unlike the other two CLIs, Kimi states the session id at the *end* of
    the stream, so it is threaded through the parse rather than fixed up
    front."""
    parsed, session_id = KimiAdapter._parse_event(
        {"role": "meta", "type": "session.resume_hint", "session_id": "session_new"}, None
    )

    assert parsed is None
    assert session_id == "session_new"
