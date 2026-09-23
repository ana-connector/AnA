# ANA

ANA is Not Agent.

ANA is an orchestrator around agent CLIs. It drives the **Claude Code CLI**
(`claude`) and the **Codex CLI** (`codex`), routes each message to whichever
one suits it, and carries the conversation context across the switch so the
incoming CLI continues the work instead of starting cold.

The unit you manage is a **workspace**: a folder, the one lead agent that works
in it, and that agent's ongoing conversation.

- **Stack**: Python (FastAPI + LangGraph + Postgres/pgvector) · TypeScript
  (Next.js + shadcn/ui) · Docker Compose
- **Design docs**: `docs/plans/`

## Running

```bash
cp .env.example .env     # then fill in what you need (see below)
docker compose up
```

- Web UI: http://localhost:3005
- API: http://localhost:8100 — `GET /healthz`, `GET/POST /workspaces`,
  `GET /threads/{id}/turns` (chat history for a reload), `GET /audit`
  (governance log), `GET /usage` (limit windows), `GET /secrets`,
  `POST /gate/tool-call` (the CLIs' hook calls this),
  `WS /ws/chat?workspace=<id>`

Ports are 3005 / 8100 / 15432 rather than the usual 3000 / 8000 / 5432,
because unrelated services already hold those on this machine. Inside the
Compose network the services still talk over 3000 / 8000 / 5432.

Both CLIs run inside the server container and authenticate from your host
config, which Compose mounts read-write: `~/.claude` and `~/.codex`. Log in on
the host first (`claude`, `codex login`) — the mounts are also where each CLI
persists the session files that make `--resume` / `codex exec resume` work.

### Local dev (no image build)

Postgres in Compose, the two servers on the host:

```bash
docker compose up -d postgres

DATABASE_URL="postgresql://postgres:ana_dev_password@127.0.0.1:15432/ana" \
CLAUDE_AUTH_MODE=pro \
ANA_ALLOWED_ORIGINS="http://localhost:3005,http://192.168.0.33:3005" \
  .venv/bin/python -m uvicorn apps.server.app.main:app --host 0.0.0.0 --port 8100

cd apps/web && NEXT_PUBLIC_WS_PORT=8100 npm run dev -- -p 3005 -H 0.0.0.0
```

`DATABASE_URL` must be overridden on the command line (or in `.env`) to point
at port 15432: the value shipped in `.env.example` still says 5432, which on
this machine is a different Postgres entirely and fails authentication.
`ANA_ALLOWED_ORIGINS` must list the origin the browser actually uses — a
missing entry closes the WebSocket with code 1008 and the UI just shows
"disconnected".

### Environment

| Variable | Meaning |
|---|---|
| `CLAUDE_AUTH_MODE` | `api` (uses `ANTHROPIC_API_KEY_SAVED`) or `pro` (subscription login in `~/.claude`) |
| `ANTHROPIC_API_KEY_SAVED` | API key, `api` mode only |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | Optional pins; blank = CLI default |
| `CODEX_MODEL` | Optional model pin for `codex`; blank = CLI default |
| `CODEX_SANDBOX` | `read-only` \| `workspace-write` \| `danger-full-access`; blank = `--dangerously-bypass-approvals-and-sandbox` |
| `ANA_WORKSPACE_DIR` | Directory both CLIs work in |
| `ANA_ALLOWED_ORIGINS` | Comma-separated origins allowed to open `/ws/chat` |
| `NEXT_PUBLIC_WS_URL` | Full WebSocket URL override; blank = derived from the page's own host |
| `NEXT_PUBLIC_WS_PORT` | Port used by that derivation (default `8100`) |
| `POSTGRES_PASSWORD`, `DATABASE_URL` | Memory/checkpoint database |

Both CLIs are run auto-approved by default (`--dangerously-skip-permissions`
for `claude`, `--dangerously-bypass-approvals-and-sandbox` for `codex`), so
they can act on `ANA_WORKSPACE_DIR` without prompting. The WebSocket endpoint
is origin-checked and Postgres is bound to localhost, which is the trust
boundary that makes this acceptable; do not expose port 8000 publicly. Set
`CODEX_SANDBOX=workspace-write` if you want the Codex side sandboxed.

## How the handoff works

Each CLI keeps its own native session, and ANA keeps one session id per agent
per thread (checkpointed in Postgres). When you switch agents mid-thread, the
incoming CLI gets:

1. its own past session resumed, if it has worked in this thread before, and
2. a handoff brief built from the thread's recent turns, attributed by agent —
   so it knows what the other CLI just did.

Same-agent turns skip the brief and use semantic recall (pgvector) over past
turns instead. See `packages/orchestrator/handoff.py` and
`docs/plans/2026-09-22-dual-cli-orchestration.md`.

## Observability

Self-hosted, so nothing leaves the machine:

| Service | URL | What it answers |
|---|---|---|
| Grafana | http://localhost:3200 (admin / `GRAFANA_PASSWORD`, default `admin`) | turns/min, turn latency, gate verdicts by rule, failovers, approval wait, cost and tokens |
| Prometheus | http://localhost:9090 | raw `ana_*` metrics, scraped from `server:8000/metrics` every 15s |
| Langfuse | http://localhost:3100 (`LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD`) | one trace per turn: prompt build, each CLI attempt, rung, tokens, cost |

The dashboard and datasource are provisioned from `ops/`, and Langfuse
self-provisions its org, project and API keys on first start (`LANGFUSE_INIT_*`
in `docker-compose.yml`) — otherwise a fresh `docker compose up` would need
someone to sign up in the UI before any trace could be sent.

Tracing is optional: with `LANGFUSE_HOST` or the keys unset, the tracer is a
no-op, and a Langfuse that is down or misconfigured can never fail a turn.

`GET /metrics` on the API serves the Prometheus exposition directly if you
want to scrape it from somewhere else.

## Governance

Neither CLI runs with blanket approval any more. Every tool call they make is
screened by ANA before it executes:

1. The CLI's `PreToolUse` hook posts the call to ANA (`POST /gate/tool-call`).
2. ANA writes an **append-only audit row**, then matches the call against
   `config/governance.yaml`.
3. `deny` never runs and the agent is told which rule blocked it; `ask` raises
   an approval card in the chat and waits; `allow` proceeds. Every outcome is
   audited.

The hook **fails closed**: if ANA is unreachable, answers oddly, or the token
is unknown, the call is denied. `GET /audit` reads the log back (the table
refuses UPDATE, DELETE and TRUNCATE at the database level).

Editing `config/governance.yaml` takes effect without a restart. Rules are
matched in order, so explicit allowances go above broader blocks.

| Variable | Meaning |
|---|---|
| `ANA_GOVERNANCE` | `off` restores the old bypass behavior; anything else (including unset) keeps the gate on |
| `ANA_GOVERNANCE_RULES` | Path to the rule set (default `config/governance.yaml`) |
| `ANA_APPROVAL_TIMEOUT_SECONDS` | How long an approval card waits before the call is denied (default 300) |
| `CODEX_SANDBOX` | Codex's own sandbox mode. Leave unset under Docker: codex sandboxes through bubblewrap, which cannot start in a container ("bwrap: No permissions to create a new namespace"). The hook still runs and still denies, so the gate plus the container stays the enforcement boundary |
| `ANA_WORKSPACE_ROOT` | Root new workspaces are created under (default `$ANA_WORKSPACE_DIR/sessions`) |
| `ANA_ROUTING_RULES` | Path to the routing rules (default `config/routing.yaml`) |
| `ANA_FIVE_HOUR_TOKEN_BUDGET` / `ANA_WEEKLY_TOKEN_BUDGET` | Budgets the status bar estimates against when the CLI reports no window of its own |
| `ANA_SECRET_KEY` | Key that encrypts stored secrets at rest. Changing it makes existing rows unreadable (they are skipped, and listed so you can re-enter them) |
| `ANA_MAX_THREAD_COST_USD` / `ANA_MAX_THREAD_TOKENS` | Circuit-breaker budget per thread (defaults 5.0 / 2,000,000) |
| `ANA_RATE_LIMIT_UTILIZATION` | Window utilization that counts as "at the cap" (default 0.95) |
| `ANA_FAILOVER_COOLDOWN_SECONDS` | How long a thread stays on its fallback before retrying the preferred one (default 900) |

### Workspaces

A workspace is a folder plus the one lead agent that works in it. It owns
exactly one ongoing conversation, so the workspace id is also the thread id —
its CLI sessions, checkpointed state, turn history and directory all line up
behind one name.

- The sidebar lists workspaces; creating one either makes a folder under
  `ANA_WORKSPACE_ROOT` or adopts a path you give it (the contents are never
  touched). Removing one unregisters it and leaves the folder alone.
- Both CLIs run with that folder as their working directory, and the gate
  escalates any tool call whose paths reach outside it — read-only system
  paths (`/usr`, `/proc`, …) are exempt so the prompt stays meaningful.
- `GET/POST /workspaces`, `PATCH/DELETE /workspaces/{id}` back the UI, and
  `WS /ws/chat?workspace=<id>` opens one (`thread_id=` is still accepted).
- An id that was never registered is adopted rather than rejected, so
  conversations from before workspaces existed keep working.

### Per-workspace containers

A workspace can run its CLIs inside its own container with its own published
ports, so a dev server the agent starts is reachable from the browser:

- Turn it on per workspace in the sidebar (image optional, ports like
  `5173, 8080:80`). Nothing starts on save — the container comes up the next
  time the workspace is used.
- The workspace folder is mounted at the same path inside, so every path the
  agent, the gate and the audit log mention means one thing.
- The CLIs are still screened by the gate: the hook inside the container
  reaches ANA at `ANA_CONTAINER_GATE_URL` (the service name on the compose
  network), and fails closed if it cannot.
- A container whose image, ports or mounts no longer match its workspace is
  replaced rather than reused.

This needs the Docker socket, which is **host-root equivalent**. It is off
unless `ANA_WORKSPACE_CONTAINERS=on` *and* the socket is mounted; the UI shows
the switch as unavailable otherwise rather than failing at turn time.

| Variable | Meaning |
|---|---|
| `ANA_WORKSPACE_CONTAINERS` | `on` enables the feature |
| `ANA_WORKSPACE_IMAGE` | Default image (`ana-server:latest`, which carries both CLIs) |
| `ANA_WORKSPACE_NETWORK` | Network workspace containers join (`ana_default`) |
| `ANA_CONTAINER_GATE_URL` | Gate URL as seen from inside a workspace container |
| `DOCKER_GID` | Group id of the host's docker socket (the server runs as uid 1000) |

### Secrets

`/secrets` in the web UI (also `GET`/`PUT`/`DELETE /secrets` on the API)
registers credentials, project- or global-scoped. Values are encrypted at
rest, injected into both CLIs as environment variables, and never returned by
the API. The gate blocks any tool call that carries a registered **value**
(using `$THE_VARIABLE` is the supported path) and masks it in the audit log,
so the log never becomes the leak.

### Failover

A turn that dies on a provider usage limit is retried down a chain rather
than surfaced as an error: the Claude CLI's other auth mode first (different
billing bucket), then Codex, which receives a handoff brief since it has none
of the thread's context. Crossing the thread's cost/token budget starts the
next turn one rung down. The thread returns to its preferred rung once the
cooldown expires — no scheduler, no probing. The chat shows one
`⤵ claude/pro → claude/api` line where it happened.

