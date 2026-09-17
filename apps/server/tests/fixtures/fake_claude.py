#!/usr/bin/env python3
"""Fake `claude` CLI for tests: ignores its arguments and prints a canned
stream-json turn, matching the real CLI's observed output shape.

If the FAKE_CLAUDE_BIG_STDERR env var is set, writes well over one OS pipe
buffer's worth (~64KiB on Linux) of stderr output in between stdout events,
to exercise the case where a real `claude` CLI's noisy hooks fill the stderr
pipe while stdout is still open — a regression test for the stdout/stderr
deadlock in ClaudeCodeAdapter.run_stream()."""
import json
import os
import sys

SESSION_ID = "test-session-id"

EVENTS = [
    {"type": "system", "subtype": "init", "session_id": SESSION_ID},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "pong"}]},
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
            # backpressure threshold (2x its 64KiB default limit) so a
            # reader that only drains stderr after stdout EOF reliably
            # deadlocks instead of racing asyncio's own background pipe
            # draining.
            sys.stderr.write("x" * 5_000_000)
            sys.stderr.flush()


if __name__ == "__main__":
    main()
