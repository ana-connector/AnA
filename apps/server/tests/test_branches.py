"""Feature-branch checkouts: isolation, merge-back, and where a conflict lands."""
import subprocess
from pathlib import Path

import pytest

from packages.orchestrator import branches


def _git(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


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
    # Main is never the tree left in conflict.
    assert (repo / "a.txt").read_text(encoding="utf-8") == "from main\n"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    # The branch checkout holds the conflict, with markers its own agent may read.
    branch_text = (Path(checkout.path) / "a.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" in branch_text and "from main" in branch_text


async def test_a_resolved_conflict_merges_on_the_second_attempt(repo):
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (repo / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=repo)
    assert (await branches.merge_into_project(checkout, "auth 작업")).status == "conflict"

    # What the branch's lead agent does, in its own checkout.
    (Path(checkout.path) / "a.txt").write_text("resolved\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(checkout.path))
    _git("commit", "-m", "resolve", cwd=Path(checkout.path))

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "merged"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "resolved\n"


async def test_merge_reports_a_checkout_that_is_gone(repo):
    checkout = await branches.create(str(repo), "auth")
    _git("worktree", "remove", "--force", checkout.path, cwd=repo)

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "missing"
    assert "auth" in outcome.detail


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
