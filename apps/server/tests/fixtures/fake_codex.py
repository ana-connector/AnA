#!/usr/bin/env python3
"""Fake `codex` CLI for tests: prints a canned `codex exec --json` JSONL
turn, matching the real CLI's observed output shape (see
docs/plans/2026-09-22-dual-cli-orchestration.md).

Records the argv it was invoked with to the file named by FAKE_CODEX_ARGV_LOG
(if set), so tests can assert on argv construction — specifically that a
resumed turn uses `exec resume <session id>` rather than a fresh `exec`.

If FAKE_CODEX_FAIL is set, exits non-zero after writing to stderr, to
exercise the adapter's non-zero-exit error path."""
import json
import os
import sys

THREAD_ID = "test-codex-thread-id"


def main() -> None:
    argv_log = os.environ.get("FAKE_CODEX_ARGV_LOG")
    if argv_log:
        with open(argv_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")

    # Only the *resume* attempt fails, exactly as the real CLI behaves when the
    # rollout file is gone; the retry without `resume` then succeeds.
    if os.environ.get("FAKE_CODEX_MISSING_ROLLOUT") and "resume" in sys.argv:
        print(json.dumps({"type": "thread.started", "thread_id": THREAD_ID}))
        print(
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "thread/resume: thread/resume failed: no rollout found "
                            "for thread id 01a0cbea (code -32600)"
                        )
                    },
                }
            )
        )
        sys.stdout.flush()
        return

    if os.environ.get("FAKE_CODEX_FAIL"):
        sys.stderr.write("codex exploded")
        sys.stderr.flush()
        sys.exit(1)

    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        # A reasoning item: progress detail the adapter must not surface as
        # assistant text.
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "hmm"}},
        *([
            {"type": "item.updated", "item": {"id": "item_1", "type": "agent_message", "text": "po"}},
            {"type": "item.updated", "item": {"id": "item_1", "type": "agent_message", "text": "pong"}},
        ] if os.environ.get("FAKE_CODEX_PARTIAL") else []),
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "pong"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 14279,
                "cached_input_tokens": 12160,
                "output_tokens": 6,
            },
        },
    ]
    for event in events:
        print(json.dumps(event))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
