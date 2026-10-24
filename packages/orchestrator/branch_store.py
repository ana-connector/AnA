"""Where a branch's git checkout and its workspace row meet.

`branches.py` runs git and knows nothing about the database; `workspaces.py`
runs SQL and knows nothing about git. This module is the only place that holds
both, so "create a branch" means one checkout and one row, and a half-done
create leaves neither.
"""
import contextlib
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from packages.orchestrator import branches
from packages.orchestrator.branches import BranchCheckout, BranchError, MergeOutcome
from packages.orchestrator.workspaces import (
    BRANCH_ID_SEPARATOR,
    Workspace,
    create_workspace,
    forget_workspace,
    get_workspace,
    list_branches,
    set_workspace_icon,
    set_workspace_system_prompt,
)


class NoSuchBranch(BranchError):
    """The id names nothing at all — a 404 rather than something to fix."""


def checkout_for(project: Workspace, branch_row: Workspace) -> BranchCheckout:
    return BranchCheckout(
        path=branch_row.path,
        branch=branch_row.branch,
        base_branch=branch_row.base_branch,
        project=project.path,
    )


async def create_branch(
    pool: AsyncConnectionPool,
    *,
    project_id: str,
    name: str,
    branch: Optional[str] = None,
    icon: str = "",
) -> Workspace:
    """A feature branch of `project_id`: a checkout, and the row that gives it
    a lead agent and a conversation.

    `name` is the label people read ("add some Feature"); `branch` is the git
    branch ("feat/abc"), derived from the label when the caller leaves it
    out."""
    if not name.strip():
        raise BranchError("브랜치 이름이 필요하다.")
    project = await get_workspace(pool, project_id)
    if project is None:
        raise BranchError("그런 프로젝트가 없다.")
    if project.parent_id is not None:
        raise BranchError("브랜치 아래에는 브랜치를 만들 수 없다. 한 단계까지다.")

    slug, checkout = await _create_checkout(
        project.path, branches.slug_for(name), branch_name=(branch or "").strip() or None
    )
    try:
        branch = await create_workspace(
            pool,
            name=name,
            path=checkout.path,
            parent_id=project.id,
            branch=checkout.branch,
            base_branch=checkout.base_branch,
            # Scoped to the project, so "auth" under two projects is two rows.
            workspace_id=f"{project.id}{BRANCH_ID_SEPARATOR}{slug}",
        )
    except Exception:
        # A row that never landed must not leave a checkout nobody can reach.
        with contextlib.suppress(Exception):
            await branches.remove(checkout, force=True)
        raise

    # Seeded, not inherited: a branch whose prompt changes when the project's
    # does is harder to reason about than one that was given a copy. Container
    # settings are deliberately not copied — see the spec.
    prompt = project.system_prompt
    if prompt:
        await set_workspace_system_prompt(pool, branch.id, prompt)
    if icon:
        await set_workspace_icon(pool, branch.id, icon)
    return await get_workspace(pool, branch.id) or branch


async def _create_checkout(
    project_path: str, slug: str, *, branch_name: Optional[str] = None
) -> tuple[str, BranchCheckout]:
    """The checkout, and the slug it ended up with.

    The id is de-duplicated by `_unique_id`, but the checkout path is the slug
    itself, so the slug has to be de-duplicated too — otherwise a second
    branch is refused for a name its user never used. The first failure that
    is not a collision is raised as it is.

    A git branch name the caller *chose* is never de-duplicated: silently
    creating `feat/abc-2` for someone who asked for `feat/abc` is worse than
    telling them the name is taken."""
    if branch_name:
        return slug, await branches.create(
            project_path, slug, branch_name=branch_name
        )
    for suffix in range(1, 100):
        candidate = slug if suffix == 1 else f"{slug}-{suffix}"
        try:
            return candidate, await branches.create(project_path, candidate)
        except BranchError:
            # A collision is the only reason to try the next name. Anything
            # else — not a repository, no commits — fails the same way for
            # every candidate, so ask once whether this is a collision at all.
            if await branches.can_branch(project_path) != "":
                raise
    raise BranchError(f"「{slug}」로 쓸 수 있는 이름을 찾지 못했다.")


async def _branch_and_project(
    pool: AsyncConnectionPool, branch_id: str
) -> tuple[Workspace, Workspace]:
    branch = await get_workspace(pool, branch_id)
    if branch is None:
        raise NoSuchBranch("그런 브랜치가 없다.")
    if branch.parent_id is None:
        # A real workspace, just not a branch — something the caller can fix,
        # which is not what "no such thing" says.
        raise BranchError("브랜치가 아니라 프로젝트다. 머지할 브랜치를 골라라.")
    project = await get_workspace(pool, branch.parent_id)
    if project is None:
        raise BranchError("이 브랜치의 프로젝트가 없다.")
    return branch, project


async def merge_branch(
    pool: AsyncConnectionPool, branch_id: str, *, commit_project: bool = False
) -> MergeOutcome:
    branch, project = await _branch_and_project(pool, branch_id)
    return await branches.merge_into_project(
        checkout_for(project, branch),
        f"ANA: 「{branch.name}」 브랜치 작업",
        commit_project=commit_project,
    )


async def delete_branch(
    pool: AsyncConnectionPool,
    branch_id: str,
    *,
    force: bool = False,
    purge: bool = True,
) -> None:
    """Removes the checkout, the git branch, and the row — with its
    conversation unless `purge` is false. Refuses while the branch holds
    unmerged commits, unless forced: that branch is the only copy of that
    work."""
    branch, project = await _branch_and_project(pool, branch_id)
    await branches.remove(checkout_for(project, branch), force=force)
    await forget_workspace(pool, branch_id, purge=purge)


async def delete_project_branches(pool: AsyncConnectionPool, project_id: str) -> None:
    """Takes a project's branches with it.

    Forced: the user is deleting the whole project, and the branches are part
    of what they are deleting — refusing over unmerged commits here would
    leave rows pointing at a project that no longer exists. A checkout that is
    already gone is not an error; the row still has to go."""
    for branch in await list_branches(pool, project_id):
        with contextlib.suppress(Exception):
            await delete_branch(pool, branch.id, force=True)
        if await get_workspace(pool, branch.id) is not None:
            await forget_workspace(pool, branch.id)


async def branch_capability(
    pool: AsyncConnectionPool, workspace_id: str
) -> tuple[bool, str]:
    """Whether this workspace can host branches, and why not when it cannot."""
    workspace = await get_workspace(pool, workspace_id)
    if workspace is None:
        return False, "그런 워크스페이스가 없다."
    if workspace.parent_id is not None:
        return False, "브랜치 아래에는 브랜치를 만들 수 없다. 한 단계까지다."
    reason = await branches.can_branch(workspace.path)
    return (reason == ""), reason
