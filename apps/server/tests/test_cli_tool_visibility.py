"""Tool calls from the CLIs ANA's gate cannot hook.

Every tool record in ANA used to come from the PreToolUse hook, which only
Claude Code, Codex and Kimi have. A Cursor or OpenCode turn therefore showed
narration and a final answer with nothing in between: the file it edited and
the command it ran were in no frame the browser received and in no transcript
it could reopen. These CLIs report their calls in their own streams, so the
records come from there instead -- marked `cli:<agent>`, because nothing
screened them.
"""
import os
import sys
from pathlib import Path

import pytest

from apps.server.app.graph import _TurnOutcome, _run_worker, _stream_turn, _worker_tool
from packages.adapters.base import cli_tool_name, cli_tool_record
from packages.adapters.cursor import CursorAdapter
from packages.adapters.opencode import OpenCodeAdapter
from packages.orchestrator.timeline import TurnTimeline

FAKE_CURSOR = str(Path(__file__).parent / "fixtures" / "fake_cursor.py")
FAKE_OPENCODE = str(Path(__file__).parent / "fixtures" / "fake_opencode.py")


def _cursor(tmp_path) -> CursorAdapter:
    adapter = CursorAdapter(
        workspace_dir=str(tmp_path),
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        executable=sys.executable,
    )
    adapter._extra_args_prefix = [FAKE_CURSOR]
    return adapter


def _opencode(tmp_path) -> OpenCodeAdapter:
    adapter = OpenCodeAdapter(
        workspace_dir=str(tmp_path),
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        executable=sys.executable,
        model="opencode/beam-free",
    )
    adapter._extra_args_prefix = [FAKE_OPENCODE]
    return adapter


def test_names_a_cli_shares_with_the_gates_records():
    # The rule set, the audit log and the UI labels all speak the Claude CLI's
    # names, so the same action is recorded under the same name.
    assert cli_tool_name("shell") == "Bash"
    assert cli_tool_name("bash") == "Bash"
    assert cli_tool_name("read") == "Read"
    assert cli_tool_name("edit") == "Edit"
    # And a tool with no counterpart is not renamed into one.
    assert cli_tool_name("semSearch") == "SemSearch"
    assert cli_tool_name("") == "Tool"


def test_a_record_never_claims_ana_allowed_the_call():
    record = cli_tool_record("cursor", "shell", {"command": "rm -rf /"}, "call-1")

    assert record["verdict"] == "allow"
    assert record["decided_by"] == "cli:cursor"
    assert record["rule_id"] is None
    assert record["category"] is None
    assert record["agent_id"] is None


def test_a_non_dict_argument_still_records_the_call():
    record = cli_tool_record("opencode", "bash", "echo hi", "call-1")

    assert record["input"] == {"value": "echo hi"}


@pytest.mark.asyncio
async def test_a_cursor_lead_turn_sends_its_calls_and_saves_them(tmp_path):
    frames: list[dict] = []
    timeline = TurnTimeline()

    await _stream_turn(
        _cursor(tmp_path), outcome=_TurnOutcome(), prompt="ping", agent="cursor",
        session_id=None, writer=frames.append, base_cost_usd=0, base_tokens=0,
        rate_limit_utilization=1, timeline=timeline,
    )

    tools = [frame["tool"] for frame in frames if frame["type"] == "tool"]
    assert [tool["tool_name"] for tool in tools] == ["Bash", "Bash", "Edit"]
    assert all(frame["agent"] == "cursor" for frame in frames if frame["type"] == "tool")

    # Saved in order next to what the agent said, and the repeated id is one
    # block, not two commands.
    assert [block["kind"] for block in timeline.blocks] == ["text", "tool", "tool"]
    assert timeline.blocks[0]["text"] == "pong"
    assert [block["tool"]["tool_use_id"] for block in timeline.blocks[1:]] == [
        "call-abc-0", "call-abc-1",
    ]


@pytest.mark.asyncio
async def test_an_opencode_lead_turn_saves_its_calls(tmp_path):
    frames: list[dict] = []
    timeline = TurnTimeline()

    await _stream_turn(
        _opencode(tmp_path), outcome=_TurnOutcome(), prompt="ping", agent="opencode",
        session_id=None, writer=frames.append, base_cost_usd=0, base_tokens=0,
        rate_limit_utilization=1, timeline=timeline,
    )

    assert [frame["tool"]["tool_name"] for frame in frames if frame["type"] == "tool"] == [
        "Read", "Bash",
    ]
    # The calls happen before the answer, and the transcript keeps that order.
    assert [block["kind"] for block in timeline.blocks] == ["tool", "tool", "text"]


@pytest.mark.asyncio
async def test_a_worker_on_a_hookless_cli_fills_its_own_card(tmp_path):
    """A planned sub-agent's panel reads like a transcript; before this a
    Cursor worker's panel had narration and no calls at all."""
    frames: list[dict] = []
    card: dict = {}

    class _Task:
        id = "task-1"

    await _run_worker(
        _cursor(tmp_path),
        "ping",
        on_tool=lambda tool: _worker_tool(frames.append, card, _Task, "cursor", tool),
    )

    assert [tool["tool_name"] for tool in card["tools"]] == ["Bash", "Edit"]
    # The id the browser routes by: a worker's calls belong on its card, not in
    # the lead's reply.
    assert all(tool["agent_id"] == "task-1" for tool in card["tools"])
    assert [block["kind"] for block in card["blocks"]] == ["tool", "tool"]
    assert all(frame["tool"]["agent_id"] == "task-1" for frame in frames)


def test_a_repeated_call_updates_the_card_instead_of_doubling_it():
    frames: list[dict] = []
    card: dict = {}

    class _Task:
        id = "task-1"

    started = cli_tool_record("cursor", "shell", {"command": "echo hi"}, "call-1")
    finished = cli_tool_record("cursor", "shell", {"command": "echo hi", "done": True}, "call-1")
    _worker_tool(frames.append, card, _Task, "cursor", started)
    _worker_tool(frames.append, card, _Task, "cursor", finished)

    assert len(card["tools"]) == 1
    assert card["tools"][0]["input"] == {"command": "echo hi", "done": True}
    assert len(card["blocks"]) == 1
    # Both frames are sent: the tab updates the entry it already drew.
    assert len(frames) == 2
