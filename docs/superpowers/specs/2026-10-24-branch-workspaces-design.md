# Branch workspaces: feature branches under a project

Date: 2026-10-24
Status: approved, ready to plan

## Intent and why

ANA's unit of management is the workspace: one folder, one lead agent, one
ongoing conversation. That holds up for one line of work. It breaks the moment
the user wants to push two features forward at the same time, which is the
case that prompted this: *"여러 기능을 병렬로 작업해야 할 때 필요했었음."*

Today the only way to get a second parallel line of work is to register a
second workspace pointing at a second copy of the project. That loses the
relationship between the two (nothing says they are the same project), loses
the shared project identity (icon, system prompt, container settings are
re-entered), and leaves the user to reconcile the copies by hand.

The change: a project (what is today a workspace) can hold **feature
branches**. Each branch is a real git worktree on its own branch, with its own
lead agent and its own conversation. `main` keeps a lead agent too — it is the
project row itself. Parallel feature work becomes a first-class thing ANA
manages, rather than something the user improvises with duplicate workspaces.

## Goals

- A project can have N feature branches; each is an isolated checkout, so two
  branches' agents never touch the same file on disk.
- Every branch has its own lead agent and its own conversation, with the same
  capabilities a workspace's lead has today (system prompt, model routing,
  sub-agent fan-out, containers, governance gate).
- `main` has a lead agent as well, and it is the one the project row already
  has — opening a project opens main's conversation, exactly as before.
- Branch work returns to main through an explicit user action (a button), not
  automatically.
- When that merge conflicts, the work is never lost and never silently
  resolved: the merge is aborted and the branch's own lead agent is handed the
  conflict as a turn to work on.
- The sidebar shows the hierarchy: projects at the top level, their branches
  nested beneath, with a way to add a branch from the project row.

## Non-goals

- No cross-branch orchestration: main's lead agent does not plan, schedule, or
  dispatch work into branches. A branch is driven by its own lead, by the user.
- No PR/remote integration. Merging is local `git merge` into the project's
  checkout. Pushing and opening PRs stays the user's business.
- No rebase, squash, or cherry-pick options in v1. One merge semantic.
- No nesting beyond one level. A branch cannot have sub-branches.
- No automatic branch creation. ANA never decides on its own that a piece of
  work deserves a branch.
- No change to the existing per-task worker worktrees under `.ana/worktrees/`.

## Anti-goals (what makes this a failure even if it ships)

- **Silent conflict resolution.** An orchestrator that guesses at a merge
  destroys work it cannot see. This is already the stated principle in
  `packages/orchestrator/worktrees.py` and it carries over unchanged.
- **Losing a branch's commits.** Deleting a branch workspace must never be
  able to drop commits that are not in main; removing the checkout is fine,
  removing the git branch when it holds unmerged work is not.
- **A second concept.** If a branch ends up with its own table, its own id
  space, its own settings UI, and its own code paths in the graph, the change
  has failed: the point is that a branch *is* a workspace with a parent.
- **A sidebar that is harder to scan than the flat list it replaced.**

## Constraints (from the existing system)

These are facts about the codebase the design has to live with, not choices:

- `workspaces.id` **is** the LangGraph thread id. `call_lead_agent` in
  `apps/server/app/graph.py` does `get_workspace(pool, thread_id)`. Anything
  that wants its own lead agent and its own conversation must therefore be a
  row in `workspaces` with its own id.
- The web app routes conversations at `/[session]`, where the segment is the
  workspace id. `RESERVED_WORKSPACE_IDS` in
  `packages/orchestrator/workspaces.py` lists the top-level paths a workspace
  id may not collide with.
- The governance gate confines every tool call to the session's workspace
  directory (`packages/governance/workspace.py`). A branch's checkout must
  therefore live *inside* the project folder, or every edit a branch agent
  makes would read as an escape attempt.
- `.ana/` is already added to `.git/info/exclude` by
  `worktrees.py::_exclude_worktrees`, and `.ana/worktrees/` is already used
  for per-task worker checkouts. Branch checkouts need a sibling directory,
  not that one.
- `create_workspace` already runs `git init -b main` plus one empty commit for
  a newly created folder (`_ensure_git`), so new projects are git repositories
  and can host worktrees. An adopted folder that is not a repository cannot.
- Merges already run under one `asyncio.Lock` per workspace path
  (`worktrees.merge_lock_for`). Branch merges must take the same lock as the
  project's worker merges, keyed on the project path, or two merges race the
  index.

## The design

### A branch is a workspace with a parent

Add three columns to `workspaces`:

- `parent_id TEXT` — NULL for a project, the project's id for a branch.
  Referential cleanup is the application's job (see deletion below).
- `branch TEXT NOT NULL DEFAULT ''` — the git branch name this row works on.
  Empty for rows that predate the change and for projects whose branch is just
  "whatever the checkout is on".
