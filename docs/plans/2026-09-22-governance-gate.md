# ANA — Governance Gate (roadmap stage 3, slice 1)

Date: 2026-09-22
Status: implementing

## Goal

Stop running both CLIs with approval bypassed. Every tool call they make goes
through an ANA-owned gate that:

1. writes an **append-only audit record** before anything else,
2. classifies the call against a **YAML rule set**, and
3. asks the user for **y/n in the web UI** when the rule says the call is
   risky — denying automatically when nobody answers.

This is the roadmap's "HITL 승인 게이트 + Command Interceptor + 감사 로그"
trio, which only means anything as one pipeline.

## Verified CLI facts (this machine, 2026-09-22)

Both CLIs expose a `PreToolUse` hook with the **same payload and the same
response contract**, so one hook script serves both.

Payload on stdin:

```json
{"session_id": "...", "cwd": "...", "hook_event_name": "PreToolUse",
 "tool_name": "Bash", "tool_input": {"command": "echo hi"},
 "tool_use_id": "..."}
```

Response on stdout:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
 "permissionDecision": "allow" | "deny",
 "permissionDecisionReason": "..."}}
```

- **claude**: hooks come from `--settings <file>`; a denial reaches the model
  as an errored tool result carrying our reason. With `--permission-prompts
  none` anything the hook does not explicitly allow is denied instead of
  hanging on a prompt nobody can answer. `--dangerously-skip-permissions` is
  dropped.
- **codex**: hooks come from `$CODEX_HOME/hooks.json`; a denial surfaces as
  "Command blocked by PreToolUse hook: <reason>". Hooks in automation need
  `--dangerously-bypass-hook-trust` (it bypasses *hook trust prompts*, not the
  sandbox). `--dangerously-bypass-approvals-and-sandbox` is dropped in favor
  of `-s workspace-write`, so the sandbox is defense in depth behind the gate.

## Design

### Gate flow

```
CLI tool call
  → PreToolUse hook script (packages/governance/hook.py)
  → POST $ANA_GATE_URL/gate/tool-call   (header: X-ANA-Gate-Token)
      1. token → thread_id + agent
      2. rules.evaluate(tool_name, tool_input) → allow | ask | deny
      3. audit row written BEFORE any prompt
      4. "ask" → approval_request frame on that thread's WebSocket,
                 await the user's answer (timeout → deny)
      5. audit row for the decision
      ← {"decision": "allow"|"deny", "reason": "..."}
  → hook prints the CLI's hookSpecificOutput JSON
```

**Fail-closed**: if the hook cannot reach ANA, cannot parse the response, or
times out, it prints `deny`. A broken gate must not silently become no gate.

### Rule set (`config/governance.yaml`)

```yaml
default_action: allow        # tools nothing matches
rules:
  - id: fs-recursive-delete
    category: filesystem-destruction
    action: deny
    tools: [Bash]
    patterns: ['\brm\s+(-\w+\s+)*-\w*[rR]\w*[fF]']
```

`action` is `deny` (never runs), `ask` (user decides) or `allow` (explicit
allowlist, wins over later rules). Categories follow the roadmap: filesystem
destruction, DB destruction, git history rewriting, system control, secret
exfiltration, log/test tampering, plus `ask` for network egress and for
writes outside the workspace.

Rules live in YAML precisely so adding one needs no code change.

### Audit log

Postgres table `audit_events`, append-only enforced **in the database** by a
BEFORE UPDATE/DELETE trigger that raises — not by convention in application
code. One row per gate decision: thread, agent, tool, input, matched rule,
verdict, who decided (rule / user / timeout), reason, timestamp.

### Session tokens

The hook runs inside the CLI subprocess and must prove which conversation it
belongs to. Each WebSocket connection registers a random token mapped to its
thread, and `ANA_GATE_URL` / `ANA_GATE_TOKEN` are injected into the CLI
subprocess environment (the env builders already strip `DATABASE_URL` the
same way). An unknown token is refused, so a stray process on the machine
cannot drive approvals.

### Web UI

An approval request renders as a card in the chat: tool, command, matched
rule and category, with Allow / Deny. The answer goes back as
`{"type": "approval", "request_id": "...", "decision": "allow"|"deny"}`.
Unanswered requests expire (default 300s) and are audited as denied.

## Slices 2-4 (same day, in the order the user asked for)

**Circuit breaker + failover** (`packages/orchestrator/breaker.py`). A turn
that fails on a provider usage limit is retried down a chain — Claude's other
auth mode, then Codex with a handoff brief — instead of surfacing as an
error. Crossing the thread's cost/token budget starts the next turn one rung
down. Recovery is the cooldown expiring: the thread remembers its rung and
`tripped_at`, so the next turn after the window starts from the preferred rung
again, with no scheduler and no probe traffic. A `failover` frame marks the
switch in the transcript. An *ordinary* failure (bad request, blocked command)
is never retried elsewhere — only usage limits are.

**Secrets + egress scanning** (`packages/governance/secrets.py`). Project- or
global-scoped credentials, encrypted at rest, injected into both CLIs as
environment variables, managed at `/secrets` in the web UI. The gate refuses
any tool call whose input contains a registered *value* — using
`$THE_VARIABLE` is the supported path — and masks the value in the audit row,
so the audit log never becomes the leak it recorded. Short values are not
scanned (they match everything); a row that cannot be decrypted under the
current `ANA_SECRET_KEY` is skipped rather than fatal, and still listed so it
can be re-entered.

**Session workspaces** (`packages/governance/workspace.py`). One directory per
conversation under `ANA_WORKSPACE_ROOT`, used as both CLIs' working directory.
The gate escalates any call whose paths leave the calling thread's directory
to an approval card. Read-only system paths are exempt on purpose: flagging
`ls /usr/bin` would train the user to approve without reading. The thread id
is sanitized and then containment-checked, since it arrives as a query
parameter.

## Still out of scope

**Per-session containers.** Every conversation shares the one server
container. Doing it properly means giving ANA the Docker socket (host-root
equivalent, which the current threat model does not grant), building a session
image with both CLIs, managing container lifecycle per thread, and routing the
gate hook out of each container back to ANA. That is a larger slice than
everything above combined, and the workspace + gate confinement covers the
single-user local case it was meant to protect.

## Surprises found while building this

- **The same CLI version is not the same binary.** The npm build of claude
  2.1.197 has no `--permission-prompts`, while the native install of 2.1.197
  does, and passing it to the former aborts the run. The flag is now probed
  once per process (`supports_permission_prompts`) instead of assumed.
- **codex cannot sandbox inside Docker.** Its `-s workspace-write` shells out
  to bubblewrap, which needs unprivileged user namespaces and fails in a
  container: "bwrap: No permissions to create a new namespace". The hook runs
  and still denies under `--dangerously-bypass-approvals-and-sandbox`
  (verified), so in a container the gate plus the container itself is the
  boundary, and `CODEX_SANDBOX` is left for hosts where bubblewrap works.
- **A missing rules file denied everything**, because `config/` was not in the
  server image. That is the fail-closed behavior working, but it reads like a
  broken agent, so the file ships in the image now.

## Verification

- `pytest`: rule evaluation, fail-closed hook behavior, audit append-only
  enforcement (UPDATE/DELETE must raise), gate endpoint allow/deny/ask paths,
  token rejection, approval timeout.
- Manual, through the running stack: a benign command runs; `rm -rf` is
  denied by rule and appears in the audit log; a network-egress command
  raises an approval card and honors both answers.
