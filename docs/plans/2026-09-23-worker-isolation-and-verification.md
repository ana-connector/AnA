# ANA — worker isolation and the verification loop

Date: 2026-09-23
Status: implemented

Two of the gaps the audit called tier-1, plus the workspace fix that makes the
first one actually apply.

## 1. A git worktree per sub-agent

**The problem.** Five workers, one directory, no locking. Two tasks touching
the same file silently clobbered each other — last writer won and the other
worker's edit was simply gone — and a task that went wrong left its mess in
the workspace with nothing to roll back to.

**Now** (`packages/orchestrator/worktrees.py`): each task gets
`git worktree add -B ana/<thread>-<task> .ana/worktrees/<task>` off the
workspace's current branch. The worker's adapter is built with that path as
its working directory, so its edits land there and nowhere else. When it
finishes, ANA commits the worktree and merges the branch back — **sequentially,
under one lock**, so two merges never race the index.

A conflict is **reported, not resolved**: the merge is aborted, the workspace
is left exactly as it was, the branch is kept as the only copy of that work,
and the task is marked failed with the branch name. Guessing at a merge is how
an orchestrator destroys work it cannot see.

Deliberate limits:

- Worktrees live **inside** the workspace (`.ana/worktrees/`), because the gate
  confines every tool call to the session workspace — a worktree outside it
  would make each worker's own edits look like an escape. `.ana/` goes into
  `.git/info/exclude`, not the user's `.gitignore`.
- A workspace that is not a git repository (or has no commits, or a detached
  HEAD) is **not** isolated: its workers share the directory as before, and
  the code says so by returning None rather than pretending.
- `ANA_WORKER_WORKTREES=off` restores the old behavior.

### The fix that makes it apply

New workspaces are now `git init`-ed with one empty commit
(`ANA_WORKSPACE_GIT_INIT=off` to disable). Without it, every workspace ANA
creates is a plain directory and the isolation above would never engage — which
is exactly what the first test showed. An **adopted** directory that is already
a repository is left alone: no commit, no branch change.

## 2. Verification: ANA checks, rather than believing the worker

**The problem.** "I fixed the test" and "the test passes" were the same
sentence to the system. The lead synthesized whatever the workers wrote.

**Now** a plan task may carry `verify`, a shell command
(`packages/orchestrator/verification.py`):

```json
{"title": "…", "agent": "codex", "prompt": "…", "verify": "pytest -q tests/test_x.py"}
```

ANA runs it itself — in that worker's worktree, as a plain subprocess, never
through an agent — after the worker reports done. A failure sends the task back
to a worker **once** (`ANA_MAX_TASK_RETRIES`, default 1) with the failure
output appended and labeled as a check ANA ran. A task whose check still fails
is `error`, however confidently the worker wrote its summary, and the synthesis
prompt states the verdict next to each result so the lead cannot round it up to
success.

`verify` is model-written text heading for a shell, so it is screened by the
**same YAML rule set as any agent command**. Only an `allow` verdict runs; an
`ask` verdict is refused too, because no human is watching this path.

## 3. Also in this batch

- **Per-agent token budgets** (`ANA_KIMI_FIVE_HOUR_TOKEN_BUDGET`,
  `ANA_KIMI_WEEKLY_TOKEN_BUDGET`, and the `CLAUDE_`/`CODEX_` equivalents).
  Claude and Codex report real utilization, so their budget only matters before
  the first turn; Kimi reports none, so its bar *is* that number and a shared
  default is a guess about the wrong plan. Kimi's real quota lives behind
  `GET <base>/usages` on its API, which is Cloudflare-gated against non-CLI
  clients — reachable only by replicating the CLI's own identity headers and
  token refresh, which would break the moment either changes.
- **The status bar keeps the reset schedule** after a window rolls over.
  Utilization becomes unknown again (nobody has run that CLI since), but the
  next reset is arithmetic from the last instant the provider stated.
- **An unreachable workspace path is a 422 with a reason**, not a bare 500.
  The server only sees what is mounted into it; `mkdir -p` on a root-owned
  parent fails, and the message now says so and names the root that works.

## Verification (live, in the running stack)

A real two-task fan-out into a fresh workspace:

```
PLAN [('one.txt 생성', 'codex', 'test -f one.txt'), ('two.txt 생성', 'codex', 'test -f two.txt')]
SUB t2 done verify=True merge=True
SUB t1 done verify=True merge=True
$ git log --oneline
a1502a2 Merge branch 'ana/worktree-probe2-t1'
dad4e88 ANA t1: one.txt 생성
a1b0335 Merge branch 'ana/worktree-probe2-t2'
0d10c13 ANA t2: two.txt 생성
80be3da ANA: workspace created
```

— two isolated checkouts, two verified tasks, two merges, no leftover branches,
clean status.

The run before it found a real bug: both merges failed with *"Committer
identity unknown"*, because the server container has no global git config and
only the commit carried `-c user.name/-c user.email`. A merge commit needs it
too, and the failure arrives as a non-zero exit that reads exactly like a
conflict. Both live runs are worth keeping in mind as the pattern here: the
turn *reported* the failure honestly (the lead said the files were not in
`main`), which is the verification work doing its job.

`504 passed`, including `test_worktrees.py` (10) and `test_verification.py`
(10).

## Follow-ups from running it (same day)

- **Planning is no longer a mode the user picks.** The composer had a
  Plan/Direct toggle; the lead has no tools at all, so "can this be answered
  without touching the machine" is a judgement about the request, not a
  preference — and the toggle let the user choose the mode that cannot do the
  job. Every turn now asks the lead, which answers `{"mode":"direct"}` or a
  plan of tasks. `ANA_PLANNER=off` remains for debugging. The planning call
  runs at the CLI's cheapest effort (`ANA_PLANNER_EFFORT`), since it is a
  short JSON judgement rather than the work.
- **The planner prompt now separates "small" from "needs no tools".** It used
  to say "direct unless splitting helps", which invited the lead to answer
  questions about files it cannot read. Direct is now for what it already
  knows; anything touching the machine is a plan, even a one-task one.
- **The lead was answering in JSON after the fan-out**, because its
  instructions tell it to return plans as JSON. The synthesis prompt now says
  the planning step is over and the answer is prose.
- **A read-only scope no longer refuses the shell outright.** The first
  read-only worker could not answer "how many files are here": listing a
  directory is a shell command. Shell tools are judged by their command now —
  an allowlist of `ls`, `cat`, `grep`, `find`, `git log/status/diff`, … with
  redirections, `sed -i` and anything unlisted still refused. The scope
  instructions say so too; the run *after* the allowlist landed still failed
  because the prompt was telling the worker its shell was blocked.

## Still open

- **No dependency graph.** Tasks are still one round of independent work; a
  task cannot wait for another's merge.
- **Verification is per task, not per turn.** Nothing re-checks the workspace
  after every branch has merged, so two individually-passing tasks can still
  leave a broken tree.
- **No rollback UI.** Every turn's work is now in git history, which is what a
  "revert this turn" command would need — but the command does not exist yet.
