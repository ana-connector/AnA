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
import hashlib
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


def enabled() -> bool:
    """ANA_BRANCHES=off puts a deployment back to one lead agent per folder.

    Branches change what a workspace *is*, so a deployment that does not want
    them needs a way to say so without the feature having to be removed. Read
    per call rather than at import, as `worktrees.enabled()` is, so it can be
    flipped without restarting the server.

    Off hides the control rather than breaking it: `can_branch` answers with
    the reason, so the API refuses with it and the sidebar's button carries it
    as its tooltip. Branches that already exist keep working — they are
    ordinary workspace rows, and taking their conversations away because a
    flag changed would be the wrong kind of off."""
    return os.environ.get("ANA_BRANCHES", "on").lower() != "off"


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

    merged     — the branch is in the project now
    empty      — the branch has nothing the project does not already have
    dirty      — the project has uncommitted changes; retry with commit_project
    moved      — the project checkout is no longer on the branch's base branch
    missing    — the branch's checkout is gone from disk
    conflict   — aborted in the project, markers left in the branch for its agent
    unresolved — the branch is still mid-conflict; nothing was merged
    failed     — git refused for some other reason; nothing was touched
    """

    status: str
    detail: str = ""
    paths: tuple[str, ...] = ()


def slug_for(name: str) -> str:
    """A folder- and branch-safe slug for a branch name.

    `slugify` keeps only `[a-z0-9._-]`, so a Korean name — which is what this
    UI is written in — survives as nothing at all and every branch of a
    project would be called "branch". A short digest of the name keeps them
    apart, and keeps the same name mapping to the same slug."""
    slug = slugify(name, fallback="")
    if slug:
        return slug
    digest = hashlib.sha256(name.strip().encode("utf-8")).hexdigest()[:8]
    return f"branch-{digest}"


async def can_branch(project_path: str) -> str:
    """"" when the project can host branches, otherwise why it cannot.

    A sentence rather than a boolean: every caller — the API, the UI's
    disabled button — has to tell the user which of these it is."""
    if not enabled():
        return "이 배포에서는 브랜치 기능이 꺼져 있다 (ANA_BRANCHES=off)."
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


async def valid_branch_name(project_path: str, name: str) -> str:
    """"" when git would accept `name` as a branch, otherwise why not.

    Asked of git rather than reimplemented: the rules (no `..`, no trailing
    `.lock`, no control characters, and a dozen more) live in
    `git check-ref-format`, and a second copy of them would drift."""
    code, _ = await worktrees.git(
        "check-ref-format", "--branch", name, cwd=project_path
    )
    if code != 0:
        return (
            f"「{name}」은 git 브랜치 이름으로 쓸 수 없다. 공백과 `..`, `~`, `^`, "
            "`:`, `?`, `*`, `[`, 끝의 `.lock` 같은 것은 git이 거부한다."
        )
    return ""


async def create(
    project_path: str, slug: str, *, branch_name: Optional[str] = None
) -> BranchCheckout:
    """A new checkout for one feature branch, under the project.

    `slug` names the checkout's directory; `branch_name` is the git branch,
    which the user may choose (`feat/abc`) rather than have derived from a
    label. Two names on purpose: a label is for people and reads in their own
    language, a branch name is for git and has to survive its rules."""
    reason = await can_branch(project_path)
    if reason:
        raise BranchError(reason)

    base = await worktrees.current_branch(project_path)
    branch = (branch_name or "").strip() or f"{BRANCH_PREFIX}/{slug}"
    invalid = await valid_branch_name(project_path, branch)
    if invalid:
        raise BranchError(invalid)
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

            # Before anything is committed anywhere. A branch left mid-conflict
            # by an earlier attempt must not be committed as it stands:
            # `git add -A` marks a conflicted file resolved with its markers
            # still in it, and the merge that follows would put `<<<<<<<` in
            # the project.
            unresolved = await conflicted_paths(checkout.path)
            if unresolved:
                return MergeOutcome(
                    status="unresolved",
                    detail="이 브랜치에 아직 해결되지 않은 충돌이 있다.",
                    paths=unresolved,
                )

            # Checked before the project is committed: "커밋하고 머지" that
            # cannot merge must not commit either.
            if not await _has_anything_to_merge(checkout):
                return MergeOutcome(status="empty")

            code, dirty = await worktrees.git(
                "status", "--porcelain", cwd=checkout.project
            )
            if code == 0 and dirty.strip():
                if not commit_project:
                    return MergeOutcome(status="dirty", detail=dirty[-2000:])
                landed, output = await _commit_project(checkout)
                if not landed:
                    # A refusing pre-commit hook leaves the tree dirty, and
                    # merging anyway fails in a way that reads like a conflict.
                    return MergeOutcome(status="dirty", detail=output[-2000:])

            await worktrees.commit(_as_worktree(checkout), message)

            code, output = await worktrees.git(
                *worktrees.IDENTITY, "merge", "--no-edit", "--no-ff", checkout.branch,
                cwd=checkout.project,
            )
            if code == 0:
                return MergeOutcome(status="merged")

            # Read before the abort erases it: a non-zero exit is not
            # necessarily a conflict. A timeout, a hook, a stale index.lock
            # all land here, and calling those a conflict sent the branch's
            # agent looking for markers that do not exist.
            project_conflicts = await conflicted_paths(checkout.project)
            await worktrees.git("merge", "--abort", cwd=checkout.project)
            if not project_conflicts:
                return MergeOutcome(status="failed", detail=output[-2000:])
            paths = await _reproduce_conflict_in_branch(checkout)
            return MergeOutcome(
                status="conflict", detail=output[-2000:], paths=paths or project_conflicts
            )


async def _has_anything_to_merge(checkout: BranchCheckout) -> bool:
    """Whether the branch holds work the project does not.

    Commits it is ahead by, or an uncommitted working tree that is about to
    become one. A base branch that cannot be compared counts as "something" —
    proceeding to a merge that fails honestly beats claiming there is nothing
    there."""
    code, dirty = await worktrees.git("status", "--porcelain", cwd=checkout.path)
    if code == 0 and dirty.strip():
        return True
    ahead = await unmerged_commits(checkout)
    return ahead is None or ahead > 0


async def _commit_project(checkout: BranchCheckout) -> tuple[bool, str]:
    """Commits the project's own uncommitted work. False with git's output
    when it did not land."""
    code, output = await worktrees.git("add", "-A", cwd=checkout.project)
    if code != 0:
        return False, output
    code, output = await worktrees.git(
        *worktrees.IDENTITY, "commit", "-m",
        f"ANA: {checkout.base_branch} 작업 저장 (머지 전)",
        cwd=checkout.project,
    )
    if code != 0:
        return False, output
    code, remaining = await worktrees.git("status", "--porcelain", cwd=checkout.project)
    return (code != 0 or not remaining.strip()), output


async def conflicted_paths(path: str) -> tuple[str, ...]:
    """The paths a checkout has left in conflict, as git reports them."""
    code, names = await worktrees.git(
        "diff", "--name-only", "--diff-filter=U", cwd=path
    )
    if code != 0:
        return ()
    return tuple(line for line in names.splitlines() if line.strip())


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
    return await conflicted_paths(checkout.path)


async def unmerged_commits(checkout: BranchCheckout) -> Optional[int]:
    """How many commits the branch has that its base does not, or None when
    git cannot say.

    None is not zero. `rev-list base..branch` fails whenever the base branch
    no longer resolves — renamed, deleted, the project moved — and answering
    "nothing unmerged" there let a delete drop the only copy of the work."""
    code, output = await worktrees.git(
        "rev-list", "--count", f"{checkout.base_branch}..{checkout.branch}",
        cwd=checkout.project,
    )
    if code != 0:
        return None
    try:
        return int(output.strip())
    except ValueError:
        return None


async def remove(checkout: BranchCheckout, *, force: bool = False) -> None:
    """Drops the checkout and the branch.

    Refuses while the branch holds commits the project does not: that branch
    is the only copy of that work, and deleting it is not recoverable."""
    if not force:
        ahead = await unmerged_commits(checkout)
        if ahead is None:
            # Cannot tell is not permission. The guard protects the only copy
            # of the work, so it refuses rather than guessing.
            raise BranchError(
                f"이 브랜치를 「{checkout.base_branch}」와 비교할 수 없다 — 그 "
                "브랜치가 이름이 바뀌었거나 없어진 것 같다. 무엇을 잃는지 알 수 "
                "없으니 지우지 않았다. 확인하고 정말 버릴 거면 강제 삭제를 선택해라."
            )
        if ahead:
            raise BranchError(
                f"아직 「{checkout.base_branch}」에 없는 커밋이 {ahead}개 있다. "
                "머지하거나, 보관함에 넣거나, 정말 버릴 거면 강제 삭제를 선택해라."
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