## Web UI

Built from shadcn/ui components (`base-nova` style, Base UI primitives):

- **Sidebar (inset)** lists workspaces — folder, turn count, last agent —
  and opening one switches the whole conversation to it. Creating a workspace
  takes a name and an optional existing folder to adopt.
- **Auto routing**: the composer has no agent buttons by default. The rules in
  `config/routing.yaml` pick the CLI per message (implementation to Codex,
  explanation and review to Claude; no match keeps the CLI that already holds
  the context), and the header shows which model answered and why. `Claude` /
  `Codex` remain as a manual override.
- **Tool calls** appear under each reply as the gate screens them, and
  `/tools` is the full append-only log with a verdict filter.
- **Status bar** shows each CLI's 5-hour and 7-day windows: Claude's are the
  provider's own numbers, the Codex ones are ANA's estimate against
  `ANA_FIVE_HOUR_TOKEN_BUDGET` / `ANA_WEEKLY_TOKEN_BUDGET` and are labeled as
  estimates.
- **Markdown** replies, and a light/dark toggle.
- **MessageScroller + Message + Bubble** render the transcript, so streaming
  follow, scroll anchoring and jump-to-latest are the component's job rather
  than hand-written scroll math.
- **Marker** rows mark the two things that are the system talking rather than
  an agent: a handoff between CLIs and a failover switch.
