# Answering from a phone: Discord as a second door to the gate

2026-09-24

## What this is

A second route to the person the agents need. Today an approval card and an
`ask_user` questionnaire can only be answered in the browser tab that holds the
WebSocket. This adds Discord as a parallel destination: the same pending
request is posted to a Discord channel at the same moment it is posted to the
browser, either side can answer it, and the first answer wins.

It is deliberately not a Discord front end for ANA. There is no way to start a
turn from Discord, pick a workspace, or change settings. The bot carries the
two interruptions that block an agent mid-work — *may I run this* and *which do
you want* — plus a short note when a turn ends, so the person who stepped away
from the desk can unblock the work from a phone.

## Why Discord, and why a bot rather than a webhook

The requirement was "answer from a phone, restricted to my devices, without
standing up an auth system". The Discord *gateway* is an outbound WebSocket:
ANA dials Discord, and button clicks arrive back down that same socket as
`INTERACTION_CREATE`. No public inbound, no tunnel, no TLS termination, no
password to design — the allowlist is a set of Discord user ids.

The alternative, a Discord Interactions endpoint, is HTTP *into* ANA and would
need exactly the public surface the gateway avoids. Exposing ANA's own web UI
(over Tailscale or otherwise) was the other candidate and stays possible later;
it was not chosen here because the notification story on iOS requires a PWA
install, and because the existing API is entirely unauthenticated — `/secrets`
included — so putting it on any network beyond localhost is a larger piece of
work than this one.

No SDK is added. `websockets` and `httpx` are already pinned in
`apps/server/requirements.txt`, and the part of the gateway that matters here
is small: identify, heartbeat, resume, and one dispatch event. This follows
what `packages/mcp/servers/ask_user.py` already does for MCP.

## The shape of the thing being extended

Everything the person has to answer is one `asyncio.Future` in
`packages/governance/session.py`:

- `/gate/tool-call` (`apps/server/app/gate.py`) evaluates the rule set, writes a
  `pending` audit row, then parks on `GateRegistry.request_approval`.
- `/gate/ask` parks on `GateRegistry.request_answer`.
- Both send a frame through `GateSession.send`, which `TurnRunner.__init__`
  rebinds to `TurnRunner.emit` — so the frame is buffered for replay even when
  no browser is attached.
- The browser answers with a `{"type": "approval"}` or `{"type": "answer"}`
  frame, which `apps/server/app/websocket.py` turns into
  `resolve_approval` / `resolve_answer`.

So the feature is not a new mechanism. It is a fan-out on the way out and a
fan-in on the way back, around a future that already exists.

## Design

### 1. The notifier seam

A `RemoteNotifier` protocol in `packages/remote/notifier.py`:

```python
async def approval_posted(request_id, session, request) -> None
async def question_posted(request_id, session, request) -> None
async def request_closed(request_id, outcome) -> None
async def turn_ended(session, text) -> None
```

`GateRegistry` holds one (a `NullNotifier` by default, so every existing test
and any deployment without a bot token behaves exactly as before).
`request_approval` and `request_answer` call the notifier right after
`session.send`, and call `request_closed` in their `finally` block, whatever
ended the wait — an answer, a timeout, or a closed connection. A notifier that
raises is swallowed and logged: Discord being down must never turn an approval
into a denial.

`turn_ended` is driven from `TurnRunner.emit` when a frame's type is in
`TURN_END_FRAMES`, which is the same place the replay buffer is already cleared.

### 2. Fan-in, and who answered

Two changes to the resolution path:

**A request id is addressable without a token.** `GateRegistry` keeps
`request_id -> GateSession` for the life of the request. The browser path does
not need it (it has the session), but a Discord interaction only carries what
was packed into the `custom_id`.

**The answer carries its author.** `_Pending.resolve` takes a `RemoteAnswer`
(`value`, `answered_by`) rather than a bare value, and `request_approval`
returns it. `gate.py` passes `answered_by` into the audit row's `decided_by`,
so the log distinguishes `user` (the browser) from `discord:<user id>`. The
Prometheus counter keeps using the part before the colon, so the label
cardinality stays bounded no matter how many people can answer.

When either side resolves a request, `GateSession.on_resolved` fires.
`TurnRunner` binds it to a small handler that discards the buffered card
(`discard_request`, which already exists so an answered card does not come back
on reconnect) and emits a `{"type": "request_resolved", "request_id", "by"}`
frame. The browser drops the card it is showing, which is what stops two people
staring at a question that is already answered.