- `base_branch TEXT NOT NULL DEFAULT ''` — for a branch, the project branch it
  was cut from and the only branch it will merge back into. Recorded rather
  than re-read at merge time: `git merge` in the project targets whatever HEAD
  happens to be on, and a user who switched the project checkout to another
  branch should get a refusal, not a merge into the wrong place.

Everything else a branch needs — lead agent, conversation, turn history,
system prompt, icon, archive flag, container settings, gate confinement — it
gets by being a `workspaces` row. No new table, no new id space, no new code
path in the graph.

A project row is unchanged in meaning: `parent_id IS NULL`, `path` is the
project folder, and opening it opens main's conversation with main's lead
agent. **Nothing about existing workspaces changes**; they are projects with
no branches.

### Physical layout

```
myproject/                        project row, path = here, main's lead agent
  .ana/
    branches/
      auth/                       branch row, path = here, auth's lead agent
        .ana/worktrees/...        auth's own per-task worker checkouts
      payments/                   branch row, payments' lead agent
    worktrees/...                 main's per-task worker checkouts
```

- Checkout: `<project>/.ana/branches/<slug>`, created with
  `git worktree add -B <branch> <path> HEAD` from the project folder.
- Git branch name: `ana/branch/<slug>`. Distinct from the `ana/` prefix the
  per-task worker branches use, so a branch worktree is never mistaken for a
  disposable one by anything that lists branches.
- Base: the project checkout's current branch at the time the branch is
  created, recorded on the row so the merge target is not re-guessed later.
- Lifetime: until the user deletes it. This is the difference from
  `.ana/worktrees/`, whose entries are created and removed inside one turn.

The nesting is deliberate: it keeps a branch's files inside the project folder
so the gate's confinement check passes unchanged, and it keeps a branch's own
sub-agent workers isolated under that branch rather than under the project.

### Branch id and name

A branch row's id is derived from the project id and the branch name
(`<project-id>--<slug>`, de-duplicated by the existing `_unique_id`), so a
branch is addressable at `/[session]` like any workspace and the id reads as
what it is. It is subject to the same reserved-id and slug rules.

The **slug is de-duplicated too**, not only the id: it is the checkout's
folder name and the git branch's name, so a taken slug has to become
`<slug>-2` or the second branch is refused for a name its user never used.
And because `slugify` keeps only `[a-z0-9._-]`, a Korean name — which is what
this UI is written in — survives as nothing at all, so a name with no ASCII
in it slugs to `branch-<8 hex of the name>`. Without that, every branch of a
project contends for one slug and a project can hold exactly one.

### Creating a branch

From the project row in the sidebar. Inputs: a name (and optionally an icon).
The server:

1. Refuses if the project is not a git repository, or has no commit to branch
   from, or is on a detached HEAD — with the reason, so the UI can say why.
2. Creates the worktree and the git branch.
3. Inserts the `workspaces` row with `parent_id`, `branch`, the base branch,
   and `path` set to the checkout.

Settings a branch starts with: it inherits the project's **system prompt**, as
a copy it can then diverge from. A copy rather than a live inheritance — a
branch whose settings silently change when the project's do is harder to
reason about than one that was seeded.

It does **not** inherit container settings, and starts with
`container_enabled = false`. A per-workspace container mounts only its own
workspace path (`containers.run_args`), and a branch checkout's `.git` is a
file pointing at `<project>/.git/worktrees/<slug>` — outside that mount, so
every git command inside a branch container would fail. Copied port mappings
would also collide with the project's own container, which publishes the same
host ports. Turning a container on for a branch stays possible and stays the
user's call; seeding one that cannot work is not.

### Merging a branch into main

An explicit **"main으로 머지"** action on the branch. The server takes the
branch's merge lock and then the project's (`worktrees.merge_lock_for` on each
path, always in that order — the branch's own sub-agent workers merge into its
tree under the branch lock, and nothing anywhere takes the project lock first,
so the fixed order cannot deadlock). Then:

1. Refuse when the project checkout is no longer on the branch's recorded
   `base_branch` — the user moved it, and merging into wherever it now sits is
   a guess (`status: "moved"`).
1. Refuse when the *branch* checkout still has unresolved conflicts from an
   earlier attempt (`status: "unresolved"`). This one is load-bearing:
   committing a mid-merge tree runs `git add -A`, which marks a conflicted
   file resolved **with its markers still in it**, and the merge that follows
   would put `<<<<<<<` into main. The one thing that moves this forward is the
   branch's agent finishing in its own conversation.
2. Refuse when the project checkout has uncommitted changes
   (`status: "dirty"`), **unless** the caller passes `commit_project`, in
   which case those changes are committed first with an ANA message. This is
   not a formality: main's lead agent edits the project folder directly and
   ANA never commits for it, so the project tree's normal state is dirty and a
   plain refusal would reject almost every real merge. Committing someone's
   half-finished work in their own repository without asking is the other
   wrong answer, so the UI shows the refusal with a "커밋하고 머지" action
   beside it.
3. Commit whatever is uncommitted in the branch checkout, as the existing
   `worktrees.commit` does. Nothing to merge → say so (`status: "empty"`).
