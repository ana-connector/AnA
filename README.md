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
| `ANA_QUESTION_TIMEOUT_SECONDS` | How long the agent's own question waits for an answer (default 900). A timeout is a result, not a failure: the agent states its assumption and carries on |
| `CODEX_SANDBOX` | Codex's own sandbox mode. Leave unset under Docker: codex sandboxes through bubblewrap, which cannot start in a container ("bwrap: No permissions to create a new namespace"). The hook still runs and still denies, so the gate plus the container stays the enforcement boundary |
| `ANA_WORKSPACE_ROOT` | Root new workspaces are created under (default `$ANA_WORKSPACE_DIR/sessions`) |
| `ANA_SELF_WORKSPACE` | Absolute host path of ANA's own checkout, registered as the `ana-self` workspace. Unset (or not a directory) and the recursive workspace does not appear |
| `ANA_ROUTING_RULES` | Path to the routing rules (default `config/routing.yaml`) |
| `ANA_FIVE_HOUR_TOKEN_BUDGET` / `ANA_WEEKLY_TOKEN_BUDGET` | Budgets the status bar estimates against when the CLI reports no window of its own |
| `ANA_BACKGROUND_TURNS` | `off` ties a turn's life to the connection that asked for it (the old behavior); anything else keeps it running when the browser leaves |
| `ANA_RUNNER_IDLE_SECONDS` | How long a conversation's runner — queue, gate token, CLI subprocesses — is kept after the last browser detaches and nothing is running (default 900) |
| `ANA_PLAN_ROUNDS` | How many planning rounds one turn may use: the first plan plus that many chances to extend it (default 2; 1 = plan once) |
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
  touched).
- Deleting one destroys its **conversation** — turns and checkpoints, which
  nothing undoes — so it goes through a confirmation that names what is lost.
  The folder, and the audit log, stay; a per-workspace container is removed
  with it rather than left holding its ports. Deleting the workspace you are
  in opens another existing one instead of minting a fresh conversation.
- Both CLIs run with that folder as their working directory, and the gate
  escalates any tool call whose paths reach outside it — read-only system
  paths (`/usr`, `/proc`, …) are exempt so the prompt stays meaningful.
- `GET/POST /workspaces`, `PATCH/DELETE /workspaces/{id}` back the UI, and
  `WS /ws/chat?workspace=<id>` opens one (`thread_id=` is still accepted).
- An id that was never registered is adopted rather than rejected, so
  conversations from before workspaces existed keep working.

### The recursive workspace (ANA on ANA)

Mount ANA's own checkout and it appears in the list as an ordinary workspace,
`ana-self`, so the agent that edits ANA is reached the same way as any other
project — same gate, same worktree-per-sub-agent isolation, same audit log:

```yaml
# docker-compose.yml (already wired)
- ./:${ANA_SELF_WORKSPACE:-/home/hello/ana}
```

- `ANA_SELF_WORKSPACE` is the absolute **host** path of the checkout. It is the
  path inside the server container too, because a per-workspace container's
  bind mounts are resolved by the daemon on the host — one path, or the two
  disagree about what the workspace is.
- The row is re-asserted on every start (the mount may have moved) but its name
  is not: renaming it is yours. Deleting it works like any other workspace; the
  next start registers it again.
- `config/` and `packages/skills/library` are mounted read-only from the
  same checkout and re-read on mtime change, so a rule or a skill edited in
  this workspace is live without a rebuild. That is also the hazard: an agent
  could rewrite the rules that screen it. The `ana-governance-self-edit` rule
  escalates every tool call touching `config/governance.yaml` or
  `packages/governance/{rules,hook,secrets}.py` to an approval card.
- Everything else (the FastAPI app, the adapters, the web UI) runs from the
  image, so those edits need `docker compose up --build` — the workspace shows
  you the source, it does not hot-swap the running orchestrator.

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

### The agent asking you

Everything else here flows one way — you ask, the agents work, the gate
interrupts when a tool call needs a decision. So a lead facing a real fork in
the requirements had to guess, and a wrong guess surfaces at the end.

`ask_user` is ANA's own MCP server (`packages/mcp/servers/ask_user.py`), scoped
to the lead alone. The lead calls it with up to four questions, each with
optional choices; the question appears in the chat as a card, the turn waits,
and the answers come back as the tool's result.

- **Why MCP**: the gate is driven by the CLIs' `PreToolUse` hook, which only
  fires because the agent already did something. A question the agent starts
  needs a tool the agent can call.
- **Why the lead alone**: several workers asking at once would race for one
  human. A worker that needs a decision reports back to the lead, and the gate
  denies its call here as out of scope.
- **Why it waits rather than fails**: an unanswered question returns "nobody
  answered" after `ANA_QUESTION_TIMEOUT_SECONDS`, and the lead is told to state
  its assumption and continue. A turn is never lost because someone stepped away.
