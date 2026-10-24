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


async def test_branches_can_be_switched_off(repo, monkeypatch):
    """ANA_BRANCHES=off puts a deployment back to one lead agent per folder.
    Read per call, as the worker-worktree switch is, so it can be flipped
    without a restart."""
    monkeypatch.setenv("ANA_BRANCHES", "off")

    assert not branches.enabled()
    reason = await branches.can_branch(str(repo))
    assert reason != ""
    with pytest.raises(branches.BranchError):
        await branches.create(str(repo), "auth")
    assert not (repo / ".ana" / "branches").exists()


async def test_branches_are_on_by_default(repo, monkeypatch):
    monkeypatch.delenv("ANA_BRANCHES", raising=False)

    assert branches.enabled()
    assert await branches.can_branch(str(repo)) == ""


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


async def test_a_second_press_never_commits_conflict_markers_into_main(repo):
    """`git add -A` in a mid-merge checkout marks conflicted files resolved
    with the markers still in them, so an unguarded second merge wrote
    `<<<<<<<` into the project — the spec's own anti-goal."""
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (repo / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=repo)
    assert (await branches.merge_into_project(checkout, "auth 작업")).status == "conflict"

    outcome = await branches.merge_into_project(checkout, "auth 작업")

    assert outcome.status == "unresolved"
    assert outcome.paths == ("a.txt",)
    assert "<<<<<<<" not in (repo / "a.txt").read_text(encoding="utf-8")


async def test_a_merge_that_fails_for_another_reason_is_not_called_a_conflict(repo):
    """A git failure that is not a conflict used to be reported as one, and
    the branch was given a merge commit nobody asked for."""
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    try:
        outcome = await branches.merge_into_project(checkout, "auth 작업")
    finally:
        (repo / ".git" / "index.lock").unlink(missing_ok=True)

    assert outcome.status == "failed"
    assert outcome.detail
    # No conflict was invented in the branch: no markers, and its HEAD is the
    # ordinary one-parent commit of its own work, not a merge of the base.
    assert await branches.conflicted_paths(checkout.path) == ()
    parents = _git("rev-list", "--parents", "-1", "HEAD", cwd=Path(checkout.path))
    assert len(parents.split()) == 2


async def test_a_project_commit_that_does_not_land_is_reported_as_dirty(repo):
    """A pre-commit hook that refuses leaves the tree dirty. Merging anyway
    fails in a way that reads like a conflict, so the refusal is reported."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")

    outcome = await branches.merge_into_project(checkout, "auth 작업", commit_project=True)

    assert outcome.status == "dirty"
    assert not (repo / "b.txt").exists()


async def test_commit_project_does_not_commit_when_there_is_nothing_to_merge(repo):
    """"커밋하고 머지" that cannot merge must not commit either: a write to
    someone's repository that serves no purpose."""
    checkout = await branches.create(str(repo), "auth")
    (repo / "a.txt").write_text("mid-edit\n", encoding="utf-8")
    before = _git("rev-parse", "HEAD", cwd=repo)

    outcome = await branches.merge_into_project(checkout, "auth 작업", commit_project=True)

    assert outcome.status == "empty"
    assert _git("rev-parse", "HEAD", cwd=repo) == before
    assert _git("status", "--porcelain", cwd=repo) != ""


async def test_unmerged_commits_says_it_cannot_tell_rather_than_zero(repo):
    """`rev-list base..branch` fails when the base branch no longer resolves.
    Reporting that as 0 let an unforced delete drop the only copy."""
    checkout = await branches.create(str(repo), "auth")
    (Path(checkout.path) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(checkout.path))
    _git("commit", "-m", "work", cwd=Path(checkout.path))
    assert await branches.unmerged_commits(checkout) == 1

    _git("branch", "-m", "main", "trunk", cwd=repo)

    assert await branches.unmerged_commits(checkout) is None
    with pytest.raises(branches.BranchError):
        await branches.remove(checkout)
    assert Path(checkout.path).exists()


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
