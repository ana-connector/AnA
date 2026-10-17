"""OpenCode CLI adapter: argv, event mapping, token/cost summing."""
import json
import os
import sys
from pathlib import Path

import pytest

from packages.adapters.opencode import OpenCodeAdapter, _error_message, _tokens_from_step
from packages.governance.cli_config import prepare_opencode_home
from packages.mcp.config_writer import write_opencode_mcp_config

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_opencode.py")


def _adapter(tmp_path, **kwargs) -> OpenCodeAdapter:
    adapter = OpenCodeAdapter(
        workspace_dir=str(tmp_path),
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        executable=sys.executable,
        **kwargs,
    )
    adapter._extra_args_prefix = [FAKE_CLI]
    return adapter


async def _collect(stream):
    return [event async for event in stream]


def test_argv_run_json_auto_and_thinking(tmp_path):
    args = _adapter(tmp_path)._build_args("안녕", None)

    assert args[args.index("--format") + 1] == "json"
    # Without --auto the CLI rejects its own permission requests; without
    # --thinking it drops reasoning parts entirely.
    assert "--auto" in args
    assert "--thinking" in args
    assert args[args.index("--dir") + 1] == str(tmp_path)
    assert "--session" not in args
    assert "--variant" not in args
    # The prompt is last, behind `--`, so a prompt starting with a dash is a
    # message rather than an unknown flag.
    assert args[-2:] == ["--", "안녕"]


def test_argv_model_variant_session_and_system_prompt(tmp_path):
    args = _adapter(
        tmp_path,
        model="omniroute/tapie/claude-opus-5",
        effort="high",
        system_prompt="역할 지시",
    )._build_args("안녕", "ses_abc")

    # A model id may carry slashes of its own; nothing splits it.
    assert args[args.index("--model") + 1] == "omniroute/tapie/claude-opus-5"
    assert args[args.index("--variant") + 1] == "high"
    assert args[args.index("--session") + 1] == "ses_abc"
    assert args[-1].startswith("[시스템 지시]\n역할 지시")
    assert args[-1].endswith("안녕")


def test_tokens_from_step_flattens_cache():
    assert _tokens_from_step(
        {"tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 5, "write": 3}}}
    ) == {
        "input_tokens": 10,
        "output_tokens": 2,
        "reasoning_tokens": 1,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 3,
    }


def test_error_message_prefers_provider_text():
    assert _error_message({"name": "X", "data": {"message": "모델 없음"}}) == "모델 없음"
    assert _error_message({"name": "ProviderAuthError"}) == "ProviderAuthError"


@pytest.mark.asyncio
async def test_run_stream_sums_steps_into_one_result(tmp_path):
    adapter = _adapter(tmp_path, model="opencode/space-bunny-free")
    events = await _collect(adapter.run_stream("ping"))

    kinds = [e.type for e in events]
    # One model event (the stream never names the model itself), then the
    # reasoning and text parts, then the synthesized result.
    assert kinds == ["model", "reasoning", "text", "result"]
    assert events[0].model == "opencode/space-bunny-free"
    assert events[1].text == "thinking…"
    assert events[2].text == "pong"

    result = events[-1]
    assert result.text == "pong"
    assert result.session_id == "ses_test"
    assert result.is_error is False
    # Both steps, not just the last one; `tokens.total` is a running session
    # figure and is deliberately ignored.
    assert result.usage == {
        "input_tokens": 105,
        "output_tokens": 17,
        "reasoning_tokens": 2,
        "cache_read_input_tokens": 53,
        "cache_creation_input_tokens": 1,
    }
    assert result.cost_usd == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_run_stream_session_error_is_reported_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_ERROR", "1")
    events = await _collect(_adapter(tmp_path).run_stream("ping"))

    errors = [e for e in events if e.type == "error"]
    # The CLI prints the error and *then* exits 1; the exit must not turn one
    # failure into two.
    assert len(errors) == 1
    assert errors[0].is_error and errors[0].text == "boom"


@pytest.mark.asyncio
async def test_repeated_text_part_yields_only_what_is_new(tmp_path):
    adapter = _adapter(tmp_path)
    part = {"id": "prt_x", "type": "text", "time": {"start": 1, "end": 2}}
    lines = [
        json.dumps({"type": "text", "sessionID": "ses_1", "part": {**part, "text": "ab"}}),
        json.dumps({"type": "text", "sessionID": "ses_1", "part": {**part, "text": "abcd"}}),
    ]
    script = (
        "import sys\n"
        f"sys.stdout.write({json.dumps(chr(10).join(lines) + chr(10))})\n"
    )
    adapter._extra_args_prefix = ["-c", script]

    events = await _collect(adapter.run_stream("ping"))
    assert [e.text for e in events if e.type == "text"] == ["ab", "cd"]
    assert [e.text for e in events if e.type == "result"] == ["abcd"]


def test_prepare_opencode_home_symlinks_auth_from_data_dir(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "auth.json").write_text('{"omniroute":{"type":"api"}}', encoding="utf-8")
    home = prepare_opencode_home(tmp_path / "prepared", source)

    auth = home / ".local" / "share" / "opencode" / "auth.json"
    assert auth.is_symlink()
    assert auth.read_text(encoding="utf-8") == '{"omniroute":{"type":"api"}}'
    # The overlay config lands here, so the directory has to exist first.
    assert (home / ".config" / "opencode").is_dir()


def test_mcp_overlay_uses_opencode_shape(tmp_path):
    from packages.mcp.registry import ResolvedServer

    server = ResolvedServer(name="ask", command="echo", args=("hi",), env={})
    path = write_opencode_mcp_config([server], tmp_path / "state", tmp_path / "home")
    payload = json.loads(path.read_text(encoding="utf-8"))

    # OpenCode takes one `command` list under an `mcp` key, not the
    # command/args pair the Claude/Kimi/Cursor files use.
    entry = payload["mcp"]["ask"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert len(entry["command"]) == 1 and entry["command"][0].endswith("ask")
    assert path.name == "ana.json"
