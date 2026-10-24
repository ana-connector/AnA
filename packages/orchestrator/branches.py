"""Feature branches: a long-lived checkout, with its own lead agent.

A workspace is one folder and one lead agent, which is one line of work. Two
features at once meant two workspaces pointing at two copies of the same
project, with nothing to say they were related. A project can now hold feature
branches instead: each is a git worktree under `.ana/branches/`, on its own
branch, driven by its own lead agent in its own conversation.

This is not `worktrees.py`. That module gives each *sub-agent of one turn* a
checkout and merges it back before the turn ends; the checkouts are plumbing
the user never sees. A branch checkout lives until the user deletes it, is
merged by an explicit action, and has a workspace row and a conversation of
its own. They share the git plumbing (`worktrees.git`, `worktrees.IDENTITY`)
and nothing else — including the branch prefix, so nothing that lists branches
can mistake one for the other.

Two decisions worth stating, because neither is obvious:

- **A conflict is reproduced in the branch, never left in the project.** The
  merge runs in the project checkout; when it conflicts, it is aborted there
  and re-run the other way round *inside the branch*, which is left with the
  markers. The branch's lead agent is confined by the gate to its own
  checkout, so a conflict described to it in prose — with `--abort` having
  erased every trace — is a task it has no way to carry out. Markers in files
  it owns are the only form of the problem it can act on, and main is never
  the tree left broken.
- **A dirty project does not merge by default.** Main's lead agent edits the
  project folder directly and ANA never commits for it, so the project tree's
  normal state is dirty. Refusing outright would reject almost every real
  merge; committing someone's half-finished work without asking is the other
  wrong answer. So: refused, with `commit_project` for the caller who has
  asked the user.
"""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from packages.orchestrator import worktrees
from packages.orchestrator.workspaces import slugify

BRANCHES_DIRNAME = os.path.join(".ana", "branches")
# Deliberately not worktrees.BRANCH_PREFIX: those branches are disposable and
# deleted at the end of a turn, these are the user's work.
BRANCH_PREFIX = "ana/branch"


class BranchError(RuntimeError):
    """Something the user can fix, said in the words they will read."""


@dataclass(frozen=True)
class BranchCheckout:
    """One branch's checkout, and where it merges back to."""

    path: str
    branch: str
    base_branch: str
    project: str


@dataclass(frozen=True)
class MergeOutcome:
    """`status` is one of:

    merged  — the branch is in the project now
    empty   — the branch has nothing the project does not already have
    dirty   — the project has uncommitted changes; retry with commit_project
    moved   — the project checkout is no longer on the branch's base branch
    missing — the branch's checkout is gone from disk
    conflict— aborted in the project, markers left in the branch for its agent
    """

    status: str
    detail: str = ""
    paths: tuple[str, ...] = ()


def slug_for(name: str) -> str:
    return slugify(name, fallback="branch")


async def can_branch(project_path: str) -> str:
    """"" when the project can host branches, otherwise why it cannot.

    A sentence rather than a boolean: every caller — the API, the UI's
    disabled button — has to tell the user which of these it is."""
    if shutil.which("git") is None:
        return "이 서버에 git이 없다. 브랜치는 git 워크트리로 만든다."
    if not await worktrees.is_git_workspace(project_path):
        return "git 저장소가 아니다. 브랜치를 만들려면 폴더가 git 저장소여야 한다."
    code, _ = await worktrees.git("rev-parse", "--verify", "HEAD", cwd=project_path)
    if code != 0:
        return "커밋이 하나도 없다. 브랜치는 커밋에서 갈라져 나온다."
    if await worktrees.current_branch(project_path) is None:
        return "HEAD가 분리된 상태다. 돌아갈 브랜치가 없으면 머지할 곳도 없다."
    return ""


async def create(project_path: str, slug: str) -> BranchCheckout:
    """A new checkout for one feature branch, under the project."""
    reason = await can_branch(project_path)
    if reason:
        raise BranchError(reason)

    base = await worktrees.current_branch(project_path)
    branch = f"{BRANCH_PREFIX}/{slug}"
    path = Path(project_path) / BRANCHES_DIRNAME / slug

    if path.exists():
        raise BranchError(f"「{slug}」 체크아웃이 이미 있다: {path}")
    code, _ = await worktrees.git("rev-parse", "--verify", branch, cwd=project_path)
    if code == 0:
        raise BranchError(f"「{branch}」 브랜치가 이미 있다. 다른 이름을 써라.")

    path.parent.mkdir(parents=True, exist_ok=True)
    await worktrees._exclude_worktrees(project_path)
    # -b, not -B: -B resets an existing branch, which would take a live
    # checkout out from under whoever is working in it.
    code, output = await worktrees.git(
        "worktree", "add", "-b", branch, str(path), "HEAD", cwd=project_path
    )
    if code != 0:
        raise BranchError(f"워크트리를 만들지 못했다: {output[-500:]}")
    return BranchCheckout(
        path=str(path), branch=branch, base_branch=base or "", project=project_path
    )


