#!/usr/bin/env python3
"""Fake `claude` CLI for tests: ignores its arguments and prints a canned
stream-json turn, matching the real CLI's observed output shape."""
import json
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
    for event in EVENTS:
        print(json.dumps(event))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