- **Alert** cards carry gate approvals inline in the transcript.

## Planner-Worker (sub-agents)

Toggle **Plan** in the composer and the lead first answers with a plan instead
of the request: either "just answer this" or a list of independent sub-tasks,
each routed to the CLI it judges right for the job (roughly: implementation to
Codex, explanation and review to Claude). ANA runs those workers as separate
CLI sessions, then hands their results back to the lead to synthesize one
answer. The chat draws the fan-out with React Flow as each worker starts and
finishes.

- Workers share the thread's workspace and gate token, so every tool call they
  make is screened and confined exactly like the lead's.
- `ANA_MAX_SUBAGENTS` (default 5) caps concurrency; extra tasks queue.
- `ANA_SUBAGENT_TIMEOUT_SECONDS` (default 600) bounds each worker. A timeout or
  an error is a task result, not a turn failure — the lead is told what failed
  and still answers.
- `ANA_PLANNER_DEFAULT=on` makes planning the default for every turn. It is off
  otherwise, because planning costs one extra CLI call.

## Chat behavior

- **Reload-safe screen**: the browser refetches the thread's turns from
  `GET /threads/{id}/turns` on load, so the chat comes back instead of
  appearing empty while the agents still remember everything.
- **Stop**: while a turn streams, Send becomes Stop. The server cancels that
  one turn and kills the CLI subprocess; the connection, and anything queued
  behind it, stay alive. A cancelled turn's partial output is still recorded.
- **Cost / tokens**: the header shows the thread's cumulative cost and tokens
  from checkpointed graph state. Only the Claude CLI reports dollars; the
  Codex CLI reports tokens, so both numbers are shown.
- **Queueing**: messages sent during a turn are processed in order rather than
  interrupting it.

## Layout

```
apps/server     FastAPI app: /ws/chat, LangGraph wiring
apps/web        Next.js + shadcn/ui chat UI with the agent switcher
packages/adapters      claude_code.py, codex.py — one subprocess per turn
packages/orchestrator  handoff.py — cross-CLI context brief
packages/memory        Postgres/pgvector turn storage and recall
```

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r apps/server/requirements.txt
.venv/bin/python -m pytest
```

The suite starts a throwaway `pgvector/pgvector:pg16` container, so a working
Docker daemon is required. Adapter tests run against fake CLIs
(`apps/server/tests/fixtures/`) and never call a real model.
