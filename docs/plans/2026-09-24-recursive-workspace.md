# The recursive workspace: ANA on ANA

2026-09-24

## What this is

ANA's own checkout, registered as an ordinary workspace (`ana-self`), so the
agent that develops ANA is reached the same way as the agent that works on any
other project: same list, same gate, same worktree-per-sub-agent isolation,
same audit log.

Nothing about it is a special mode. It is one row in `workspaces` whose `path`
happens to be the repository this file is in.

## Why it is worth doing deliberately

A workspace is "a folder, and the one lead agent that works in it". ANA's own
repository is a folder. Pointing a workspace at it needs no new concept — but
it does need three things to line up, and each of them is a way to get it
wrong:

1. **One path, host and container.** The folder has to be mounted at the path
   it has on the host, not somewhere convenient under `/app`. A per-workspace
   container's bind mounts are resolved by the Docker daemon on the *host*, so a
   server-only path would silently create an empty directory and the workspace
   container would come up looking at nothing. Hence
   `- ./:${ANA_SELF_WORKSPACE:-/home/hello/ana}` and the same value in the
   environment.

2. **What is live, and what needs a rebuild.** `config/` and
   `packages/skills/library` are mounted read-only from the same checkout and
   re-read on mtime change, so a governance rule, a routing rule or a skill
   edited in this workspace takes effect on the next turn. Everything else —
   the FastAPI app, the adapters, the web UI — runs from the image, so those
   edits need `docker compose up --build`. The workspace shows the source; it
   does not hot-swap the running orchestrator.

3. **The agent must not be able to rewrite its own leash.** Point 2 means an
   agent working here could edit `config/governance.yaml` — the file the gate
   reads — and have that take effect on its next tool call, with nobody asked.
   Every other rule in the file is downstream of that one, so the rule set now
   carries `ana-governance-self-edit`: any tool call whose input names
   `config/governance.yaml`, `packages/governance/{rules,hook,secrets}.py` or
   `ANA_GOVERNANCE_RULES` is `ask`, for every tool rather than just Bash (the
   CLIs write files through Edit/Write/apply_patch, and the path is in the
   serialized input either way).

## Implementation

- `packages/orchestrator/workspaces.py`
  - `SELF_WORKSPACE_ID = "ana-self"`, reserved in `_unique_id` so a user
    workspace called "ANA self" cannot take the id the server registers.
  - `self_workspace_path()` reads `ANA_SELF_WORKSPACE` and returns it only if it
    is a directory: a pointer to a folder the server cannot see would only fail
    at the first turn.
  - `ensure_self_workspace()` upserts the row on startup, re-asserting the path
    (the mount may have moved) and leaving the name alone (renaming it is the
    user's call).
- `apps/server/app/main.py` calls it in `lifespan`, suppressing failures — a
  registry problem must not stop the server from serving chat — and reports the
  id from `GET /workspaces/capabilities` as `self_workspace`.
- The web UI marks that row and warns inside its settings dialog. It is still
  an ordinary workspace: renameable, containerizable, deletable (the next start
  registers it again).

## What was deliberately not built

- No separate "ANA develops ANA" mode, page or agent role. The whole argument
  for the workspace concept is that one folder plus one lead agent is the unit;
  a second mechanism for the same thing would be two ways to describe one row.
- No automatic restart or rebuild after an edit here. An orchestrator that
  restarts itself mid-turn on a file write is a way to lose a conversation, and
  `docker compose up --build` is one line the user can run when they mean it.
- No write-protection beyond the governance rule. The point of the workspace is
  that the agent can change these files; the rule only ensures it cannot change
  the rules about changing them without being asked.