### 3. What Discord shows

Pure functions in `packages/remote/render.py` — no network, no client object —
so the mapping is unit-tested directly.

**An approval** is one message: the tool name and the rule's Korean
`description` as the body, the workspace name and the agent as fields, the
command itself in a fenced block (truncated to Discord's limits), and two
buttons, `허용` / `거부`.

**A questionnaire** is one message with one string select per question (the
schema caps `ask_user` at four questions, and Discord allows five action rows,
so a select per question plus a final button row always fits). A question that
also offers free text gets a `직접 입력` button; that button opens a modal
carrying every free-text question in the request, because a modal can hold five
inputs and can only be opened as the direct response to a click. Selections
accumulate server-side against the request id, and `보내기` submits them.
Required questions that are still empty produce an ephemeral warning rather
than a partial answer.

The answer handed back is the same `(string | string[])[]` the browser produces
in `apps/web/src/components/ui/question-card.tsx`: one entry per question, in
order, a list when `multi` is set and a string otherwise. That is what
`/gate/ask` records in the transcript and what `ask_user` hands to the agent,
so neither has to learn that Discord exists.

`custom_id` is `ana:<kind>:<request_id>:<field>` — 36 characters of UUID leaves
room inside Discord's 100-character limit.

### 4. Identity, authority and audit

A Discord button is an allow button for commands the rule set already thought
worth stopping. Three things follow:

- **Allowlist.** `ANA_DISCORD_ALLOWED_USER_IDS` is a comma-separated list of
  Discord user ids. An interaction from anyone else is answered with an
  ephemeral refusal and never touches the future. An empty allowlist disables
  answering entirely rather than allowing everyone.
- **One channel, named.** `ANA_DISCORD_CHANNEL_ID` is where requests are
  posted; interactions from any other channel are ignored. Every message names
  the workspace, because one channel carries every conversation.
- **The log says who.** `decided_by` becomes `discord:<user id>`. The existing
  `user` value keeps meaning "answered in the web UI", so nothing already
  written changes meaning.

The bot token is read from the environment, never from the `secrets` table, and
never logged. `ANA_DISCORD_BOT_TOKEN` being unset is the off switch: no client
is started and the notifier stays null.

### 5. Reading

Kept to the minimum that makes the answering usable: when a turn ends, the
final answer text is posted to the same channel, truncated, with the workspace
name. That is enough to know whether the thing you approved worked without
opening a laptop. Streaming deltas, tool calls and sub-agent panels stay in the
web UI, where there is room for them.

### 6. Connection lifetime

The gateway client is one asyncio task started in the FastAPI lifespan. It
identifies with intents `0` (interactions are delivered regardless of intents),
heartbeats on the interval from `HELLO`, tracks the sequence number, and
resumes after a drop. `INVALID_SESSION` and `RECONNECT` restart it; repeated
failures back off. A request posted while the socket is down is simply not
posted — the browser card and the gate's own timeout are unaffected, which is
the property worth protecting.

## Configuration

| Variable | Meaning |
| --- | --- |
| `ANA_DISCORD_BOT_TOKEN` | Bot token. Unset disables the whole feature. |
| `ANA_DISCORD_CHANNEL_ID` | Channel that receives requests and answers. |
| `ANA_DISCORD_ALLOWED_USER_IDS` | Comma-separated user ids permitted to answer. Empty means nobody. |
| `ANA_DISCORD_TURN_SUMMARIES` | `off` to stop posting turn-end summaries. |

## Testing

- `render.py` is pure: message payloads for approvals and questionnaires,
  `custom_id` round-trips, truncation at Discord's limits, answer assembly
  including multi-select, free text and skipped optional questions, and the
  refusal to submit with a required question unanswered.
- The registry fan-out is tested with a recording fake notifier: posted on
  request, closed on answer, closed on timeout, closed on a dropped connection.
- The fan-in is tested by resolving through the registry by request id and
  asserting the future's value and `answered_by`, that an unknown or
  already-answered request id is a no-op, and that an interaction from a user
  outside the allowlist resolves nothing.
- `decided_by` reaching the audit row as `discord:<id>` is tested through
  `/gate/tool-call` with a fake remote answer, next to the existing tests that
  assert `user` and `timeout`.
- The gateway client itself is not unit-tested against Discord; its framing
  (identify, heartbeat, resume payloads) is tested as pure dict construction,
  and the socket loop is kept thin enough to be read.
