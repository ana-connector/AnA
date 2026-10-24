"""A branch as a registered workspace: created, merged, deleted."""
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from packages.orchestrator.branches import BranchError
from packages.orchestrator.branch_store import (
    create_branch,
    delete_branch,
    delete_project_branches,
    merge_branch,
)
from packages.orchestrator.workspaces import (
    create_workspace,
    get_workspace,
    list_branches,
    run_workspace_migrations,
    set_container_settings,
    set_workspace_system_prompt,
)


def _git(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest_asyncio.fixture
async def workspace_pool(db_pool):
    await run_workspace_migrations(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM workspaces")
    return db_pool


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@localhost", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "base", cwd=path)
    return path


async def test_create_branch_registers_a_child_workspace(workspace_pool, repo):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))

    branch = await create_branch(workspace_pool, project_id=project.id, name="인증")

    assert branch.parent_id == project.id
    assert branch.branch.startswith("ana/branch/")
    assert branch.base_branch == "main"
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
    # Unmerged work does not hold up the delete: the user is removing the
    # whole project, and the branch is part of what they are removing.
    (Path(branch.path) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(branch.path))
    _git("commit", "-m", "work", cwd=Path(branch.path))
    path = Path(branch.path)

    await delete_project_branches(workspace_pool, project.id)

    assert await get_workspace(workspace_pool, branch.id) is None
    assert not path.exists()


async def test_two_projects_can_each_have_a_branch_of_the_same_name(
    workspace_pool, tmp_path
):
    """Branch ids are scoped to their project; "auth" under two projects is
    two rows, not a collision that silently renames one of them."""
    ids = []
    for name in ("one", "two"):
        path = tmp_path / name
        path.mkdir()
        _git("init", "-b", "main", cwd=path)
        _git("config", "user.email", "test@localhost", cwd=path)
        _git("config", "user.name", "test", cwd=path)
        (path / "a.txt").write_text("base\n", encoding="utf-8")
        _git("add", "-A", cwd=path)
        _git("commit", "-m", "base", cwd=path)
        project = await create_workspace(workspace_pool, name=name, path=str(path))
        branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
        ids.append(branch.id)

    assert ids == ["one--auth", "two--auth"]


async def test_a_failed_registration_leaves_no_orphan_checkout(
    workspace_pool, repo, monkeypatch
):
    project = await create_workspace(workspace_pool, name="proj", path=str(repo))

    async def explode(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr("packages.orchestrator.branch_store.create_workspace", explode)

    with pytest.raises(RuntimeError):
        await create_branch(workspace_pool, project_id=project.id, name="auth")

    assert not (repo / ".ana" / "branches" / "auth").exists()
