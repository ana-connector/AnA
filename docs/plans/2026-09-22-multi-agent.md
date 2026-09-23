# ANA — Multi-Agent Orchestration (roadmap stage 5, remaining)

Date: 2026-09-22
Status: implementing

## Already done

The Codex adapter and cross-CLI context handoff landed earlier today
(`2026-09-22-dual-cli-orchestration.md`). What is left is the Planner-Worker
structure on top of it.

## Goal

The lead agent, when a request is big enough to split, produces a **plan** of
sub-tasks and says which CLI should run each one. ANA spawns those workers as
separate CLI sessions, runs them with a concurrency cap, collects their
results, and hands them back to the lead to synthesize a single answer. The web
UI draws the fan-out as it happens.

## Design

### Who decides to delegate

The lead does, in one planning call: it answers with JSON — either
`{"mode": "direct"}` (just answer normally) or `{"mode": "delegate", "tasks":
[...]}` with a per-task `agent` of `claude` or `codex`. The roadmap leaves the
routing rule to the lead's judgment (roughly: code generation/refactoring to
Codex, explanation/review/planning to Claude), so it lives in the planning
prompt rather than in Python.

Planning costs an extra CLI call, so it is **opt-in per turn** — the composer
has a "Plan" toggle, and `ANA_PLANNER_DEFAULT=on` flips the default. A turn
without it behaves exactly as before.

### Workers

- Separate CLI sessions, never resumed: a worker gets its task prompt plus the
  thread's recent context, not the lead's session.
- Same workspace and the same gate token as the thread, so every worker tool
  call is screened by the governance gate and confined to the session
  workspace (roadmap: sub-agents share the lead's workspace and run inside the
  scope it was given).
- `ANA_MAX_SUBAGENTS` bounds concurrency, default 5 per the roadmap; extra
  tasks queue behind the semaphore rather than being dropped.
- `ANA_SUBAGENT_TIMEOUT_SECONDS` bounds each worker. A timeout or an error is
  a *task* result, not a turn failure: the synthesis step is told which tasks
  failed, and the user still gets an answer.

### Supervision

`packages/orchestrator/supervisor.py` owns the fan-out: semaphore, per-task
timeout, error isolation, and cancellation (a cancelled turn kills every
worker subprocess it started). It reports each task's state through a callback
so the graph can stream `subagent` frames.

### Frames and visualization

New WebSocket frames: `plan` (the task list), `subagent` (one per state
change: running / done / error / timeout). The UI renders them with React Flow
as a lead node fanning out to worker nodes that change color as they finish —
and the same data drives a plain list, so the graph is a view, not the source
of truth.

## Out of scope

Letting the lead spawn workers *mid-answer* through an MCP tool it calls at
will. Both CLIs support MCP servers, so it is the natural next step, but it
needs an ANA-side MCP server and a second approval path; the plan-then-fan-out
shape gives the same Planner-Worker behavior with one deterministic place to
enforce the cap.

## Result (verified live)

A real two-task delegation through the running stack:

```
PLAN: delegate [('피보나치 한 줄 코드', 'codex'), ('재귀 vs 반복 차이', 'claude')]
  SUB t1 codex   running
  SUB t2 claude  running
  SUB t2 claude  done     tok=2762
  SUB t1 codex   done     tok=14270
RESULT  → one synthesized answer containing both results
```

The lead routed the implementation task to Codex and the explanation task to
Claude on its own, both ran in parallel, and the synthesis named the outcome of
each. `ana_subagents_total{agent,status}` counted both.

One bug this caught: the `plan_mode` flag survived parsing and the queue but
was missing from the `astream` payload, so planning silently never ran — the
turn just answered normally. There is now a test that follows the flag all the
way into the node, because that failure is invisible from the outside.

## Verification

- `pytest`: plan parsing (fenced JSON, junk around it, unknown agents, task
  cap), supervisor (cap enforced, one failing task does not sink the others,
  timeout, cancellation kills workers), the delegate path end-to-end with fake
  adapters, and the frames the UI needs.
- Live: a real two-task delegation through the stack, with the workers visible
  in the audit log under the same thread.