async def merge_into_project(
    checkout: BranchCheckout, message: str, *, commit_project: bool = False
) -> MergeOutcome:
    """Brings the branch's work into the project checkout.

    Takes the branch's merge lock and then the project's, always in that
    order. The branch's own sub-agent workers merge into its tree under the
    branch lock, and nothing anywhere takes the project lock first, so a fixed
    order cannot deadlock."""
    if not os.path.isdir(checkout.path):
        return MergeOutcome(status="missing", detail=checkout.path)

    async with worktrees.merge_lock_for(checkout.path):
        async with worktrees.merge_lock_for(checkout.project):
            on = await worktrees.current_branch(checkout.project)
            if on != checkout.base_branch:
                return MergeOutcome(
                    status="moved",
                    detail=f"프로젝트 체크아웃이 「{on or 'HEAD 분리'}」에 있다. "
                    f"이 브랜치는 「{checkout.base_branch}」로만 머지된다.",
                )

            code, dirty = await worktrees.git(
                "status", "--porcelain", cwd=checkout.project
            )
            if code == 0 and dirty.strip():
                if not commit_project:
                    return MergeOutcome(status="dirty", detail=dirty[-2000:])
                await worktrees.git("add", "-A", cwd=checkout.project)
                await worktrees.git(
                    *worktrees.IDENTITY, "commit", "-m",
                    f"ANA: {checkout.base_branch} 작업 저장 (머지 전)",
                    cwd=checkout.project,
                )

            committed = await worktrees.commit(_as_worktree(checkout), message)
            if not committed and await unmerged_commits(checkout) == 0:
                return MergeOutcome(status="empty")

            code, output = await worktrees.git(
                *worktrees.IDENTITY, "merge", "--no-edit", "--no-ff", checkout.branch,
                cwd=checkout.project,
            )
            if code == 0:
                return MergeOutcome(status="merged")

            await worktrees.git("merge", "--abort", cwd=checkout.project)
            paths = await _reproduce_conflict_in_branch(checkout)
            return MergeOutcome(status="conflict", detail=output[-2000:], paths=paths)


async def _reproduce_conflict_in_branch(checkout: BranchCheckout) -> tuple[str, ...]:
    """Merges the base branch *into* the branch checkout and leaves it
    conflicted, so the branch's lead agent has markers to resolve.

    An already-conflicted checkout is left as it is: the agent is presumably
    partway through, and restarting its merge would throw that away."""
    code, _ = await worktrees.git(
        "rev-parse", "--verify", "MERGE_HEAD", cwd=checkout.path
    )
    if code != 0:
        await worktrees.git(
            *worktrees.IDENTITY, "merge", "--no-edit", checkout.base_branch,
            cwd=checkout.path,
        )
    code, names = await worktrees.git(
        "diff", "--name-only", "--diff-filter=U", cwd=checkout.path
    )
    if code != 0:
        return ()
    return tuple(line for line in names.splitlines() if line.strip())


async def unmerged_commits(checkout: BranchCheckout) -> int:
    """How many commits the branch has that its base does not."""
    code, output = await worktrees.git(
        "rev-list", "--count", f"{checkout.base_branch}..{checkout.branch}",
        cwd=checkout.project,
    )
    if code != 0:
        return 0
    try:
        return int(output.strip())
    except ValueError:
        return 0


async def remove(checkout: BranchCheckout, *, force: bool = False) -> None:
    """Drops the checkout and the branch.

    Refuses while the branch holds commits the project does not: that branch
    is the only copy of that work, and deleting it is not recoverable."""
    if not force:
        ahead = await unmerged_commits(checkout)
        if ahead:
            raise BranchError(
                f"아직 main에 없는 커밋이 {ahead}개 있다. 머지하거나, 보관함에 "
                "넣거나, 정말 버릴 거면 강제 삭제를 선택해라."
            )
    await worktrees.git(
        "worktree", "remove", "--force", checkout.path, cwd=checkout.project
    )
    await worktrees.git("branch", "-D", checkout.branch, cwd=checkout.project)
    await worktrees.git("worktree", "prune", cwd=checkout.project)
    if os.path.isdir(checkout.path):
        shutil.rmtree(checkout.path, ignore_errors=True)


def _as_worktree(checkout: BranchCheckout) -> worktrees.Worktree:
    """`worktrees.commit` takes a Worktree, and committing a working tree is
    the same operation whoever owns it."""
    return worktrees.Worktree(
        path=checkout.path,
        branch=checkout.branch,
        base_branch=checkout.base_branch,
        workspace=checkout.project,
    )
