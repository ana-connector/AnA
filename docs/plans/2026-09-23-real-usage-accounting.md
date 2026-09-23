# ANA — real usage accounting (stage 4 follow-up)

Date: 2026-09-23
Status: implemented

## The question

The status bar carried two kinds of number: Claude's real window utilization
(from the CLI's `rate_limit_event`) and, for Codex, ANA's own estimate of
tokens spent against a configured budget. The estimate is a guess about a
budget nobody confirmed — so: can both be real?

Yes, for both CLIs. Neither number had to be invented.

## What each CLI actually gives (verified 2026-09-23)

**Claude** (`claude -p --output-format stream-json`, 2.1.280) emits, per turn:

```json
{"type": "rate_limit_event", "rate_limit_info": {
  "unifiedWindows": {"five_hour": {"utilization": 0.4,  "resetsAt": 1790163000},
                     "seven_day": {"utilization": 0.05, "resetsAt": 1790474400}}}}
```

Already consumed; the reset instants were being dropped.

**Codex** (`codex exec --json`, 0.156.1) emits token counts only — no limits.
But the CLI writes them into its rollout log for the session:

```json
"rate_limits": {"limit_id": "codex",
                "primary":   {"used_percent": 95.0, "window_minutes": 300,   "resets_at": ...},
                "secondary": {"used_percent": 29.0, "window_minutes": 10080, "resets_at": ...}}
```

`$CODEX_HOME/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl`, one record per turn.

## Design

`packages/observability/codex_usage.py` reads the newest such record back:
the five most recently touched rollout files, last 256 KiB of each, scanned
backwards for `rate_limits`, cached on the newest file's mtime. Windows are
matched by `window_minutes` rather than by the names "primary"/"secondary" —
what matters is which of ANA's two windows a figure belongs to, and the CLI
says that in minutes.

It is read in two places: after every Codex turn (in `finally`, so a turn that
died *on* a limit still reports it) and on `GET /usage`, so a restarted server
shows the real figure before it has run anything.

`UsageTracker` now stores a reset instant next to every reported figure, and a
reported window **expires at its reset** — after that, "95% spent" describes a
window that no longer exists, and the bar falls back to the estimate. Manual
estimates already worked this way as of the reset-time change.

The status bar shows `↺ 14:30` (or `↺ 9/28 14:30` for another day) next to
each window, and the "추정" badge now appears only where a figure really is
ANA's own guess.

## What is still an estimate

- A CLI ANA has never run in this state directory has no figures at all; the
  token-budget estimate is the fallback, and it is labeled.
- The token budgets (`ANA_FIVE_HOUR_TOKEN_BUDGET`, `ANA_WEEKLY_TOKEN_BUDGET`)
  still only matter for that fallback.
- Codex's log is per `CODEX_HOME`. ANA prepares its own (shared with workspace
  containers through the state volume), so turns run outside ANA — the user's
  own terminal, with the real `~/.codex` — are not counted. They are the same
  account, so the provider's percentage already includes them; it simply
  arrives at ANA's bar one ANA turn later.

## Verification

Live, with the stack running: `GET /usage` reports Codex at
`"source": "reported"`, `five_hour 0.96`, `seven_day 0.29`, both with reset
instants — and the worker ladder's `until: 0.9` rung now steps past Codex on
its own, which is the whole point of having a real number.

Full suite: 443 passed, including `apps/server/tests/test_codex_usage.py`.
