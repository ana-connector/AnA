"""Per-worker git worktrees: isolation, merge-back, and conflict handling."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from packages.orchestrator import worktrees


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


async def test_each_worker_gets_its_own_checkout(repo):
    one = await worktrees.create(str(repo), "t1", thread_id="th")
    two = await worktrees.create(str(repo), "t2", thread_id="th")

    assert one is not None and two is not None
    assert one.path != two.path
    assert (Path(one.path) / "a.txt").read_text(encoding="utf-8") == "base\n"
    # Writing in one is invisible in the other — the whole point.
    (Path(one.path) / "a.txt").write_text("from one\n", encoding="utf-8")
    assert (Path(two.path) / "a.txt").read_text(encoding="utf-8") == "base\n"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"


async def test_work_merges_back_into_the_workspace(repo):
    tree = await worktrees.create(str(repo), "t1", thread_id="th")
    (Path(tree.path) / "b.txt").write_text("new file\n", encoding="utf-8")

    result = await worktrees.merge(tree, "add b")

    assert result.merged and not result.empty
    assert (repo / "b.txt").read_text(encoding="utf-8") == "new file\n"


async def test_a_worker_that_changed_nothing_merges_nothing(repo):
    tree = await worktrees.create(str(repo), "t1", thread_id="th")

    result = await worktrees.merge(tree, "noop")

    assert result.merged and result.empty
    assert _git("log", "--oneline", cwd=repo).count("\n") == 0  # still one commit


async def test_two_workers_on_the_same_file_do_not_clobber_each_other(repo):
    """Before worktrees this was silent data loss: last writer won, and the
    other worker's edit was simply gone."""
    one = await worktrees.create(str(repo), "t1", thread_id="th")
    two = await worktrees.create(str(repo), "t2", thread_id="th")
    (Path(one.path) / "a.txt").write_text("from one\n", encoding="utf-8")
    (Path(two.path) / "a.txt").write_text("from two\n", encoding="utf-8")

    lock = asyncio.Lock()
    first = await worktrees.merge(one, "one", lock=lock)
    second = await worktrees.merge(two, "two", lock=lock)

    assert first.merged
    # The second edit conflicts rather than overwriting, and its work is kept
    # on its branch for a human to resolve.
    assert not second.merged
    assert second.branch in _git("branch", "--list", second.branch, cwd=repo)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "from one\n"
    # A failed merge must leave the workspace clean, not mid-merge.
    assert _git("status", "--porcelain", cwd=repo) == ""


async def test_removing_a_worktree_keeps_a_conflicted_branch(repo):
    tree = await worktrees.create(str(repo), "t1", thread_id="th")
    (Path(tree.path) / "a.txt").write_text("x\n", encoding="utf-8")
    await worktrees.commit(tree, "x")

    await worktrees.remove(tree, keep_branch=True)

    assert not Path(tree.path).exists()
    assert tree.branch in _git("branch", "--list", tree.branch, cwd=repo)


async def test_a_plain_directory_is_not_isolated(tmp_path):
    """No repository, nothing to branch: the workers share the directory as
    they did before, and the caller is told so with None."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert await worktrees.create(str(plain), "t1") is None


async def test_an_empty_repository_is_not_isolated(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git("init", "-b", "main", cwd=empty)

    assert await worktrees.create(str(empty), "t1") is None


async def test_the_switch_turns_it_off(repo, monkeypatch):
    monkeypatch.setenv("ANA_WORKER_WORKTREES", "off")

    assert await worktrees.create(str(repo), "t1") is None


async def test_worktrees_are_kept_out_of_git_status(repo):
    await worktrees.create(str(repo), "t1", thread_id="th")

    # The user's own `git status` must not fill up with ANA's scaffolding —
    # and the exclusion goes in .git/info/exclude, not their .gitignore.
    assert _git("status", "--porcelain", cwd=repo) == ""
    assert not (repo / ".gitignore").exists()


async def test_merging_works_without_a_configured_git_identity(tmp_path, monkeypatch):
    """The server container has no global git config. Without an identity on
    the merge command itself, git refuses with "Committer identity unknown" —
    a non-zero exit that reads exactly like a merge conflict, which is how the
    first live run reported two perfectly mergeable branches as conflicted."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "home" / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "home" / "system.gitconfig"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "anon"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("-c", "user.name=x", "-c", "user.email=x@y", "commit", "--allow-empty", "-m", "base", cwd=repo)

    tree = await worktrees.create(str(repo), "t1", thread_id="th")
    (Path(tree.path) / "new.txt").write_text("hi\n", encoding="utf-8")
    result = await worktrees.merge(tree, "add new")

    assert result.merged, result.reason
    assert (repo / "new.txt").exists()


