#!/usr/bin/env python3
"""Fake `claude` CLI for tests: ignores its arguments and prints a canned
stream-json turn, matching the real CLI's observed output shape.

If the FAKE_CLAUDE_BIG_STDERR env var is set, writes well over one OS pipe
buffer's worth (~64KiB on Linux) of stderr output in between stdout events,
to exercise the case where a real `claude` CLI's noisy hooks fill the stderr
pipe while stdout is still open — a regression test for the stdout/stderr
deadlock in ClaudeCodeAdapter.run_stream().

If the FAKE_CLAUDE_BIG_STDOUT_LINE env var is set, the "assistant" event's
text field is inflated to well over 64KiB (asyncio's default StreamReader
line-length limit) on a single stdout line, matching how this machine's
global Claude Code hooks inject large hook_response payloads into every real
stream-json line — a regression test for the "Separator is not found, and
chunk exceed the limit" crash in ClaudeCodeAdapter.run_stream()."""
import json
import os
import sys

SESSION_ID = "test-session-id"


def _build_events() -> list[dict]:
    text = "pong"
    if os.environ.get("FAKE_CLAUDE_BIG_STDOUT_LINE"):
        text = "p" * 200_000 + "ong"  # well over the default 64KiB limit
    partial = (
        [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}}
            for chunk in ("po", "ng")
        ]
        if os.environ.get("FAKE_CLAUDE_PARTIAL") else []
    )
    return [
        {"type": "system", "subtype": "init", "session_id": SESSION_ID},
        *partial,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            "session_id": SESSION_ID,
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.11}}},
            "session_id": SESSION_ID,
        },
        {
            "type": "result",
            "result": "pong",
            "session_id": SESSION_ID,
            "total_cost_usd": 0.07,
            "is_error": False,
        },
    ]


EVENTS = _build_events()


def main() -> None:
    big_stderr = os.environ.get("FAKE_CLAUDE_BIG_STDERR")
    for event in EVENTS:
        print(json.dumps(event))
        sys.stdout.flush()
        if big_stderr and event["type"] == "assistant":
            # Write after the first real stdout event, before the rest, so a
            # reader that isn't draining stderr concurrently with stdout
            # would leave the child blocked here — never reaching the
            # remaining stdout events. Must comfortably exceed the OS pipe
            # buffer (~64KiB) *and* asyncio's internal StreamReader
            # backpressure threshold (2x ClaudeCodeAdapter's stream `limit`,
            # currently 10MiB -> 20MiB) so a reader that only drains stderr
            # after stdout EOF reliably deadlocks instead of racing
            # asyncio's own background pipe draining. 25MB clears 20MiB.
            sys.stderr.write("x" * 25_000_000)
            sys.stderr.flush()


if __name__ == "__main__":
    main()