- The call is still gated and audited like any other, with an explicit `allow`
  rule (`ask-user`) so the question itself never waits behind an approval — and
  the question and its answer are appended to the turn's transcript, so
  reopening the conversation shows what was asked.
- No new dependency: the stdio protocol (`initialize`, `tools/list`,
  `tools/call`) is ~200 lines of stdlib, against `@shadcn/questionnaire` on the
  browser side.

### Answering from a phone (Discord)

An approval card and an `ask_user` questionnaire both park on one future
(`packages/governance/session.py`). The Discord bridge is a second door to that
same future: the request is posted to a Discord channel at the moment the
browser card appears, either side can answer, and the first answer wins — the
other side's card is withdrawn rather than left offering a decision that is
already made.

It is not a Discord front end. There is no way to start a turn, pick a
workspace or change settings from Discord; the bot carries the two things that
block an agent mid-work, plus a short note when a turn ends.

- **Why a bot, not a webhook**: the Discord gateway is an outbound WebSocket.
  ANA dials Discord and button clicks arrive back down the same socket, so
  there is no public inbound, no tunnel and no auth system to design. The
  allowlist is a set of Discord user ids.
- **Who may answer**: only ids in `ANA_DISCORD_ALLOWED_USER_IDS`, and only in
  `ANA_DISCORD_CHANNEL_ID`. Anyone else gets an ephemeral refusal and the
  future is never touched. An empty allowlist means nobody, not everyone — a
  button here approves commands the rule set already thought worth stopping.
- **The log says who**: `decided_by` becomes `discord:<user id>`, next to the
  existing `user` (the web UI), `rule` and `timeout`.
- **Discord being down is not a denial**: a failed post is swallowed and
  logged. The browser card and the gate's own timeout are unaffected.
- No new dependency: `websockets` and `httpx` are already pinned, and the part
  of the gateway that matters is identify, heartbeat, resume and one dispatch
  event — the same trade `ask_user` makes for MCP.

| Variable | Meaning |
| --- | --- |
| `ANA_DISCORD_BOT_TOKEN` | Bot token. Unset disables the bridge entirely — no client is started |
| `ANA_DISCORD_CHANNEL_ID` | Channel that receives requests and turn summaries. Interactions from any other channel are ignored |
| `ANA_DISCORD_ALLOWED_USER_IDS` | Comma-separated Discord user ids permitted to answer. Empty means nobody can. `ANA_DISCORD_ALLOWED_USER_ID` (singular) is read as well, for the one-person case |
| `ANA_DISCORD_TURN_SUMMARIES` | `off` stops posting what the agent finally said at the end of a turn |

Setup: create an application at <https://discord.com/developers/applications>,
add a bot, copy its token into `ANA_DISCORD_BOT_TOKEN`, and invite it to the
server with the `bot` scope and permission to send messages in the channel. No
privileged intents are needed — interactions are delivered regardless of them.
Your own user id comes from Discord's *Copy User ID* (Developer Mode on).

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
- **Sub-agent transcripts** open from the plan they belong to — click a task
  in the graph or its row in the list and a side panel shows what that worker
  was told, what it ran and what it answered, with the conversation still on
  screen behind it. This used to be a `Select` in the header that listed every
  task of every turn and replaced the whole transcript with one worker's
  output.
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
- **A status line** says what is happening while nothing is arriving: which
  model, which phase (planning, the fan-out, re-planning, writing the answer)
  and how long it has been going. A turn can spend minutes before the first
  character of the answer exists, and silence in that window reads exactly like
  a broken connection.
- **Questionnaire** cards carry the agent's own questions inline in the
  transcript, above the approvals: radio or checkboxes when the lead offered
  choices, always with a free-text box, because the answer it did not think of
  is the one worth having.
- **Alert** cards carry gate approvals inline in the transcript, and an
  **AlertDialog** guards workspace deletion (the one action here that destroys
  something).
- A fresh workspace offers three concrete first requests rather than an empty
  rectangle, and the workspace list grows a filter once it is long enough to
  need one.

## Skills

A skill is a named instruction — the wording an agent should follow for a
recurring job, optionally with declared inputs and an output shape. They live in
`packages/skills/library/` in either of two forms, both validated by Pydantic (a
broken file is reported, not fatal):

- `*.yaml` — ANA's own form, where the procedure is a field and inputs are
  declared and substituted.
- `*.md` — the Agent Skills form: YAML frontmatter, then the procedure as the
  body. A published `SKILL.md` drops in as it was written, which is the point:
  summarizing someone's skill into a YAML string is how a vendored skill quietly
  becomes a paraphrase of itself.

Either way they reach the agent the way Claude Code's own skills do: a terse
catalog in the system prompt, with one skill's full body pulled in only when a
task names it. `$ANA_STATE_DIR/skills/` overrides a shipped file of the same
name.