async def test_a_worker_that_raises_still_gives_its_worktree_back(repo, db_pool, monkeypatch):
    """A failed or cancelled task used to leave its checkout and its branch
    behind — one leak per failure, piling up in the workspace."""
    import contextlib

    from apps.server.app.graph import build_graph
    from langgraph.checkpoint.memory import InMemorySaver
    from packages.adapters.base import AdapterEvent, AgentAdapter

    class Exploding(AgentAdapter):
        async def run_stream(self, prompt, session_id=None):
            raise RuntimeError("boom")
            yield AdapterEvent(type="text")  # pragma: no cover

        async def terminate(self):
            return None

    class Lead(AgentAdapter):
        def __init__(self):
            self.replies = [
                '{"mode":"delegate","tasks":[{"id":"a","title":"t","prompt":"p"}]}',
                "정리했습니다",
            ]

        async def run_stream(self, prompt, session_id=None):
            text = self.replies.pop(0) if self.replies else "ok"
            yield AdapterEvent(type="text", text=text, session_id="lead-sid")
            yield AdapterEvent(type="result", text=text, session_id="lead-sid", cost_usd=0.0)

        async def terminate(self):
            return None

    from packages.memory.db import run_migrations

    await run_migrations(db_pool)
    graph = build_graph(InMemorySaver())
    config = {
        "configurable": {
            "thread_id": "wt-leak",
            "pool": db_pool,
            "embedding_model": _StubEmbedding(),
            "transcript": _NullTranscript(),
            "adapters": {"claude": Lead()},
            "worker_factory": lambda *args, **kwargs: Exploding(),
            "workspace_dir": str(repo),
        }
    }
    with contextlib.suppress(Exception):
        async for _ in graph.astream(
            {"prompt": "해줘", "agent": "claude", "plan_mode": True},
            config,
            stream_mode="custom",
        ):
            pass

    # No checkout and no branch left over.
    assert not (repo / ".ana" / "worktrees").exists() or not list(
        (repo / ".ana" / "worktrees").iterdir()
    )
    assert _git("branch", "--list", "ana/*", cwd=repo).strip() == ""


class _StubEmbedding:
    def encode(self, text):
        return [0.0] * 384

    async def encode_async(self, text):
        return [0.0] * 384


class _NullTranscript:
    def append(self, *args, **kwargs):
        return None


async def test_two_turns_on_one_workspace_do_not_share_a_worktree_name():
    """A second browser tab on the same workspace is enough: both turns plan a
    "t1", and a shared name meant creating the second one deleted the first
    one's live checkout out from under a running worker."""
    import tempfile

    directory = Path(tempfile.mkdtemp())
    _git("init", "-b", "main", cwd=directory)
    _git("-c", "user.name=x", "-c", "user.email=x@y", "commit", "--allow-empty", "-m", "base", cwd=directory)

    first = await worktrees.create(str(directory), "t1", thread_id="th")
    (Path(first.path) / "mine.txt").write_text("first\n", encoding="utf-8")
    second = await worktrees.create(str(directory), "t1", thread_id="th")

    assert first.path != second.path
    assert first.branch != second.branch
    # The first worker's work is still there.
    assert (Path(first.path) / "mine.txt").exists()


def test_the_merge_lock_is_per_workspace():
    from packages.orchestrator import worktrees as module

    assert module.merge_lock_for("/a") is module.merge_lock_for("/a/")
    assert module.merge_lock_for("/a") is not module.merge_lock_for("/b")
