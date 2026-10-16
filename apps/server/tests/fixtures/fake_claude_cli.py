#!/usr/bin/env python3
"""A stand-in `claude` for the end-to-end run.

Put on PATH as `claude`, it prints the stream-json shape ClaudeCodeAdapter
parses, so a whole turn can be driven through the real server and the real
browser without an account, a quota or a cost.

It answers with the live question echoed back. ANA prepends recalled
conversation to the prompt, so only the text after the last "현재 질문:" is
read — echoing the whole prompt made every answer contain every earlier one,
and a marker in the history made every later turn slow.

"SLOWMARK" in the live question parks the turn for a minute, which is how the
browser checks test stopping a turn that is still running.
"""
import json, os, sys, time

prompt = ""
args = sys.argv[1:]
for index, value in enumerate(args):
    if value == "-p" and index + 1 < len(args):
        prompt = args[index + 1]
session = "e2e-session"
for index, value in enumerate(args):
    if value in ("--session-id", "--resume") and index + 1 < len(args):
        session = args[index + 1]

def emit(event):
    print(json.dumps(event)); sys.stdout.flush()

emit({"type": "system", "subtype": "init", "session_id": session})
# Only the live question, never the recalled conversation ANA prepends:
# a marker in the history made every later turn slow too.
current = prompt.rsplit("현재 질문:", 1)[-1]
slow = "SLOWMARK" in current
if slow:
    for _ in range(600):
        time.sleep(0.1)
# Only the live question: ANA prepends recalled conversation, and
# echoing the whole thing makes every answer contain every earlier one.
answer = f"받은 말: {current.strip()[:60]}"
for chunk in (answer[:6], answer[6:]):
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": chunk}}})
    time.sleep(0.05)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]},
      "session_id": session})
emit({"type": "result", "result": answer, "session_id": session,
      "total_cost_usd": 0.0, "is_error": False})