| Skill | What it is for |
|---|---|
| `code-review` | Review a change for correctness and report only findings |
| `git-commit` | Read the diff and commit one intent, Conventional Commits |
| `github-pr` | Open or handle a PR / issue |
| `db-inspect` | Read ANA's own memory database |
| `ponytail` | Reuse what exists, stdlib and platform features before new code; the shortest working diff ([DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail), MIT) |
| `kill-ai-slop` | Find and strip the machine-default UI and copy tics (gradients, emoji, badge spam, AI voice) ([yetone/kill-ai-slop](https://github.com/yetone/kill-ai-slop)) |
| `caveman` | Compress a report to its technical substance, negations and numbers intact ([JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman)) |

The last three are working-style skills: they change how a worker builds or
writes, not which tools it may call, so they carry no MCP requirement and apply
to any task. They are vendored as their upstream `SKILL.md`, body verbatim —
only the frontmatter is ANA's (a Korean `title` for the settings page, a
one-line `description` for the catalog, and `roles`), and `kill-ai-slop` carries
a marked ANA addendum because its upstream scanner and reference files are not
part of a one-file library. `packages/skills/library` is mounted read-only into
the server, so adding a file is live on the next turn — no rebuild.

## Planner-Worker (sub-agents)

The lead first decides whether to answer directly or make a plan of sub-tasks.
Each task is routed automatically through the worker model ladder. ANA runs those workers as separate
CLI sessions, then hands their results back to the lead to synthesize one
answer. The chat draws the fan-out with React Flow as each worker starts and
finishes.

- Workers share the thread's workspace and gate token, so every tool call they
  make is screened and confined exactly like the lead's.
- If a worker hits a session or usage limit, ANA retries that task on the next
  worker model in `config/routing.yaml`.
- `ANA_MAX_SUBAGENTS` (default 5) caps concurrency; extra tasks queue.
- `ANA_SUBAGENT_TIMEOUT_SECONDS` (default 600) bounds each worker. A timeout or
  an error is a task result, not a turn failure — the lead is told what failed
  and still answers.
- Planning is on for every turn by default; `ANA_PLANNER=off` disables it for
  debugging. If a lead reaches a session limit while planning, replanning, or
  synthesizing, ANA switches models and preserves the worker results.

**The plan is not fixed when it is written.** A plan made before anything has
run is a guess about what the work will turn out to need, so after each round of
tasks finishes the lead is shown the results and asked one question: is this
done, or is there a step these results revealed? Tasks it adds are numbered
after the ones that ran (t3, t4 …) and may depend on them by id — the finished
task's output is attached to the new task's instruction, exactly as it is within
a round. `ANA_PLAN_ROUNDS` (default 2) caps this: a lead that can always add one
more task can also never finish, and each round costs one short planning call
whether or not it adds anything. `ANA_PLAN_ROUNDS=1` restores the old shape —
plan once, run, answer.

If the request itself is ambiguous, the lead can call `ask_user` before planning
rather than guessing which reading was meant.

## Two languages

Korean is what the user reads; English is what a model reads. The split is not
cosmetic — the instruction side is paid for on every turn, and the same words
cost noticeably fewer tokens in English.

| Korean | English |
|---|---|
| The lead's final answer | The lead's and workers' role instructions |
| Task titles, the plan, every UI string | Task prompts, the planning and synthesis prompts |
| The questions `ask_user` puts to the user | Handoff briefs between CLIs, verification retries |
| Approval card descriptions (`config/governance.yaml`) | Gate denial reasons, scope limits, skill instructions and the skill catalog |

Some things therefore exist twice on purpose. A gate refusal carries an English
`reason` for the agent that has to act on it and a Korean `description` for the
person deciding whether to allow it. A skill keeps a Korean `title` for the
settings page and English instructions for the worker that follows them.

## Chat behavior

- **Turns run in the background.** A turn belongs to the conversation, not to
  the browser tab that started it: opening another workspace, closing the tab or
  losing wifi leaves it running. Come back and the conversation says it is
  mid-turn, replays what you missed (the prompt, the text so far, the tool
  calls, the plan, an approval still waiting) and streams the rest live. It used
  to be the opposite — the relay was cancelled and both CLIs killed with the
  connection, so leaving mid-turn threw the work away.
  - One runner per thread owns the queue, the gate token and the CLI
    subprocesses (`apps/server/app/runners.py`). It is closed when it has been
    idle and unwatched for `ANA_RUNNER_IDLE_SECONDS` (default 900), on server
    shutdown, or immediately with `ANA_BACKGROUND_TURNS=off`.
  - The gate token therefore outlives the connection, because the CLI
    subprocesses hold it. An escalation raised while nobody is attached still
    ends in a denial — there is no one to approve it — and the card is waiting
    when you return, within the approval timeout.
  - Two tabs on one conversation: the last to open receives the stream. They
    would otherwise race to answer the same approval.
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
