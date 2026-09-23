# ANA — Kimi Code CLI adapter (third agent)

Date: 2026-09-23
Status: implemented

## Goal

A third CLI on the same `AgentAdapter` interface the roadmap defined in stage
1 and stage 5 extended to Codex: Moonshot's **Kimi Code CLI 2.0.2**, installed
from the vendor's script (there is no npm package; the PyPI `kimi-cli` and
`kimi-code` packages are archived pointers to it).

## Verified CLI facts (this machine, 2026-09-23)

`kimi -p "..." --output-format stream-json` produces:

```json
{"role":"meta","type":"system.version","version":"2.0.2"}
{"role":"assistant","tool_calls":[{"function":{"name":"Bash","arguments":"{\"command\":\"cat sample.txt\"}"}}]}
{"role":"tool","tool_call_id":"...","content":"hello from file\n"}
{"role":"assistant","content":"The command printed: ..."}
{"role":"meta","type":"session.resume_hint","session_id":"session_...","command":"kimi -r session_..."}
```

Four things follow from that, and they shape the whole adapter:

- **No result event.** The stream just ends, so `KimiAdapter` synthesizes the
  `result` AdapterEvent the graph records a turn from.
- **No usage event either** — but the CLI writes token counts into the
  session's own `wire.jsonl` (`{"usage":{"inputOther":1058,"output":69,
  "inputCacheRead":18944,...}}`). Read back after the turn by
  `packages/observability/kimi_usage.py`, the same trick as Codex's rate
  limits, so a Kimi turn counts against the thread's budget instead of looking
  free. There is no per-turn price, so `cost_usd` stays None rather than
  inventing a number.
- **The session id arrives last**, in the resume hint, not first as it does
  for the other two. It is threaded through the parse rather than fixed up
  front.
- **`-p` runs under the `auto` permission policy** and rejects
  `--yolo`/`--auto`/`--plan`, so there is no approval flag to pass — the
  PreToolUse hook is the only screen, which is where ANA wants it anyway.

Everything else Kimi needs lives in a data directory (`KIMI_CODE_HOME`), not
in flags:

| What | Where |
|---|---|
| hooks | `[[hooks]]` in `config.toml` |
| reasoning effort | `[thinking] effort` in `config.toml` (no CLI flag) |
| MCP servers | `mcp.json`, same `mcpServers` shape as Claude's `--mcp-config` |
| credentials | `credentials/`, refreshed in place |

## Design

`prepare_kimi_home()` (packages/governance/cli_config.py) builds ANA's own
home per effort level: `config.toml` **copied** from the user's home with
ANA's hooks appended and `[thinking] effort` set, and `credentials/`,
`device_id`, `region`, `workspace-trust` **symlinked** back. Copy-plus-link
rather than either alone:

- the hooks have to go in the same file as the providers, so it cannot be a
  symlink;
- an OAuth refresh must not diverge into two half-valid copies, so the
  credentials cannot be a copy.

One home per effort because the level is a file, not a flag: two concurrent
sub-agents on different levels would otherwise rewrite one file under each
other. `packages/orchestrator/routing.py` accepts `low`/`high`/`max` for
`kimi` — its own vocabulary, like the other two.

**The hook contract now uses the exit code as well as the JSON.** Kimi reads
the exit code first: `2` blocks, and *any other non-zero code is treated as a
script error and allowed* (fail-open). `packages/governance/hook.py`
therefore exits 2 whenever it denies. Claude and Codex also treat exit 2 as a
block, so one path serves all three and a denial can never be downgraded to an
allow by the runner.

Failover chains gained a Kimi rung: `claude/pro → claude/api → codex → kimi`,
and a chain that starts at Kimi falls back to the other two.

## Verification

Live, inside the running server container:

```
$ python -c "…build_kimi_adapter(…)…"
text   '안녕하세요, 저는 ANA의 하위 에이전트로…'
result session_c1d8be12-…  {'input_tokens': 12150, 'output_tokens': 75, …}
```

— so the binary, the mounted credentials, the prepared home, the synthesized
result and the usage read-back all work end to end.

The gate hook fires too. With the prepared home but no gate configured (the
fail-closed case):

```
{"role":"tool","tool_call_id":"…","content":"Blocked by PreToolUse hook"}
```

`478 passed`, including `test_kimi_adapter.py` (13) against a fake CLI that
emits the real event shapes.

## Two fixes this work turned up

- **Approval futures were resolved across threads.** `resolve_approval` called
  `future.set_result` directly; when the answer arrives on a thread other than
  the loop's, asyncio does not wake the waiter, and the gate request sat until
  its timeout. It now resolves through `call_soon_threadsafe` when the caller
  is elsewhere. The gate endpoint's approval tests had become reliably red (and
  slow — 35s of waiting on timeouts); they are green and fast now.
- **The status bar only listed agents that had already run.** Every CLI ANA
  can route to now has a row from the start, so "nothing here" and "nothing
  spent" stop looking the same.

## Known gaps

- **No provider-reported limits for Kimi.** Claude reports window utilization
  on stdout and Codex in its rollout log; Kimi reports neither, so its bar is
  ANA's token estimate against the configured budget, labeled 추정 like any
  other estimate.
- **No rung in the shipped ladder.** Kimi is selectable in the composer and is
  the last failover rung, but `config/routing.yaml` does not route to it by
  default — adding `- agent: kimi` with an `until:` is a config edit.
- **Effort is per-home, not per-rung-within-a-turn.** Changing effort picks a
  different prepared home, which is fine for a rung but means a home per level
  on disk.