4. `git merge --no-edit --no-ff <branch>` in the project checkout.
5. On success: report it. The branch row, its checkout, and its conversation
   all stay — merging is not deleting. The user removes the branch when done
   with it.
6. On a failure that is **not** a conflict — a git timeout, a hook, a stale
   `index.lock` — abort and report it as `status: "failed"`, touching nothing
   in the branch. Calling every non-zero exit a conflict sent the branch's
   agent looking for markers that were never there.
7. On conflict: `git merge --abort` in the project, so main is never left in a
   conflicted state, and then **reproduce the conflict inside the branch
   checkout** by running `git merge <base_branch>` there and leaving it
   conflicted. The conflict is then handed to the branch's own lead agent as a
   turn naming the conflicting paths. The user watches it resolve in the
   branch's conversation and clicks merge again; the branch now contains main,
   so the second merge goes through.

Step 7 is the one place the merge touches the branch checkout, and it is
deliberate. The branch's lead agent is confined by the gate to its own
checkout — it cannot read the project folder, which is correct — so a conflict
described to it in prose, with `--abort` having erased every marker, is a task
it has no way to carry out. Markers in files it owns are the only form of the
problem it can act on. Main is never the tree left in conflict.

### Deleting a branch

Two outcomes the UI must distinguish:

- Branch is fully merged into main → remove the checkout, delete the git
  branch, delete the row (the conversation goes with it, as deleting a
  workspace does today).
- Branch has commits not in main → removing it would destroy the only copy.
  Refuse by default and say so; the user can archive it instead (the row and
  conversation stay, out of the main list), or confirm an explicit
  force-delete.
- The comparison **cannot be made** — `rev-list <base>..<branch>` fails
  because the base branch was renamed or deleted → also a refusal. "Cannot
  tell" is not permission: answering zero there let an unforced delete drop
  commits that existed nowhere else.

Deleting the **project** takes its branches with it, force-deleted: leaving
the rows behind would orphan conversations that are unreachable from a sidebar
with no project to nest them under, and leaving the checkouts behind would
leave worktrees registered against a repository ANA no longer tracks. The
unmerged-commits refusal does not apply here — the user is deleting the whole
project, and the branches are part of what they are deleting.

Archiving a project needs no cascade: branches render inside their project's
group, so they go with it.

A branch row whose checkout is gone — the user ran `git worktree remove`, or
moved the project — is reported at the operation that needs it. Merge and
delete return a reason naming the missing path. The listing does not check:
it is one SQL statement, and stat-ing every branch on every sidebar refresh
would put disk IO on every page render.

### Sidebar

- Top level: projects, as today. A project with branches gets a disclosure
  control; branches render as indented rows beneath it.
- The project row **is** main. Clicking it opens main's conversation, which is
  exactly what clicking a workspace does today — no "main" child row. *(My
  call, not the user's: a `main` child row would be symmetric, but it adds a
  row to every project and makes the project row click-dead, changing
  behaviour everyone is already used to. Flag it as a thing to revisit if the
  hierarchy reads ambiguously in practice.)*
- The project row gains a "+ branch" action, disabled with a reason when the
  project cannot host branches.
- Branch rows carry the same affordances workspace rows have (activity,
  rename, archive, settings) plus merge and delete.
- Archived branches follow the existing archive behaviour; the existing
  archive group stays for projects.
- The existing filter searches branches too, and a matching branch shows its
  project for context.

### Governance and gate

No change to the gate's rule. A branch's session workspace is its checkout
directory, and everything the branch's agents do happens inside it. The
project folder is *outside* a branch's confinement, which is correct: a branch
agent reaching into the project checkout is the escape the gate is for. The
merge is server-side plumbing, not a tool call, so it is not subject to it.

The asymmetry is known and left alone: because branches nest inside the
project folder, main's lead agent's own confinement *contains* every branch
checkout, so it can write there. That is already true of `.ana/worktrees/`,
where live sub-agent checkouts sit today, so this adds no new class of reach;
carving a hole in the gate is a change with its own blast radius and does not
belong in this feature.

## What is left to the builder

- Database migration style: follow the existing `ALTER TABLE ... ADD COLUMN IF
  NOT EXISTS` pattern in `WORKSPACE_MIGRATIONS`.
- Whether the branch-worktree plumbing extends
  `packages/orchestrator/worktrees.py` or gets a sibling module. The two have
  different lifetimes and different branch prefixes but share all the git
  plumbing; the builder picks whichever keeps the shared `_git` helper in one
  place.
- API shape for the new endpoints (create branch / merge / delete), following
  the existing `/workspaces` conventions in `apps/server/app/main.py`.
- Exactly how the conflict turn is injected into the branch's conversation —
  reuse whatever path the scheduler or an inbound message already uses rather
  than inventing a new entry point.
- Whether listing returns a flat list with `parent_id` (and the web groups it)
  or a nested structure. Flat matches the current endpoint; the builder may
  prefer it for that reason.
- Test coverage: the existing suite has fixtures for workspaces and for
  worktrees; extend those rather than standing up new ones.
