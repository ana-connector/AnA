#!/usr/bin/env python3
"""PreToolUse hook, run by both CLIs as a subprocess of the agent process.

Reads the hook payload on stdin, asks ANA's gate what to do, and prints the
decision in the `hookSpecificOutput` shape both CLIs accept (verified
2026-09-22 against claude 2.1.197 and codex-cli 0.155.1 — same payload, same
response contract).

Fail-closed: any failure to reach the gate, parse its answer or finish in
time prints "deny". A gate that silently degrades into "no gate" would be
worse than not having one, because the rest of the system would still report
that every call was screened.

Depends only on the standard library: it runs inside the agent's own
environment, which is not ANA's virtualenv.
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 600.0


def _decision(decision: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def ask_gate(payload: dict) -> dict:
    gate_url = os.environ.get("ANA_GATE_URL")
    token = os.environ.get("ANA_GATE_TOKEN")
    if not gate_url or not token:
        return _decision("deny", "ANA gate is not configured for this session")

    timeout = float(os.environ.get("ANA_GATE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    body = json.dumps(
        {
            "tool_name": payload.get("tool_name", ""),
            "tool_input": payload.get("tool_input", {}),
            "cli_session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "tool_use_id": payload.get("tool_use_id"),
            "model": payload.get("model"),
            # "lead" or "worker": the lead plans and answers, sub-agents do the
            # work, and the gate is where that split is enforced.
            "role": "worker" if (payload.get("agent_id") or os.environ.get("ANA_GATE_AGENT_ID"))
                    else os.environ.get("ANA_GATE_ROLE", "worker"),
            "agent_id": payload.get("agent_id") or os.environ.get("ANA_GATE_AGENT_ID"),
            "agent_type": payload.get("agent_type"),
            # The permission scope this sub-agent's lead gave it, carried in
            # its environment (packages/governance/subagent_scope.py).
            "scope_mode": os.environ.get("ANA_GATE_SCOPE_MODE"),
            "scope_paths": os.environ.get("ANA_GATE_SCOPE_PATHS"),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        gate_url.rstrip("/") + "/gate/tool-call",
        data=body,
        headers={"content-type": "application/json", "x-ana-gate-token": token},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _decision("deny", f"ANA gate refused the request ({exc.code})")
    except Exception as exc:  # noqa: BLE001 - every failure denies, by design
        return _decision("deny", f"ANA gate unreachable: {exc}")

    decision = answer.get("decision")
    if decision not in ("allow", "deny"):
        return _decision("deny", "ANA gate returned an unusable decision")
    return _decision(decision, answer.get("reason", ""))


def report_subagent(payload: dict) -> None:
    """Lifecycle hooks are observational; a telemetry failure cannot stop work."""
    gate_url = os.environ.get("ANA_GATE_URL")
    token = os.environ.get("ANA_GATE_TOKEN")
    if not gate_url or not token or not payload.get("agent_id"):
        return
    body = json.dumps({
        "event": payload.get("hook_event_name"),
        "agent_id": payload["agent_id"],
        "agent_type": payload.get("agent_type"),
        "output": payload.get("last_assistant_message") or "",
    }).encode("utf-8")
    request = urllib.request.Request(
        gate_url.rstrip("/") + "/gate/subagent",
        data=body,
        headers={"content-type": "application/json", "x-ana-gate-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
    except Exception:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - unreadable payload is not a free pass
        print(json.dumps(_decision("deny", "ANA gate could not read the hook payload")))
        sys.exit(2)
    if payload.get("hook_event_name") in ("SubagentStart", "SubagentStop"):
        report_subagent(payload)
        print("{}")
        return
    decision = ask_gate(payload)
    print(json.dumps(decision))
    if decision["hookSpecificOutput"]["permissionDecision"] == "deny":
        # Belt and braces for Kimi, whose hook contract reads the *exit code*
        # first: 2 blocks, and "any other non-zero" is treated as a script
        # error and allowed (fail-open). Claude and Codex read the JSON and
        # also treat exit 2 as a block, so one exit path serves all three —
        # and a denial can never be downgraded to an allow by the runner.
        sys.exit(2)


if __name__ == "__main__":
    main()
