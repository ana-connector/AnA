"""Per-worker git worktrees.

Until now every sub-agent of a turn worked in the *same* directory: five
workers, one tree, no locking. Two tasks touching the same file silently
clobbered each other, and a task that went wrong left its mess in the
workspace with no way back.

Each worker now gets its own worktree on its own branch, and ANA merges the
branches back one at a time when the workers finish. A conflict is reported
rather than resolved: the branch is left in place, and the lead is told which
task could not be merged — guessing at a merge is how an orchestrator destroys
work it cannot see.

Deliberate limits:

- **Only a git workspace is isolated.** A plain directory has nothing to
  branch from, so its workers share it exactly as before (and the report says
  so, rather than pretending they were isolated).
- **Worktrees live inside the workspace**, under `.ana/worktrees/`. The gate
  confines every tool call to the session workspace; a worktree outside it
  would make every worker's own edits look like an escape.
- **Merges are sequential.** They run under one lock per workspace, in task
  order, so two merges never race the index.
"""
import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WORKTREE_DIRNAME = os.path.join(".ana", "worktrees")
BRANCH_PREFIX = "ana"
# Long enough for a worker to finish its own git plumbing; short enough that a
# wedged git does not hold a turn open forever.
GIT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Worktree:
    """One worker's isolated checkout."""

    path: str
    branch: str
    base_branch: str
    workspace: str


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    reason: str = ""
    # True when the worker changed nothing, so there was nothing to merge.
    empty: bool = False
    branch: str = ""


def enabled() -> bool:
    """ANA_WORKER_WORKTREES=off puts every worker back in the shared
    directory. Read per call so it can be flipped without a restart."""
    return os.environ.get("ANA_WORKER_WORKTREES", "on").lower() != "off"


# Every command that can create a commit carries an identity. The server
# container has no global git config, and without this a merge commit fails
# with "Committer identity unknown" — which arrives as a non-zero exit and
# reads exactly like a merge conflict. The identity is passed per command
# rather than written into the repository's config, which belongs to whoever
# works in it.
IDENTITY = ("-c", "user.name=ANA", "-c", "user.email=ana@localhost")


async def _git(*args: str, cwd: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "git timed out"
    return process.returncode or 0, output.decode("utf-8", errors="replace").strip()


async def is_git_workspace(workspace: str) -> bool:
    code, output = await _git("rev-parse", "--is-inside-work-tree", cwd=workspace)
    return code == 0 and output.strip() == "true"


async def current_branch(workspace: str) -> Optional[str]:
    code, output = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace)
    if code != 0:
        return None
    branch = output.strip()
    # A detached HEAD has no branch to merge back into; the caller treats that
    # as "cannot isolate" rather than inventing one.
    return None if branch in ("", "HEAD") else branch


# One lock per workspace path, not per turn: two conversations open on the
# same folder merge into the same index, and git answers the second one with
# "Another git process seems to be running".
_MERGE_LOCKS: dict[str, asyncio.Lock] = {}


def merge_lock_for(workspace: str) -> asyncio.Lock:
    key = os.path.normpath(workspace)
    lock = _MERGE_LOCKS.get(key)
    if lock is None:
        lock = _MERGE_LOCKS[key] = asyncio.Lock()
    return lock


async def create(workspace: str, task_id: str, *, thread_id: str = "") -> Optional[Worktree]:
    """A fresh worktree for one task, or None when the workspace cannot host
    one (not a repo, detached HEAD, no commits yet, git unavailable)."""
    if not enabled() or shutil.which("git") is None:
        return None
    if not await is_git_workspace(workspace):
        return None
    base = await current_branch(workspace)
    if base is None:
        return None
    # An empty repository has no commit to branch from. `git worktree add`
    # would fail; sharing the directory is the honest fallback.
    code, _ = await _git("rev-parse", "--verify", "HEAD", cwd=workspace)
    if code != 0:
        return None

    # The name carries a per-run suffix. Without it, two turns on the same
    # workspace — a second browser tab is enough — produce the same name for
    # their own "t1", and creating the second one deleted the first one's
    # live checkout out from under a running worker.
    slug = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in f"{thread_id}-{task_id}-{uuid.uuid4().hex[:6]}"
    )
    slug = slug.strip("-") or task_id
    branch = f"{BRANCH_PREFIX}/{slug}"
    path = Path(workspace) / WORKTREE_DIRNAME / slug

    path.parent.mkdir(parents=True, exist_ok=True)
    await _exclude_worktrees(workspace)
    code, output = await _git(
        "worktree", "add", "--force", "-B", branch, str(path), "HEAD", cwd=workspace
    )
    if code != 0:
        return None
    return Worktree(path=str(path), branch=branch, base_branch=base, workspace=workspace)


async def _exclude_worktrees(workspace: str) -> None:
    """Keeps `.ana/` out of the user's `git status`.

    Written to `.git/info/exclude` rather than `.gitignore`: the workspace may
    be someone's own repository, and ANA has no business editing a tracked
    file in it."""
    code, git_dir = await _git("rev-parse", "--git-common-dir", cwd=workspace)
    if code != 0:
        return
    base = Path(git_dir if os.path.isabs(git_dir) else os.path.join(workspace, git_dir))
    exclude = base / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".ana/" not in current:
            exclude.write_text(current.rstrip() + "\n.ana/\n", encoding="utf-8")
    except OSError:
        pass  # a read-only .git is not a reason to fail the turn


async def commit(worktree: Worktree, message: str) -> bool:
    """Commits everything the worker produced. False means it produced
    nothing."""
    code, output = await _git("status", "--porcelain", cwd=worktree.path)
    if code != 0 or not output.strip():
        return False
    await _git("add", "-A", cwd=worktree.path)
    code, _ = await _git(
        *IDENTITY, "commit", "-m", message[:200] or "ANA sub-agent", cwd=worktree.path
    )
    return code == 0


async def merge(worktree: Worktree, message: str, lock: Optional[asyncio.Lock] = None) -> MergeResult:
    """Commits the worker's changes and merges the branch back.

    A conflict aborts the merge and keeps the branch: the workspace is left
    exactly as it was, and the task result says which branch holds the work.
    Resolving it is a decision, and decisions belong to the user or to a task
    the lead plans, not to plumbing."""
    committed = await commit(worktree, message)
    if not committed:
        return MergeResult(merged=True, empty=True, branch=worktree.branch)

    lock = lock or asyncio.Lock()
    async with lock:  # merges are sequential: two at once race the index
        code, output = await _git(
            *IDENTITY, "merge", "--no-edit", "--no-ff", worktree.branch,
            cwd=worktree.workspace,
        )
        if code == 0:
            return MergeResult(merged=True, branch=worktree.branch)
        await _git("merge", "--abort", cwd=worktree.workspace)
        return MergeResult(merged=False, reason=output[-800:], branch=worktree.branch)


async def remove(worktree: Worktree, *, keep_branch: bool = False) -> None:
    """Drops the checkout. The branch survives when the merge failed, because
    it is the only copy of that work."""
    await _git("worktree", "remove", "--force", worktree.path, cwd=worktree.workspace)
    if not keep_branch:
        await _git("branch", "-D", worktree.branch, cwd=worktree.workspace)
    # `worktree remove` leaves the directory behind if git refused; the
    # administrative entry is pruned either way so the next turn can reuse the
    # name.
    await _git("worktree", "prune", cwd=worktree.workspace)
    if os.path.isdir(worktree.path):
        shutil.rmtree(worktree.path, ignore_errors=True)
