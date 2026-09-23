# ANA — plan vs implementation audit

Date: 2026-09-23
Status: findings fixed except where listed under "Left open"

A pass over the roadmap and the six stage plans against the code, looking for
three things: work the plans promised and the code does not do, work that is
half-done, and places where the code does something the plan's own reasoning
says it should not.

## Fixed

### 1. Sub-agent permission scopes did not exist (roadmap stage 3 and 5)

The roadmap asks for a mechanism by which "대장 Agent가 sub-agent에 줄 권한(파일
접근 범위, 실행 가능 명령어 등)을 설정할 수 있다", and stage 5's STRIDE table
claims Elevation of Privilege is covered because "sub-agent는 대장이 부여한
권한 스코프를 못 벗어난다". Nothing in the code implemented a scope: every
sub-agent got exactly what any worker got.

Now `packages/governance/subagent_scope.py`. A scope is a mode (`write`, or
`read` which refuses the tools that change something — including
`mcp__git__git_commit`, since a read-only worker that can still commit is not
read-only) plus optional workspace-relative paths. The lead sets it per task in
its plan JSON, it rides to the CLI as environment variables, the hook returns
it with every call, and the gate enforces it *before* the rule set — a scope
narrows what the rules allow and can never widen it.

Two details worth keeping:

- The path check resolves **relative** paths, unlike the workspace-escape
  check, which skips them because they land in the working directory anyway. A
  worker scoped to `docs/` writes `src/app.py`, not `/abs/src/app.py`, so
  skipping relative paths would have been the whole hole.
- A malformed scope from the model parses to *more* restriction, never less: a
  `paths` entry containing `..` is dropped rather than widening the scope.

The scoped sub-agent is also told what it may do, so it reports "this needs
changing" instead of discovering the limit one denial at a time, and the task
card in the UI shows the scope it was given.

### 2. The lead's process withheld skills from its own sub-agents (stage 6)

The MCP fix earlier today established that the CLI's native sub-agents run
inside the lead's process. The skill catalog had the same bug in reverse: it
was worker-scoped, so the lead's prompt carried none, and a natively-spawned
sub-agent — which inherits that prompt — never saw a skill. The lead now
carries the worker catalog under a heading that says whose it is, and
`LEAD_INSTRUCTIONS` states explicitly that "no tools" applies to the lead
itself and must not be passed on to sub-agents.

### 3. The auth-mode switch promised in stage 1 was never built

The roadmap deferred the `use-claude-pro` ↔ `use-claude-api` toggle to the
stage 3 settings UI, noting that until then "`.env` 수정 + 서버 재시작" was the
only way. Stage 3 shipped without it. It is now a stored setting like the
routing rules (`GET`/`PUT /settings`, a select in the settings page); the file
wins over the environment so it survives a container restart. It applies to
conversations opened after the save — an open one keeps the adapters it was
built with.

### 4. Nothing in the UI showed MCP servers or skills (stage 6)

The endpoints existed and no page called them. The settings page now lists the
servers a sub-agent gets, each inactive one with its reason, and the skill
library including any file that failed validation — which is otherwise
invisible, since a skill that fails to load simply never reaches an agent.

### 5. Worker adapters accumulated for the life of a connection

`worker_factory` tracked every adapter it built so a disconnect could kill the
subprocesses, and never dropped them. A long conversation that plans on every
turn kept each one — with its copy of the environment, registered secrets
included — until the socket closed. Finished workers are now dropped as new
ones are built; adapters that have not started yet are left alone, since
dropping those would orphan a subprocess on disconnect.

### 6. MCP scope denials were logged with a null rule id

`decided_by` said `mcp-scope` but `rule_id` was whatever the YAML rule set had
matched, which for an MCP call is nothing. Both scope checks now record their
own rule id, so the audit log can be queried for them.

## Checked and found sound

- Audit log append-only: enforced by database triggers on UPDATE, DELETE
  **and** TRUNCATE, not by convention.
- The governance rule set covers every category the roadmap listed, including
  the Replit-style "은폐성 행동" (log/test-result tampering).
- Browser history after a refresh — the gap stage 1 recorded — is implemented
  (`/threads/{id}/turns`, restored by the chat hook).
- Sub-agent concurrency cap (5), per-task timeout, cancellation propagation.
- Failover recovery by cooldown rather than probe traffic.

## Left open (deliberately)

- **Egress scanning is input-only.** Stage 3 says the interceptor scans
  "요청/응답 내용". `PreToolUse` sees the request; catching a secret in a
  *response* needs a `PostToolUse` hook and a second gate endpoint. The
  registered-value check on the way out is the half that prevents exfiltration;
  the other half would catch a secret arriving from outside.
- **Native sub-agents are not per-task scoped.** They inherit their lead's
  environment, so they carry the lead's scope (none). Only ANA-spawned planner
  workers get a scope per task. Closing this needs the CLI to let a hook
  distinguish which sub-agent a call came from *and* to accept per-sub-agent
  environment, which it does not today; `agent_id` arrives at the gate, so a
  future scope-by-agent-id registry is the shape to build.
- **The planner does not name skills structurally.** The lead can name one in
  a task prompt, and the worker's catalog carries the instructions, but there
  is no `"skill": "git-commit"` field that ANA would validate against the
  library.
- **Codex's own `codex_apps` MCP server is denied**, as an unregistered
  server. That is the intended behavior of the scope check; registering it in
  `config/mcp.yaml` is how to allow it.

## Verification

`465 passed`, including the new `test_subagent_scope.py` (15) and
`test_gate_scope.py` (5).
