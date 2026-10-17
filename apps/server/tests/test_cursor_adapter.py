"""Cursor Agent CLI adapter: argv, event mapping, usage normalization."""
import os
import sys
from pathlib import Path

import pytest

from packages.adapters.cursor import CursorAdapter, _normalize_usage
from packages.governance.cli_config import prepare_cursor_home

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_cursor.py")


def _adapter(tmp_path, **kwargs) -> CursorAdapter:
    adapter = CursorAdapter(
        workspace_dir=str(tmp_path),
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        executable=sys.executable,
        **kwargs,
    )
    adapter._extra_args_prefix = [FAKE_CLI]
    return adapter


async def _collect(stream):
    return [event async for event in stream]


def test_argv_print_stream_json_and_force(tmp_path):
    args = _adapter(tmp_path)._build_args("안녕", None)

    assert "-p" in args
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--force" in args
    assert "--trust" in args
    assert args[args.index("--sandbox") + 1] == "disabled"
    assert args[args.index("--workspace") + 1] == str(tmp_path)
    assert "--resume" not in args
    assert args[-1] == "안녕"


def test_resume_and_model_and_system_prompt(tmp_path):
    args = _adapter(
        tmp_path, model="composer-2.5", system_prompt="역할 지시"
    )._build_args("안녕", "chat-abc")

    assert args[args.index("--resume") + 1] == "chat-abc"
    assert args[args.index("--model") + 1] == "composer-2.5"
    assert args[-1].startswith("[시스템 지시]\n역할 지시")
    assert args[-1].endswith("안녕")


def test_normalize_usage_maps_camel_case():
    assert _normalize_usage(
        {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 1}
    ) == {
        "inputTokens": 10,
        "outputTokens": 2,
        "cacheReadTokens": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 1,
    }


def test_parse_init_model():
    event = CursorAdapter._parse_event(
        {"type": "system", "subtype": "init", "model": "Auto", "session_id": "s"},
        "s",
    )
    assert event is not None
    assert event.type == "model"
    assert event.model == "Auto"


def test_parse_assistant_and_result():
    text = CursorAdapter._parse_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
            "session_id": "s",
        },
        "s",
    )
    assert text is not None and text.type == "text" and text.text == "hi"

    result = CursorAdapter._parse_event(
        {
            "type": "result",
            "result": "hi",
            "session_id": "s2",
            "is_error": False,
            "usage": {"inputTokens": 3, "outputTokens": 1},
        },
        "s",
    )
    assert result is not None
    assert result.type == "result"
    assert result.session_id == "s2"
    assert result.usage["input_tokens"] == 3


@pytest.mark.asyncio
async def test_run_stream_yields_model_reasoning_text_result(tmp_path):
    events = await _collect(_adapter(tmp_path).run_stream("ping"))

    kinds = [e.type for e in events]
    assert kinds == ["model", "reasoning", "text", "result"]
    assert events[0].model == "Auto"
    assert events[2].text == "pong"
    assert events[3].session_id == "cursor-test-session"
    assert events[3].usage["input_tokens"] == 100
    assert events[3].cost_usd is None


@pytest.mark.asyncio
async def test_run_stream_non_zero_exit_emits_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CURSOR_EXIT", "1")
    events = await _collect(_adapter(tmp_path).run_stream("ping"))

    assert any(e.type == "error" and e.is_error for e in events)


def test_prepare_cursor_home_symlinks_auth(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"accessToken":"x"}', encoding="utf-8")
    home = prepare_cursor_home(tmp_path / "prepared", source)

    auth = home / ".config" / "cursor" / "auth.json"
    assert auth.is_symlink()
    assert auth.read_text(encoding="utf-8") == '{"accessToken":"x"}'
    assert (home / ".cursor").is_dir()
