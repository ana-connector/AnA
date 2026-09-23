"""The PreToolUse hook script — the piece that runs inside the CLI process.

Its contract with both CLIs (verified 2026-09-22 against claude 2.1.197 and
codex-cli 0.155.1): read the payload on stdin, print hookSpecificOutput with
a permissionDecision. Every failure path must print "deny"."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = str(Path(__file__).resolve().parents[3] / "packages" / "governance" / "hook.py")
PAYLOAD = json.dumps(
    {
        "session_id": "cli-session",
        "cwd": "/home/hello/workspace",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "tool_use_id": "toolu_1",
    }
)


def _run(payload: str, env: dict) -> dict:
    result = subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True, env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]


def test_denies_when_gate_is_not_configured():
    output = _run(PAYLOAD, {"PATH": "/usr/bin:/bin"})

    assert output["permissionDecision"] == "deny"
    assert "not configured" in output["permissionDecisionReason"]


def test_denies_when_gate_is_unreachable():
    # Fail-closed: a gate that is down must not become "no gate".
    env = {
        "PATH": "/usr/bin:/bin",
        "ANA_GATE_URL": "http://127.0.0.1:9",  # discard port, refuses instantly
        "ANA_GATE_TOKEN": "t",
        "ANA_GATE_TIMEOUT_SECONDS": "5",
    }
    output = _run(PAYLOAD, env)

    assert output["permissionDecision"] == "deny"
    assert "unreachable" in output["permissionDecisionReason"]


def test_denies_on_unreadable_payload():
    output = _run("this is not json", {"PATH": "/usr/bin:/bin"})

    assert output["permissionDecision"] == "deny"


@pytest.fixture
def fake_gate(tmp_path):
    """A stand-in gate server, so the hook is tested over real HTTP."""
    import http.server
    import threading

    state = {"decision": "allow", "reason": "ok", "requests": [], "status": 200}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
            state["requests"].append({"body": body, "token": self.headers.get("x-ana-gate-token")})
            self.send_response(state["status"])
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"decision": state["decision"], "reason": state["reason"]}).encode()
            )

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


def _env_for(gate) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "ANA_GATE_URL": gate["url"],
        "ANA_GATE_TOKEN": "session-token",
        "ANA_GATE_TIMEOUT_SECONDS": "10",
    }


def test_forwards_tool_call_and_token_to_the_gate(fake_gate):
    output = _run(PAYLOAD, _env_for(fake_gate))

    assert output["permissionDecision"] == "allow"
    sent = fake_gate["requests"][0]
    assert sent["token"] == "session-token"
    assert sent["body"]["tool_name"] == "Bash"
    assert sent["body"]["tool_input"]["command"] == "echo hi"
    assert sent["body"]["cli_session_id"] == "cli-session"


def test_subagent_tool_uses_worker_role(fake_gate):
    payload = json.loads(PAYLOAD)
    payload["agent_id"] = "child-1"
    payload["agent_type"] = "worker"
    env = {**_env_for(fake_gate), "ANA_GATE_ROLE": "lead"}
    output = _run(json.dumps(payload), env)
    assert output["permissionDecision"] == "allow"
    body = fake_gate["requests"][0]["body"]
    assert body["role"] == "worker"
    assert body["agent_id"] == "child-1"


def test_relays_a_denial_with_its_reason(fake_gate):
    fake_gate["decision"] = "deny"
    fake_gate["reason"] = "blocked by ANA governance rule fs-recursive-force-delete"

    output = _run(PAYLOAD, _env_for(fake_gate))

    assert output["permissionDecision"] == "deny"
    assert "fs-recursive-force-delete" in output["permissionDecisionReason"]


def test_denies_on_unusable_gate_answer(fake_gate):
    fake_gate["decision"] = "maybe"

    output = _run(PAYLOAD, _env_for(fake_gate))

    assert output["permissionDecision"] == "deny"


def test_denies_when_gate_rejects_the_token(fake_gate):
    fake_gate["status"] = 403

    output = _run(PAYLOAD, _env_for(fake_gate))

    assert output["permissionDecision"] == "deny"
    assert "403" in output["permissionDecisionReason"]
