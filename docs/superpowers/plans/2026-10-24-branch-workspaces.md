# Branch Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a project hold feature branches — each a long-lived git worktree with its own lead agent and conversation — and merge them back into main on an explicit user action.

**Architecture:** A branch is a `workspaces` row with `parent_id` and `branch` set; it therefore reuses the whole workspace machinery (lead agent, thread, prompt, icon, archive, container, gate confinement) with no new code path in the graph. Branch checkouts are git worktrees at `<project>/.ana/branches/<slug>`, created by a new `packages/orchestrator/branches.py` built on `worktrees.py`'s existing `_git` helper. The sidebar groups the flat `/workspaces` listing by `parent_id`.

**Tech Stack:** Python 3.13 (FastAPI, psycopg/Postgres, asyncio subprocess git), TypeScript (Next.js, React, shadcn sidebar primitives), pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-10-24-branch-workspaces-design.md`

## Global Constraints

- A branch MUST be a row in `workspaces`. No new table, no new id space. (`workspaces.id` is the LangGraph thread id; `call_lead_agent` does `get_workspace(pool, thread_id)`.)
- Branch checkout path: `<project>/.ana/branches/<slug>`. Inside the project folder, or the governance gate reads every branch edit as an escape.
- Branch git name: `ana/branch/<slug>`. Distinct from `worktrees.BRANCH_PREFIX` (`ana`), whose branches are per-task and disposable.
- Branch merges take `worktrees.merge_lock_for(<branch path>)` and then `worktrees.merge_lock_for(<project path>)`, always in that order.
- A merge conflict runs `git merge --abort` in the project and is NEVER resolved by the merge code. It is reproduced in the branch checkout (`git merge <base_branch>` there, left conflicted) so the branch's lead agent has markers it is allowed to read.
- The merge targets the branch's recorded `base_branch`, and refuses when the project checkout is on a different branch.
- A branch inherits the project's `system_prompt` only. Never its container settings; `container_enabled` starts false.
- Deleting a branch with commits not in main is refused by default.
- Migrations are `ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS ...` appended to `WORKSPACE_MIGRATIONS`.
- Existing workspaces keep working unchanged: `parent_id IS NULL`, `branch = ''`.
- User-facing strings are Korean, matching the existing UI and error text.
- One level only: a branch (`parent_id IS NOT NULL`) cannot have branches.

## Review Focus

Inputs the spec implies but does not pin, most likely to bite first:

1. **Project is not a git repository** (an adopted folder) — branch creation must refuse with a readable reason, not a 500. Covered by Task 2 (`test_branch_refused_without_git`) and Task 4 (`test_create_branch_on_non_git_project_is_422`).
2. **Project checkout has uncommitted changes at merge time** — main's lead agent edits the project folder directly and ANA never commits for it, so this is the *normal* state, not an edge case. Refuse with `status: "dirty"`, and commit the project tree first when the caller asks for it. Covered by Task 2 (`test_merge_refused_when_project_is_dirty`, `test_commit_project_lets_a_dirty_project_merge`).
3. **Branch name collides with an existing branch of the same project** — the id must not be reused and the git branch must not be clobbered (`-B` would reset someone's checkout). Covered by Task 2 (`test_two_branches_with_the_same_name_do_not_collide`).
4. **Deleting a branch that holds unmerged commits** — the only copy of that work. Refuse unless forced. Covered by Task 3 (`test_delete_refused_when_branch_has_unmerged_commits`).
5. **Deleting a project that still has branches** — orphan rows pointing at checkouts that are about to be unreachable. Deleting a project deletes its branch rows and removes their checkouts. Covered by Task 4 (`test_deleting_a_project_removes_its_branches`).

---

### Task 1: `parent_id`, `branch` and `base_branch` on the workspaces table

**Files:**
- Modify: `packages/orchestrator/workspaces.py` (`WORKSPACE_MIGRATIONS`, `Workspace`, `_row_to_workspace`, `get_workspace`, `list_workspaces`)
- Test: `apps/server/tests/test_workspaces.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Workspace` dataclass gains `parent_id: Optional[str] = None`, `branch: str = ""` and `base_branch: str = ""`, all present in `as_dict()`.
  - `list_workspaces` rows gain `"parent_id"`, `"branch"` and `"base_branch"` keys.
  - `async def create_workspace(pool, *, name, path=None, parent_id=None, branch="", base_branch="", workspace_id=None) -> Workspace` — the new keyword arguments default to today's behaviour; `workspace_id` lets a caller supply the id base instead of slugifying the display name.
  - `async def list_branches(pool, parent_id: str) -> list[Workspace]` — the project's branch rows, ordered by `lower(name)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_plain_workspace_has_no_parent_and_no_branch(workspace_pool):
    workspace = await create_workspace(workspace_pool, name="proj")
    assert workspace.parent_id is None
    assert workspace.branch == ""
    assert (await get_workspace(workspace_pool, workspace.id)).parent_id is None


