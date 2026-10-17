#!/usr/bin/env python3
"""Fake Cursor Agent (`agent`) CLI for tests: ignores argv and prints a canned
stream-json turn matching the real CLI's observed output shape."""
import json
import os
import sys

SESSION_ID = os.environ.get("FAKE_CURSOR_SESSION", "cursor-test-session")
TEXT = os.environ.get("FAKE_CURSOR_TEXT", "pong")


def main() -> None:
    events = [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "login",
            "session_id": SESSION_ID,
            "model": "Auto",
            "permissionMode": "default",
        },
        {
            "type": "thinking",
            "subtype": "delta",
            "text": "thinking…",
            "session_id": SESSION_ID,
        },
        {
            "type": "thinking",
            "subtype": "completed",
            "session_id": SESSION_ID,
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": TEXT}]},
            "session_id": SESSION_ID,
        },
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 10,
            "is_error": False,
            "result": TEXT,
            "session_id": SESSION_ID,
            "usage": {
                "inputTokens": 100,
                "outputTokens": 5,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
            },
        },
    ]
    if os.environ.get("FAKE_CURSOR_ERROR"):
        events = [
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "boom",
                "session_id": SESSION_ID,
            }
        ]
        # Non-zero exit still yields the result first, then adapter may add
        # an error from stderr — keep exit 0 so only the result is tested
        # unless FAKE_CURSOR_EXIT is set.
    for event in events:
        print(json.dumps(event))
        sys.stdout.flush()
    if os.environ.get("FAKE_CURSOR_EXIT"):
        sys.exit(int(os.environ["FAKE_CURSOR_EXIT"]))


if __name__ == "__main__":
    main()
