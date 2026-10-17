#!/usr/bin/env python3
"""Fake OpenCode CLI for tests: ignores argv and prints a canned
`run --format json` stream matching the real CLI's observed output shape."""
import json
import os
import sys

SESSION_ID = os.environ.get("FAKE_OPENCODE_SESSION", "ses_test")
TEXT = os.environ.get("FAKE_OPENCODE_TEXT", "pong")


def _event(event_type: str, **payload) -> dict:
    return {"type": event_type, "timestamp": 1, "sessionID": SESSION_ID, **payload}


def main() -> None:
    if os.environ.get("FAKE_OPENCODE_ERROR"):
        events = [
            _event(
                "error",
                error={"name": "ProviderAuthError", "data": {"message": "boom"}},
            )
        ]
        for event in events:
            sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
        # The real CLI exits 1 after a session error, having already printed it.
        raise SystemExit(1)

    events = [
        _event(
            "step_start",
            part={"id": "prt_1", "messageID": "msg_1", "type": "step-start"},
        ),
        _event(
            "reasoning",
            part={
                "id": "prt_2",
                "type": "reasoning",
                "text": "thinking…",
                "time": {"start": 1, "end": 2},
            },
        ),
        # A tool step: its own step_finish, which is why the adapter sums
        # tokens instead of reading one turn-level figure.
        _event(
            "tool_use",
            part={
                "id": "prt_3",
                "type": "tool",
                "tool": "read",
                "callID": "call_1",
                "state": {"status": "completed", "output": "hello"},
            },
        ),
        _event(
            "step_finish",
            part={
                "id": "prt_4",
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": {
                    "total": 1000,
                    "input": 100,
                    "output": 10,
                    "reasoning": 2,
                    "cache": {"write": 0, "read": 50},
                },
                "cost": 0.01,
            },
        ),
        _event(
            "step_start",
            part={"id": "prt_5", "messageID": "msg_2", "type": "step-start"},
        ),
        _event(
            "text",
            part={
                "id": "prt_6",
                "type": "text",
                "text": TEXT,
                "time": {"start": 3, "end": 4},
            },
        ),
        _event(
            "step_finish",
            part={
                "id": "prt_7",
                "type": "step-finish",
                "reason": "stop",
                "tokens": {
                    "total": 2000,
                    "input": 5,
                    "output": 7,
                    "reasoning": 0,
                    "cache": {"write": 1, "read": 3},
                },
                "cost": 0.02,
            },
        ),
    ]
    for event in events:
        sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