async def test_a_branch_row_records_its_parent_and_git_branch(workspace_pool, tmp_path):
    project = await create_workspace(workspace_pool, name="proj", path=str(tmp_path / "proj"))
    child = await create_workspace(
        workspace_pool,
        name="auth",
        path=str(tmp_path / "proj" / ".ana" / "branches" / "auth"),
        parent_id=project.id,
        branch="ana/branch/auth",
        base_branch="main",
    )
    stored = await get_workspace(workspace_pool, child.id)
    assert stored.parent_id == project.id
    assert stored.branch == "ana/branch/auth"
    assert stored.base_branch == "main"
    assert stored.as_dict()["parent_id"] == project.id


async def test_listing_carries_parent_and_branch(workspace_pool, tmp_path):
    project = await create_workspace(workspace_pool, name="proj", path=str(tmp_path / "proj"))
    await create_workspace(
        workspace_pool, name="auth", path=str(tmp_path / "proj" / "x"),
        parent_id=project.id, branch="ana/branch/auth",
    )
    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}
    assert rows[project.id]["parent_id"] is None
    assert [r for r in rows.values() if r["parent_id"] == project.id][0]["branch"] == "ana/branch/auth"


async def test_list_branches_returns_only_this_projects_branches(workspace_pool, tmp_path):
    one = await create_workspace(workspace_pool, name="one", path=str(tmp_path / "one"))
    two = await create_workspace(workspace_pool, name="two", path=str(tmp_path / "two"))
    await create_workspace(workspace_pool, name="a", path=str(tmp_path / "one" / "a"),
                           parent_id=one.id, branch="ana/branch/a")
    await create_workspace(workspace_pool, name="b", path=str(tmp_path / "two" / "b"),
                           parent_id=two.id, branch="ana/branch/b")
    assert [w.name for w in await list_branches(workspace_pool, one.id)] == ["a"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `scripts/test.sh apps/server/tests/test_workspaces.py -k "parent or branch" -v`
Expected: FAIL — `create_workspace()` got an unexpected keyword argument `parent_id`.

- [ ] **Step 3: Add the columns and plumb them through**

In `WORKSPACE_MIGRATIONS`, append three `ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS` statements: `parent_id TEXT`, `branch TEXT NOT NULL DEFAULT ''` and `base_branch TEXT NOT NULL DEFAULT ''`, each with a comment saying what a branch row is, in the style of the existing comments there. Add the fields to `Workspace` and `as_dict()`, to `_row_to_workspace`, and to the `SELECT` lists and `GROUP BY` in `get_workspace` and `list_workspaces`. Give `create_workspace` the new keyword arguments and write them in the `INSERT`; `workspace_id` replaces `slugify(name)` as the base passed to `_unique_id`.

`create_workspace` currently calls `_ensure_git(resolved)` on every new workspace. A branch checkout is already inside a repository, so skip that call when `parent_id` is set — `_ensure_git` would return False anyway, but the call runs git twice per branch for nothing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/test.sh apps/server/tests/test_workspaces.py -v`
Expected: PASS (the whole file, so existing workspace behaviour is confirmed unchanged).

- [ ] **Step 5: Commit**

```bash
git add packages/orchestrator/workspaces.py apps/server/tests/test_workspaces.py
git commit -m "feat(workspaces): record a workspace's parent project and git branch"
```

---

### Task 2: Branch worktree plumbing

**Files:**
- Create: `packages/orchestrator/branches.py`
- Modify: `packages/orchestrator/worktrees.py` (export `_git` as `git`, keeping `_git` as an alias so existing callers are untouched)
- Test: `apps/server/tests/test_branches.py`

**Interfaces:**
- Consumes: `worktrees.git`, `worktrees.IDENTITY`, `worktrees.is_git_workspace`, `worktrees.current_branch`, `worktrees._exclude_worktrees`, `worktrees.merge_lock_for`.
- Produces, in `packages/orchestrator/branches.py`:
  - `BRANCHES_DIRNAME = os.path.join(".ana", "branches")`
  - `BRANCH_PREFIX = "ana/branch"`
  - `@dataclass(frozen=True) class BranchCheckout: path: str; branch: str; base_branch: str; project: str`
  - `class BranchError(RuntimeError)` — carries a Korean message the API hands to the user.
  - `def slug_for(name: str) -> str` — reuses `workspaces.slugify` with fallback `"branch"`.
  - `async def can_branch(project_path: str) -> str` — `""` when the project can host branches, otherwise the Korean reason it cannot.
  - `async def create(project_path: str, slug: str) -> BranchCheckout` — raises `BranchError`.
  - `async def merge_into_project(checkout: BranchCheckout, message: str, *, commit_project: bool = False) -> MergeOutcome` where `@dataclass(frozen=True) class MergeOutcome: status: str; detail: str = ""; paths: tuple[str, ...] = ()` and `status` is one of `"merged" | "empty" | "dirty" | "moved" | "conflict" | "missing"`.
  - `async def unmerged_commits(checkout: BranchCheckout) -> int`
  - `async def remove(checkout: BranchCheckout, *, force: bool = False) -> None` — raises `BranchError` when `unmerged_commits` > 0 and not `force`.

- [ ] **Step 1: Write the failing tests**

Reuse the `repo` fixture from `apps/server/tests/test_worktrees.py` verbatim (a `tmp_path` repo on `main` with one commit and `a.txt`).

```python
async def test_branch_gets_its_own_checkout_inside_the_project(repo):
    checkout = await branches.create(str(repo), "auth")
    assert Path(checkout.path) == repo / ".ana" / "branches" / "auth"
    assert checkout.branch == "ana/branch/auth"
    assert checkout.base_branch == "main"
    (Path(checkout.path) / "a.txt").write_text("from auth\n", encoding="utf-8")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"


async def test_two_branches_work_in_parallel_without_touching_each_other(repo):
    auth = await branches.create(str(repo), "auth")
    pay = await branches.create(str(repo), "payments")
    (Path(auth.path) / "a.txt").write_text("auth\n", encoding="utf-8")
    assert (Path(pay.path) / "a.txt").read_text(encoding="utf-8") == "base\n"


async def test_two_branches_with_the_same_name_do_not_collide(repo):
    first = await branches.create(str(repo), "auth")
    (Path(first.path) / "a.txt").write_text("first\n", encoding="utf-8")
    with pytest.raises(branches.BranchError):
        await branches.create(str(repo), "auth")
    assert (Path(first.path) / "a.txt").read_text(encoding="utf-8") == "first\n"


async def test_branch_refused_without_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await branches.can_branch(str(plain)) != ""
    with pytest.raises(branches.BranchError):
        await branches.create(str(plain), "auth")


async def test_merge_brings_the_branch_work_into_the_project(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    outcome = await branches.merge_into_project(checkout, "auth 작업")
    assert outcome.status == "merged"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "new\n"


async def test_merge_of_an_untouched_branch_says_there_was_nothing(repo):
    checkout = await branches.create(str(repo), "auth")
    outcome = await branches.merge_into_project(checkout, "auth 작업")
    assert outcome.status == "empty"


async def test_merge_refused_when_project_is_dirty(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "dirty"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert not (repo / "b.txt").exists()


async def test_commit_project_lets_a_dirty_project_merge(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")

    outcome = await branches.merge_into_project(checkout, "auth 작업", commit_project=True)

    assert outcome.status == "merged"
    # The project's own work was committed, not discarded.
    assert (repo / "a.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "new\n"


async def test_merge_refused_when_the_project_moved_off_the_base_branch(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("checkout", "-b", "elsewhere", cwd=repo)

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "moved"
    assert "main" in outcome.detail
    assert not (repo / "b.txt").exists()


async def test_conflict_aborts_in_the_project_and_reappears_in_the_branch(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (repo / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=repo)

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "conflict"
    assert outcome.paths == ("a.txt",)
    # Main is never left in a conflicted state.
    assert (repo / "a.txt").read_text(encoding="utf-8") == "from main\n"
    assert "MERGE_HEAD" not in _git("status", cwd=repo)
    # The branch checkout holds the conflict, with markers its own agent can read.
    branch_text = (Path(checkout.path) / "a.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" in branch_text and "from main" in branch_text


async def test_a_resolved_conflict_merges_on_the_second_attempt(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (repo / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=repo)
    assert (await branches.merge_into_project(checkout, "auth 작업")).status == "conflict"

    # What the branch's lead agent does in its own checkout.
    (Path(checkout.path) / "a.txt").write_text("resolved\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(checkout.path))
    _git("commit", "-m", "resolve", cwd=Path(checkout.path))

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "merged"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "resolved\n"


async def test_delete_refused_when_branch_has_unmerged_commits(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(checkout.path))
    _git("commit", "-m", "work", cwd=Path(checkout.path))

    assert await branches.unmerged_commits(checkout) == 1
    with pytest.raises(branches.BranchError):
        await branches.remove(checkout)
    assert Path(checkout.path).exists()

    await branches.remove(checkout, force=True)
    assert not Path(checkout.path).exists()


async def test_delete_after_merge_removes_checkout_and_branch(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    await branches.merge_into_project(checkout, "auth 작업")

    await branches.remove(checkout)

    assert not Path(checkout.path).exists()
    assert "ana/branch/auth" not in _git("branch", "--list", cwd=repo)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `scripts/test.sh apps/server/tests/test_branches.py -v`
Expected: FAIL — `ModuleNotFoundError: packages.orchestrator.branches`.

- [ ] **Step 3: Implement `packages/orchestrator/branches.py`**

Give the module a docstring in the style of `worktrees.py`: say what a branch checkout is, how it differs from a per-task worker worktree (lifetime, prefix, who owns it), and why it lives inside the project.

Specific decisions the tests do not fix:

- `can_branch` checks, in order: git on PATH, `is_git_workspace`, `rev-parse --verify HEAD` (a repository with no commit has nothing to branch from), `current_branch` is not None (detached HEAD). Each returns its own Korean sentence.
- `create` calls `can_branch` first and raises `BranchError` with that message; then refuses when `<project>/.ana/branches/<slug>` exists or `git rev-parse --verify ana/branch/<slug>` succeeds — the collision case. Use `git worktree add -b` (not `-B`), so git itself refuses to clobber an existing branch. Call `worktrees._exclude_worktrees(project_path)` before adding, as `worktrees.create` does.
- `merge_into_project` takes `worktrees.merge_lock_for(checkout.path)` and then `worktrees.merge_lock_for(checkout.project)` — that order, always, because the branch's own workers hold only the branch lock and nothing takes the project lock first. Inside, in order:
  1. `"missing"` when the checkout directory is gone.
  2. `"moved"` when `worktrees.current_branch(project)` is not `checkout.base_branch`; `detail` names both.
  3. `git status --porcelain` in the project: non-empty → commit it with `worktrees.IDENTITY` when `commit_project`, otherwise `"dirty"` with the porcelain output as `detail`.
  4. Commit the branch checkout's working tree (`worktrees.commit`); nothing committed and nothing to merge → `"empty"`.
  5. `git merge --no-edit --no-ff <branch>` in the project → `"merged"`.
  6. On failure: `git merge --abort` in the project, then `git merge --no-edit <base_branch>` inside the branch checkout, left conflicted. `paths` comes from `git diff --name-only --diff-filter=U` in the branch checkout; `detail` is the project merge's output, last 2000 characters.
- The step-4 "nothing to merge" check is `git rev-list --count <base_branch>..<branch>` being 0 *and* `worktrees.commit` having committed nothing — a branch that was committed in by its own agent has something to merge even when its working tree is clean.
- `unmerged_commits` is `git rev-list --count <base_branch>..<branch>` in the project.
- `remove` checks `unmerged_commits` unless `force`, then `git worktree remove --force`, `git branch -D`, `git worktree prune`, and an `shutil.rmtree` fallback, as `worktrees.remove` does.

In `worktrees.py`, rename `_git` to `git` and add `_git = git` beneath it with a one-line comment, so the existing call sites and this module share one implementation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/test.sh apps/server/tests/test_branches.py apps/server/tests/test_worktrees.py -v`
Expected: PASS — both files, so the rename did not break the per-task worktrees.

- [ ] **Step 5: Commit**

```bash
git add packages/orchestrator/branches.py packages/orchestrator/worktrees.py apps/server/tests/test_branches.py
git commit -m "feat(branches): long-lived per-branch worktrees with explicit merge-back"
```

---

### Task 3: Branch lifecycle over the workspace store

**Files:**
- Create: `packages/orchestrator/branch_store.py`
- Test: `apps/server/tests/test_branch_store.py`

**Interfaces:**
- Consumes: Task 1's `create_workspace(..., parent_id=, branch=)`, `list_branches`, `get_workspace`, `forget_workspace`; Task 2's `branches` module.
- Produces:
  - `async def create_branch(pool, *, project_id: str, name: str, icon: str = "") -> Workspace` — raises `BranchError` (project missing, project is itself a branch, project cannot host branches, name collides).
  - `async def merge_branch(pool, branch_id: str, *, commit_project: bool = False) -> MergeOutcome` — raises `BranchError` when the id is not a branch.
  - `async def delete_branch(pool, branch_id: str, *, force: bool = False) -> None`
  - `async def delete_project_branches(pool, project_id: str) -> None` — removes every branch row of the project and its checkout; never raises for a checkout that is already gone.
  - `def checkout_for(project: Workspace, branch_row: Workspace) -> BranchCheckout`

- [ ] **Step 1: Write the failing tests**

These need both the database and a real git repository, so the fixture is Task 1's `workspace_pool` plus Task 2's `repo`.

```python
async def test_create_branch_registers_a_child_workspace(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    branch = await create_branch(workspace_pool, project_id=project.id, name="인증")

    assert branch.parent_id == project.id
    assert branch.branch.startswith("ana/branch/")
    assert Path(branch.path).is_dir()
    assert Path(branch.path).is_relative_to(repo / ".ana" / "branches")
    assert [w.id for w in await list_branches(workspace_pool, project.id)] == [branch.id]


async def test_branch_inherits_the_prompt_but_not_the_container(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    await set_workspace_system_prompt(workspace_pool, project.id, "한국어로 답해라")
    await set_container_settings(
        workspace_pool, project.id, enabled=True, image="python:3.13",
        ports=[{"host": 8080, "container": 80}],
    )

    branch = await create_branch(workspace_pool, project_id=project.id, name="auth")

    assert branch.system_prompt == "한국어로 답해라"
    # A branch checkout's .git points outside a per-workspace container's one
    # mount, and copied ports would collide with the project's own container.
    assert branch.container_enabled is False
    assert branch.container_ports == ()


async def test_a_branch_cannot_have_branches(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
    with pytest.raises(BranchError):
        await create_branch(workspace_pool, project_id=branch.id, name="deeper")


async def test_merge_branch_moves_the_work_into_the_project(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
    (Path(branch.path) / "b.txt").write_text("new\n", encoding="utf-8")

    outcome = await merge_branch(workspace_pool, branch.id)

    assert outcome.status == "merged"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "new\n"
    # Merging is not deleting: the branch and its conversation stay.
    assert await get_workspace(workspace_pool, branch.id) is not None


async def test_delete_branch_removes_the_row_and_the_checkout(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
    path = Path(branch.path)

    await delete_branch(workspace_pool, branch.id)

    assert await get_workspace(workspace_pool, branch.id) is None
    assert not path.exists()


async def test_deleting_a_project_removes_its_branches(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))
    branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
    path = Path(branch.path)

    await delete_project_branches(workspace_pool, project.id)

    assert await get_workspace(workspace_pool, branch.id) is None
    assert not path.exists()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `scripts/test.sh apps/server/tests/test_branch_store.py -v`
Expected: FAIL — `ModuleNotFoundError: packages.orchestrator.branch_store`.

- [ ] **Step 3: Implement `packages/orchestrator/branch_store.py`**

The module is the join between the git plumbing and the workspace rows; keep the git calls in `branches.py` and the SQL in `workspaces.py`.

- `create_branch`: load the project, refuse when it is missing or `parent_id is not None`; `slug = branches.slug_for(name)`; `checkout = await branches.create(project.path, slug)`; then `create_workspace(pool, name=name, path=checkout.path, parent_id=project.id, branch=checkout.branch, base_branch=checkout.base_branch, workspace_id=f"{project.id}--{slug}")`. Copy the project's `system_prompt` and the given `icon` onto the new row; copy nothing else. If the row insert fails, `branches.remove(checkout, force=True)` before re-raising, so a failed create leaves no orphan checkout.
- `merge_branch`: load the branch row and its project, build the `BranchCheckout` with `checkout_for`, call `branches.merge_into_project` with a message naming the branch and the caller's `commit_project`.
- `delete_branch`: `branches.remove(...)` then `forget_workspace(pool, branch_id)` (purge, so the conversation goes too).
- `delete_project_branches`: `list_branches`, then `delete_branch(..., force=True)` for each, swallowing a missing checkout.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/test.sh apps/server/tests/test_branch_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/orchestrator/branch_store.py apps/server/tests/test_branch_store.py
git commit -m "feat(branches): register a branch as a child workspace with its own lead agent"
```

---

### Task 4: HTTP endpoints, and the conflict handed to the branch's lead

**Files:**
- Modify: `apps/server/app/main.py` (new endpoints; `delete_workspace`)
- Test: `apps/server/tests/test_branch_api.py`

**Interfaces:**
- Consumes: Task 3's `create_branch`, `merge_branch`, `delete_branch`, `delete_project_branches`; `apps.server.app.websocket.ensure_runner`.
- Produces:
  - `POST /workspaces/{project_id}/branches` body `{name: str, icon: str | None}` → the new workspace dict, 422 with `detail` on `BranchError`, 404 when the project is unknown.
  - `POST /workspaces/{branch_id}/merge` body `{commit_project: bool = false}` → `{"status": "merged" | "empty" | "dirty" | "moved" | "conflict" | "missing", "detail": str, "paths": list[str]}`.
  - `DELETE /workspaces/{branch_id}?force=true` → reuses the existing delete path for branches.
  - `GET /workspaces/{workspace_id}/branch-capability` → `{"can_branch": bool, "reason": str}`.

- [ ] **Step 1: Write the failing tests**

Follow `test_workspaces.py`'s `TestClient(app)` style.

```python
def test_create_branch_endpoint_returns_the_child_workspace(client, project_in_repo):
    response = client.post(f"/workspaces/{project_in_repo}/branches", json={"name": "auth"})
    assert response.status_code == 200
    assert response.json()["parent_id"] == project_in_repo


def test_create_branch_on_non_git_project_is_422(client, plain_project):
    response = client.post(f"/workspaces/{plain_project}/branches", json={"name": "auth"})
    assert response.status_code == 422
    assert response.json()["detail"]


def test_branch_capability_says_why_a_plain_folder_cannot_branch(client, plain_project):
    body = client.get(f"/workspaces/{plain_project}/branch-capability").json()
    assert body["can_branch"] is False and body["reason"]


def test_merge_endpoint_reports_a_conflict_without_merging(client, project_in_repo, conflicting_branch):
    body = client.post(f"/workspaces/{conflicting_branch}/merge").json()
    assert body["status"] == "conflict"
    assert body["paths"] == ["a.txt"]


def test_conflict_is_submitted_to_the_branchs_own_lead(client, conflicting_branch, submitted):
    client.post(f"/workspaces/{conflicting_branch}/merge")
    assert [workspace for workspace, _ in submitted] == [conflicting_branch]
    assert "a.txt" in submitted[0][1]


def test_a_dirty_project_merges_only_when_asked(client, dirty_project_branch):
    first = client.post(f"/workspaces/{dirty_project_branch}/merge").json()
    assert first["status"] == "dirty"
    second = client.post(
        f"/workspaces/{dirty_project_branch}/merge", json={"commit_project": True}
    ).json()
    assert second["status"] == "merged"


def test_deleting_a_project_removes_its_branches(client, project_in_repo, one_branch):
    client.delete(f"/workspaces/{project_in_repo}")
    assert client.get("/workspaces").json()["workspaces"] == []
```

`submitted` is a fixture that monkeypatches `apps.server.app.websocket.ensure_runner` to return a stub whose `submit(prompt, agent, auto)` appends `(workspace_id, prompt)` to a list — the same shape `test_schedules.py` already uses for the scheduler.

- [ ] **Step 2: Run them to verify they fail**

Run: `scripts/test.sh apps/server/tests/test_branch_api.py -v`
Expected: FAIL — 404/405 from FastAPI, the routes do not exist.

- [ ] **Step 3: Implement the endpoints**

Put them next to the existing `/workspaces` routes and follow their conventions (`BaseModel` bodies, `HTTPException(422, detail=str(exc))` for a user-fixable problem).

On a conflict, the endpoint imports `ensure_runner` inside the function — as `scheduler.py` does, and for the same reason — and submits a Korean prompt to the *branch's* conversation. The prompt names the base branch and the conflicting paths, says the conflict markers are already in the branch's own checkout (step 6 of `merge_into_project` put them there), and asks the lead to resolve them, commit, and say so. Submit only when the runner is not `busy`; when it is, return the conflict anyway with a `detail` saying the conversation is busy, so the merge never silently does nothing.

In `delete_workspace`, call `delete_project_branches` before `forget_workspace` so a project's branches do not outlive it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/test.sh apps/server/tests/test_branch_api.py apps/server/tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/server/app/main.py apps/server/tests/test_branch_api.py
git commit -m "feat(api): create, merge and delete branches; hand a conflict to the branch lead"
```

---

### Task 5: Sidebar hierarchy and branch actions

**Files:**
- Modify: `apps/web/src/lib/api.ts` (`WorkspaceSummary`, new calls)
- Modify: `apps/web/src/components/session-sidebar.tsx`
- Modify: `apps/web/src/components/workspace-editor.tsx` (branch rows: merge and delete)

**Interfaces:**
- Consumes: Task 4's endpoints.
- Produces, in `api.ts`:
  - `WorkspaceSummary` gains `parent_id: string | null`, `branch: string` and `base_branch: string`.
  - `createBranch(projectId: string, name: string): Promise<WorkspaceSummary>`
  - `mergeBranch(branchId: string, commitProject?: boolean): Promise<{ status: string; detail: string; paths: string[] }>`
  - `fetchBranchCapability(workspaceId: string): Promise<{ can_branch: boolean; reason: string }>`

- [ ] **Step 1: Group the listing by project**

In `session-sidebar.tsx`, derive from `visible`: `projects` (`parent_id === null`) and `branchesByProject` (a `Map<string, WorkspaceSummary[]>`). The existing `active`/`archived` split applies to projects; a branch follows its own `archived` flag within its project's group.

A branch whose project is filtered out by the search box still shows, under its project's row — the project row renders when it matches *or* any of its branches do. Keep `shortPath` on the project row; a branch row shows its branch name instead of its path, which is the useful thing about it.

- [ ] **Step 2: Render branch rows under their project**

Extract the existing `row` helper's body so a branch row reuses it with `className="pl-6"` (indent) and a branch glyph instead of `WorkspaceIcon` when the branch has no icon of its own. Add a disclosure chevron on a project row that has branches, defaulting to open — unlike the archive, these are the things you are working on now. Collapsed state lives in a `Set<string>` in component state, not persisted.

Add a "브랜치 추가" action to the project row (a second `SidebarMenuAction`, `PlusIcon`), disabled with the reason from `fetchBranchCapability` as its `title` when the project cannot host branches.

- [ ] **Step 3: Branch creation dialog**

Reuse the existing `Dialog`/`FieldGroup` pattern of the add-workspace dialog: one name field, no path field (the path is decided by the server). On success, refresh and open the new branch's tab, exactly as `submit` does for a workspace.

- [ ] **Step 4: Merge and delete in the workspace editor**

In `workspace-editor.tsx`, when `workspace.parent_id` is set, show a "main으로 머지" button and give the delete control the branch wording. Report the merge result in the existing error place, one message per status:

- `merged` → done.
- `empty` → nothing to merge.
- `dirty` → the project has uncommitted changes, with a second button "커밋하고 머지" that re-calls `mergeBranch(id, true)`. This is the common path, not an edge case, so the second button sits next to the message rather than behind a dialog.
- `moved` → the project checkout is on another branch; name the base branch from `detail`.
- `missing` → the checkout is gone from disk; name the path.
- `conflict` → list `paths` and say the conflict is waiting in this branch's own conversation for its agent to resolve.

- [ ] **Step 5: Verify the UI builds and the shapes line up**

Run: `cd apps/web && npx tsc --noEmit`
Expected: no errors.

Then check the real app: a project row with two branches, each opening its own conversation, and a merge button on a branch.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/lib/api.ts apps/web/src/components/session-sidebar.tsx apps/web/src/components/workspace-editor.tsx
git commit -m "feat(web): show branches under their project, with create and merge"
```

---

### Task 6: README and full-suite verification

**Files:**
- Modify: `README.md`, `README.en.md` (the paragraph defining a workspace)

- [ ] **Step 1: Update the workspace paragraph**

Both READMEs say the unit of management is a workspace: one folder, one lead agent, one conversation. Add that a project can hold feature branches, each an isolated checkout with its own lead agent and conversation, merged back on an explicit action. Two or three sentences, matching the surrounding tone; Korean in `README.md`, English in `README.en.md`.

- [ ] **Step 2: Run the whole suite**

Run: `scripts/test.sh`
Expected: PASS. (~120-160s; needs Docker for the Postgres fixture.)

- [ ] **Step 3: Commit**

```bash
git add README.md README.en.md
git commit -m "docs: branches under a project"
```
