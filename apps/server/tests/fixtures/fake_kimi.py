#!/usr/bin/env python3
"""Stands in for the `kimi` CLI in tests.

Emits the stream-json shape kimi 2.0.2 actually produces (see
packages/adapters/kimi.py) — version meta, assistant messages, a tool call
and its result, then the resume hint that carries the session id.

Controlled by environment variables:
  FAKE_KIMI_TEXT        the assistant reply (default "안녕")
  FAKE_KIMI_SESSION     session id reported in the resume hint
  FAKE_KIMI_EXIT        process exit code (default 0)
  FAKE_KIMI_STDERR      text written to stderr
  FAKE_KIMI_ARGS_FILE   file to dump argv into, for argv assertions
  FAKE_KIMI_SILENT      emit no assistant message (a refused request)
  FAKE_KIMI_ORPHAN      seconds a leaked child keeps stdout/stderr open after
                        this process exits, as kimi's unreaped MCP servers do
"""
import json
import os
import subprocess
import sys


def main() -> None:
    args_file = os.environ.get("FAKE_KIMI_ARGS_FILE")
    if args_file:
        with open(args_file, "w", encoding="utf-8") as handle:
            json.dump(sys.argv[1:], handle)

    print(json.dumps({"role": "meta", "type": "system.version", "version": "2.0.2"}))
    if os.environ.get("FAKE_KIMI_TOOL_CALL"):
        print(json.dumps({
            "role": "assistant",
            "tool_calls": [{
                "type": "function",
                "id": "tool_1",
                "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"},
            }],
        }))
        print(json.dumps({"role": "tool", "tool_call_id": "tool_1", "content": "a.txt\n"}))
    if os.environ.get("FAKE_KIMI_SWARM"):
        print(json.dumps({
            "role": "assistant",
            "tool_calls": [{"type": "function", "id": "swarm_1",
                            "function": {"name": "AgentSwarm", "arguments": json.dumps({
                                "items": ["첫 번째 작업", "두 번째 작업"]})}}],
        }))
        print(json.dumps({"role": "tool", "tool_call_id": "swarm_1", "content": "완료"}))
    orphan = os.environ.get("FAKE_KIMI_ORPHAN")
    if orphan:
        # Inherits this process's stdout and stderr and outlives it, which is
        # what keeps those pipes from reaching EOF when the CLI is gone.
        subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({float(orphan)})"],
            start_new_session=True,
        )
    if not os.environ.get("FAKE_KIMI_SILENT"):
        print(json.dumps({
            "role": "assistant",
            "content": os.environ.get("FAKE_KIMI_TEXT", "안녕"),
        }))
    session_id = os.environ.get("FAKE_KIMI_SESSION", "session_fake")
    print(json.dumps({
        "role": "meta",
        "type": "session.resume_hint",
        "session_id": session_id,
        "command": f"kimi -r {session_id}",
        "content": f"To resume this session: kimi -r {session_id}",
    }))

    stderr = os.environ.get("FAKE_KIMI_STDERR")
    if stderr:
        print(stderr, file=sys.stderr)
    sys.exit(int(os.environ.get("FAKE_KIMI_EXIT", "0")))


if __name__ == "__main__":
    main()
