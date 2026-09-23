# ANA — Dual-CLI Orchestration (Claude Code + Codex)

Date: 2026-09-22
Status: implementing

## Goal

Let one ANA chat thread be driven by **either** the `claude` CLI or the `codex`
CLI, switchable per message from the web UI, with conversation context carried
across the switch so the incoming agent continues the work instead of starting
cold.

Scope is deliberately minimal: this builds on the existing core loop
(FastAPI + LangGraph + Postgres/pgvector + Next.js/shadcn) rather than
introducing new infrastructure.

## Non-goals

- Multi-agent parallelism (two CLIs running the same turn).
- Automatic agent selection / routing heuristics. The user picks.
- Cost/usage normalization between the two providers. Codex's JSONL reports
  token counts, not a dollar figure; we record what each CLI gives us.

## CLI facts (verified on this machine, 2026-09-22)

`codex exec --json` emits JSONL on stdout:

```
{"type":"thread.started","thread_id":"01a0c8b1-..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}
{"type":"turn.completed","usage":{"input_tokens":14279,...,"output_tokens":6,...}}
```

- Session continuity: `codex exec resume <SESSION_ID> <PROMPT>` (session id is
  the `thread_id` from `thread.started`).
- `codex exec` reads the prompt from stdin when stdin is a pipe, so the
  subprocess must be started with stdin closed (`DEVNULL`) or it blocks.
- Working directory: `-C <DIR>`; `--skip-git-repo-check` for non-repo
  workspaces.
- Approvals: `--dangerously-bypass-approvals-and-sandbox`, matching the
  `--dangerously-skip-permissions` the Claude adapter already passes. Both are
  acceptable only because ANA's WebSocket endpoint is origin-checked and
  localhost-bound; this is recorded as the existing trust boundary, not a new
  one.
- No cost figure in the JSONL, so `AdapterEvent.cost_usd` stays `None` for
  Codex turns and only token counts land in the `usage` event's raw payload.

## Design

### 1. `CodexAdapter` (`packages/adapters/codex.py`)

Implements the existing `AgentAdapter` interface (one subprocess per turn,
`--resume`-style continuity) so it is a drop-in alongside `ClaudeCodeAdapter`:

| AdapterEvent | Codex JSONL source |
|---|---|
| `text` | `item.completed` where `item.type == "agent_message"` |
| `result` | `turn.completed` (carries the final text and session id) |
| `usage` | `turn.completed`'s `usage` block (raw) |
| `error` | non-zero exit (stderr), or `turn.failed` / `error` events |

`thread.started` sets the session id for the turn, mirroring how the Claude
adapter takes it from the `result` event.

### 2. Per-agent session ids in graph state

`GraphState` gains:

- `agent`: `"claude" | "codex"` — which CLI runs this turn.
- `session_ids`: `dict[str, str]` — one CLI session id per agent, so each CLI
  resumes *its own* conversation.
- `last_agent`: which CLI ran the previous turn in this thread.

All three are checkpointed per `thread_id` by the existing
`AsyncPostgresSaver`, so a page reload keeps both sessions alive.

### 3. Context handoff

When `agent != last_agent`, the prompt sent to the incoming CLI is prefixed
with a handoff brief built from the thread's own recent turns (Postgres
`turns` table, thread-scoped, newest N, each field truncated):

```
[ANA 핸드오프] 직전까지 <other agent> 에이전트가 이 작업을 진행했다.
이어서 진행해라.

최근 대화:
1. (claude) Q: ... A: ...
2. (codex)  Q: ... A: ...

현재 요청: <user prompt>
```

The incoming CLI *also* resumes its own past session if it has one, so a
switch back to an agent that already worked in this thread gives it both its
native history and what happened while it was away.

Same-agent turns get no handoff block — only the existing RAG augmentation —
so the common case costs nothing extra.

`turns` gains an `agent` column so the handoff brief can attribute each past
turn, and so the UI can label replies.

### 4. WebSocket protocol

Client messages become JSON: `{"prompt": "...", "agent": "claude"|"codex"}`.
A bare text frame is still accepted and treated as `{"prompt": <frame>,
"agent": "claude"}` for backward compatibility with existing clients/tests.

Server events gain an `agent` field on non-`text` frames, so the UI can label
which CLI produced a reply.

### 5. Web UI (shadcn)

- Agent toggle (`ToggleGroup`-style segmented control) above the composer,
  persisted in `localStorage` next to `ana_thread_id`.
- Each agent message is labeled with a `Badge` naming the CLI that produced
  it, and a handoff turn shows a divider marking the switch.
- Components added under `src/components/ui/` in the repo's existing
  `base-nova` / `@base-ui/react` shadcn style.

### 6. Packaging

- `apps/server/Dockerfile` installs `@openai/codex` alongside
  `@anthropic-ai/claude-code`.
- `docker-compose.yml` mounts `${HOME}/.codex` read-write (the CLI persists
  session files there, which `codex exec resume` reads) and `${HOME}/.claude`
  as before.
- `docker compose up` remains the single command to run everything.

## Follow-up slice (same day)

Four gaps closed right after the dual-CLI work landed:

1. **Reload-safe chat screen** — `GET /threads/{thread_id}/turns` (backed by
   `thread_history()`) plus a history fetch on mount. Closes the gap the
   roadmap recorded as "브라우저 쪽 채팅 화면은 새로고침하면 사라진다".
   Needed a per-request CORS layer, since the UI and API are different
   origins; it reads the same `allowed_origins()` the WS handshake uses
   rather than freezing a list at startup.
2. **Cancelling an in-flight turn** — `{"type": "cancel"}` frame. Each turn
   now runs as its own task, so cancelling one leaves the connection and the
   queue behind it intact, and the CLI subprocess is killed explicitly
   (unwinding the async generator does not reap an OS process). The partial
   turn is still recorded, which the graph node's `finally` already handled.
3. **Cost / token reporting** — `AdapterEvent.usage` carries each CLI's own
   token payload; the graph accumulates `total_tokens` next to
   `total_cost_usd` and reports both on the `result` frame. Cache-hit counts
   are excluded from the sum because both CLIs report them as a breakdown of
   `input_tokens`.
4. **Compose image build verified** — `docker compose build` / `up`, with
   `which claude codex` inside the server image.

## Verification

- `pytest` — new: Codex adapter event parsing / session resume / error path
  (against a `fake_codex.py` fixture CLI, mirroring `fake_claude.py`), handoff
  brief construction, graph agent routing, WebSocket JSON protocol +
  backward-compatible text frames.
- Manual: `docker compose up`, send a message to Claude, switch to Codex, and
  confirm the Codex reply references the earlier Claude turn.
