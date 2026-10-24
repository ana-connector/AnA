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
    Workspace,
    create_workspace,
    forget_workspace,
    get_workspace,
    list_branches,
    set_workspace_icon,
    set_workspace_system_prompt,
)


def checkout_for(project: Workspace, branch_row: Workspace) -> BranchCheckout:
    return BranchCheckout(
        path=branch_row.path,
        branch=branch_row.branch,
        base_branch=branch_row.base_branch,
        project=project.path,
    )


async def create_branch(
    pool: AsyncConnectionPool, *, project_id: str, name: str, icon: str = ""
) -> Workspace:
    """A feature branch of `project_id`: a checkout, and the row that gives it
    a lead agent and a conversation."""
    if not name.strip():
        raise BranchError("브랜치 이름이 필요하다.")
    project = await get_workspace(pool, project_id)
    if project is None:
        raise BranchError("그런 프로젝트가 없다.")
    if project.parent_id is not None:
        raise BranchError("브랜치 아래에는 브랜치를 만들 수 없다. 한 단계까지다.")

    slug = branches.slug_for(name)
    checkout = await branches.create(project.path, slug)
    try:
        branch = await create_workspace(
            pool,
            name=name,
            path=checkout.path,
            parent_id=project.id,
            branch=checkout.branch,
            base_branch=checkout.base_branch,
            # Scoped to the project, so "auth" under two projects is two rows.
            workspace_id=f"{project.id}--{slug}",
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


async def _branch_and_project(
    pool: AsyncConnectionPool, branch_id: str
) -> tuple[Workspace, Workspace]:
    branch = await get_workspace(pool, branch_id)
    if branch is None:
        raise BranchError("그런 브랜치가 없다.")
    if branch.parent_id is None:
        raise BranchError("브랜치가 아니라 프로젝트다.")
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
    pool: AsyncConnectionPool, branch_id: str, *, force: bool = False
) -> None:
    """Removes the checkout, the git branch, and the row with its
    conversation. Refuses while the branch holds unmerged commits, unless
    forced — that branch is the only copy of that work."""
    branch, project = await _branch_and_project(pool, branch_id)
    await branches.remove(checkout_for(project, branch), force=force)
    await forget_workspace(pool, branch_id)


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
