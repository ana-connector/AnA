# ANA — MCP & Skills (roadmap stage 6)

Date: 2026-09-23
Status: implemented

## Goal

Roadmap stage 6, as written: reuse existing open-source MCP servers
(① Git/GitHub, ② Postgres) instead of writing our own, define skills as YAML
validated by Pydantic, and partition MCP access by agent role.

Built additively while other work was in flight on the same tree, so the
footprint in existing files is deliberately tiny — everything else is new
modules (see "Blast radius" below).

## Design

### MCP: config decides, the gate enforces

```
config/mcp.yaml ──► packages/mcp/registry.py ──► packages/mcp/wiring.py
                         (role scoping)              │
                                                     ├─► launcher.py  (one sh script + env file per server)
                                                     ├─► config_writer.py
                                                     │     ├─ claude: --mcp-config <file> --strict-mcp-config
                                                     │     └─ codex:  -c mcp_servers.<n>.command="..."
                                                     └─► build_adapter() / build_codex_adapter()

apps/server/app/gate.py ──► packages/mcp/scope.py   (a call outside the role's scope is denied + audited)
```

- **The registry is a file.** `config/mcp.yaml` is re-read whenever its mtime
  changes, the same contract `config/governance.yaml` and `config/routing.yaml`
  already use. Adding a server is an edit, not a restart.
- **The lead gets no MCP servers at all.** It plans and delegates; the gate
  already refuses it every tool call but the delegation ones. Servers are
  scoped `roles: [worker]`.
- **`--strict-mcp-config` is not optional.** Without it the Claude CLI also
  loads whatever MCP servers the host user configured for themselves, and the
  role scoping would be advisory rather than enforced.
- **Secrets never reach argv.** Codex takes MCP config as `-c` overrides,
  which land in the command line where `ps` inside the workspace container
  shows them to the agent the secret is being kept from. So both CLIs are
  pointed at a generated launcher script (`$ANA_STATE_DIR/mcp/<name>`, 0700)
  that sources a sibling `<name>.env` (0600). The state directory sits outside
  the session workspace, so an agent reading those files trips the gate's
  existing workspace-escape rule.
- **The Postgres server gets the DSN the agent does not.** Stage 2 strips
  `DATABASE_URL` from every agent subprocess on purpose; the server's own
  environment carries it, and `--access-mode=restricted` keeps it to reads.
- **A server that cannot run is skipped, not fatal.** Missing executable,
  missing secret, disabled, out of scope — each becomes a reason string shown
  at `GET /mcp/servers`. The availability probe is skipped when the CLI runs
  inside a workspace container, where the container's PATH is what counts.

### Skills: a catalog, not a dispatcher

`packages/skills/library/*.yaml`, validated by Pydantic
(`packages/skills/models.py`), loaded with user overrides from
`$ANA_STATE_DIR/skills/`, and rendered into the agent's system prompt as a
one-line-per-skill catalog (`packages/skills/catalog.py`).

Four shipped skills: `git-commit`, `db-inspect` (postgres), `github-pr`
(github), `code-review` (no MCP server needed).

- **Validation is strict.** `extra: forbid` on every model, kebab-case names,
  and instructions may not reference an input nobody declared — a skill that
  fails validation is dropped with a message at `GET /skills` rather than
  shipped into a prompt where the failure looks like the model ignoring us.
- **`skill.render(inputs)` validates against a model built from the YAML.**
  That is what stage 6's "Pydantic으로 입출력 스키마 검증" buys: the arguments
  are checked before they are pasted into a prompt.
- **A skill is only offered when its MCP servers are actually scoped.**
  Offering `git-commit` to an agent with no git server walks it into a gate
  denial; the catalog filters on the resolved server names.

## Blast radius (parallel work was in flight)

New: `config/mcp.yaml`, `packages/mcp/*`, `packages/skills/*`,
`apps/server/app/capabilities.py`, six test files.

Touched, minimally:

| File | Change |
|---|---|
| `packages/adapters/claude_code.py` | `mcp_config_path` param; 2 argv lines |
| `packages/adapters/codex.py` | `mcp_args` param; 1 argv line |
| `apps/server/app/websocket.py` | 2 imports; one `claude_mcp()`/`codex_mcp()` call per factory |
| `apps/server/app/gate.py` | 2 imports; one scope check block |
| `apps/server/app/main.py` | 1 import; 1 `include_router` |
| `apps/server/Dockerfile` | install `uv` (for `uvx`) |

No reflow, no restructuring, no shared signature changed in a
backwards-incompatible way — every new parameter is keyword-optional.

## Switches

- `ANA_MCP=off` — no MCP servers for anyone (the escape hatch
  `ANA_GOVERNANCE=off` gives the gate).
- `ANA_MCP_REGISTRY` — an alternative `mcp.yaml`.
- `ANA_SKILLS_DIR` — where user skills live (default `$ANA_STATE_DIR/skills`).

## Verification

Live, in the running stack (2026-09-23, `claude` 2.1.197 in the server image):

```
$ claude -p "MCP로 붙은 도구 이름만 나열해" --mcp-config ... --strict-mcp-config
mcp__git__git_status, mcp__git__git_commit, ... mcp__postgres__execute_sql, ...
```

`GET /mcp/servers` reports git and postgres active, github inactive with
"시크릿 미등록". `codex mcp list` with the generated `-c` overrides shows the
server `enabled`, and a live Codex turn produced a `mcp__<server>__<tool>`
name at the gate, confirming the spelling `packages/mcp/scope.py` parses.

The first real turns also showed the lead CLI process picking up an
**account-level** MCP server (claude.ai Claude Docs) that ANA never granted,
because a role with no servers was given no `--mcp-config` at all. Fixed two
ways: the config is now always written and always passed with
`--strict-mcp-config`, and the lead process carries the *worker* server set —
the CLI's native sub-agents run inside that process and are workers, while
the gate still refuses the lead itself every tool call but delegation.

Three things had to be fixed to get there, all recorded in `config/mcp.yaml`:
`mcp-server-git` exits when `--repository` is not a git repository (the
default workspace is a plain directory); `postgres-mcp` needs `--with "mcp<2"`
because it imports the FastMCP name mcp 2.x removed; and `config/` is baked
into the image, so the repo copy is now bind-mounted read-only — otherwise
"edit the YAML, no restart" was only true outside Docker.

`420 passed` (full suite, 2026-09-23), including 54 new tests:

- `test_mcp_registry.py` — role scoping, placeholders, skip reasons.
- `test_mcp_wiring.py` — launcher secrecy and quoting, both CLIs' argv.
- `test_mcp_scope.py` — gate-side scope decisions, both tool-name forms.
- `test_mcp_gate.py` — the endpoint: allowed, denied, audited.
- `test_skills.py` — schema validation, rendering, catalog filtering.
- `test_capabilities_endpoint.py` — `/mcp/servers`, `/skills`.

## Not done / known gaps

- **Codex's built-in app server is denied, by design.** A live Codex turn
  called `mcp__codex_apps__codex_document_control__list_document_sessions`
  and the gate refused it as an unregistered server. Codex has no
  `--strict-mcp-config`, so its own server stays visible to the model; the
  gate is the enforcement point there. Add it to `config/mcp.yaml` if it
  should be usable.
- **`claude mcp list` / `codex mcp list` do not show ANA's session config.**
  Those subcommands read the user/project config files; the per-session
  servers arrive as `--mcp-config` and `-c` overrides on the run itself. An
  agent asked "which MCP servers do you have" will answer from the wrong
  place — ask it to list its `mcp__*` tools instead, or read
  `GET /mcp/servers`.
- **No UI.** `/mcp/servers` and `/skills` are read-only endpoints; the settings
  page does not show them yet.
- **The planner does not name skills.** Agents pick a skill from the catalog
  themselves; `parse_plan()` has no `skill` field, which would be the natural
  next slice.
- **`packages/mcp_servers/` (custom in-house servers) is not started** — the
  roadmap says to build one only when no open-source server exists.
