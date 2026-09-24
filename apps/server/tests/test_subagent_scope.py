"""Sub-agent permission scopes: what a lead may hand a worker, and what the
gate does with it (roadmap stage 3 "권한 스코프", stage 5 STRIDE/EoP)."""
import pytest

from packages.governance.subagent_scope import (
    MODE_ENV,
    PATHS_ENV,
    READ,
    WRITE,
    Scope,
    violation,
)
from packages.orchestrator.planner import parse_plan

WORKSPACE = "/work/thread-1"


def test_default_scope_restricts_nothing():
    scope = Scope()

    assert scope.unrestricted
    assert scope.to_env() == {}
    assert violation("Bash", {"command": "rm x"}, scope, WORKSPACE) is None


def test_read_scope_refuses_the_tools_that_change_things():
    scope = Scope(mode=READ)

    for tool in ("Write", "Edit", "NotebookEdit", "mcp__git__git_commit"):
        assert violation(tool, {}, scope, WORKSPACE) is not None, tool


@pytest.mark.parametrize(
    "command",
    ["ls -la", "ls | wc -l", "cat a.txt", "grep -rn x .", "git log --oneline",
     "git status", "find . -name '*.py'", "RG_FLAG=1 rg pattern", "sed -n 1,5p f"],
)
def test_read_scope_still_allows_a_shell_that_only_looks(command):
    """The first read-only worker ANA ran could not answer "how many files are
    here", because listing a directory is a shell command. A survey task needs
    `ls`, `grep` and `git log`; it does not need `rm`."""
    assert violation("Bash", {"command": command}, Scope(mode=READ), WORKSPACE) is None


@pytest.mark.parametrize(
    "command",
    ["echo hi > f", "rm -rf x", "git commit -m x", "sed -i 's/a/b/' f",
     "python script.py", "ls && rm f", "cat a.txt >> b.txt", "npm install"],
)
def test_read_scope_refuses_a_shell_that_could_change_something(command):
    """An allowlist, because the set of ways to change a file is not
    enumerable — an unknown command reads as a write."""
    assert violation("Bash", {"command": command}, Scope(mode=READ), WORKSPACE) is not None


def test_read_scope_still_allows_reading():
    scope = Scope(mode=READ)

    for tool in ("Read", "Grep", "Glob", "mcp__git__git_log", "mcp__git__git_diff"):
        assert violation(tool, {"file_path": f"{WORKSPACE}/a.py"}, scope, WORKSPACE) is None, tool


def test_path_scope_confines_relative_paths_too():
    """The workspace-escape rule ignores relative paths because they resolve
    inside the working directory — which is exactly the hole a sub-directory
    scope has to close."""
    scope = Scope(paths=("docs",))

    assert violation("Read", {"file_path": "docs/plan.md"}, scope, WORKSPACE) is None
    assert violation("Edit", {"file_path": "src/app.py"}, scope, WORKSPACE) is not None


def test_path_scope_accepts_the_absolute_form_of_the_same_path():
    scope = Scope(paths=("docs",))

    assert violation("Read", {"file_path": f"{WORKSPACE}/docs/a.md"}, scope, WORKSPACE) is None
    assert violation("Read", {"file_path": "/etc/passwd"}, scope, WORKSPACE) is not None


def test_path_scope_reads_paths_out_of_a_shell_command():
    scope = Scope(paths=("docs",))

    assert violation("Bash", {"command": f"cat {WORKSPACE}/src/secret.py"}, scope, WORKSPACE)


def test_system_paths_are_not_scope_violations():
    """Same reasoning as the workspace check: flagging `ls /usr/bin` trains the
    user to approve without reading."""
    scope = Scope(paths=("docs",))

    assert violation("Bash", {"command": "ls /usr/bin"}, scope, WORKSPACE) is None


def test_scope_survives_the_trip_through_the_environment():
    scope = Scope(mode=READ, paths=("docs", "tests"))
    env = scope.to_env()

    restored = Scope.from_payload(env.get(MODE_ENV), env.get(PATHS_ENV))

    assert restored == scope


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"mode": "read"}, Scope(mode=READ)),
        ({"mode": "nonsense"}, Scope(mode=WRITE)),
        ({"paths": ["docs", "docs", "src/"]}, Scope(paths=("docs", "src"))),
        # A malformed scope must read as "no extra permission", never as "no
        # restriction on where it may reach".
        ({"paths": ["../../etc", "~/.ssh", 3]}, Scope()),
        ("not-a-dict", Scope()),
        (None, Scope()),
    ],
)
def test_scope_parsing_is_defensive(raw, expected):
    assert Scope.parse(raw) == expected


def test_plan_tasks_carry_the_scope_the_lead_chose():
    plan = parse_plan(
        """
        {"mode": "delegate", "tasks": [
          {"title": "조사", "agent": "claude", "prompt": "읽고 보고해라",
           "scope": {"mode": "read", "paths": ["docs"]}},
          {"title": "구현", "agent": "codex", "prompt": "고쳐라"}
        ]}
        """
    )

    assert plan.tasks[0].scope == Scope(mode=READ, paths=("docs",))
    # Unstated means the default, which is what every task got before scopes
    # existed.
    assert plan.tasks[1].scope.unrestricted


WORKTREE = f"{WORKSPACE}/.ana/worktrees/th-t1"


def test_a_path_scope_follows_the_worker_into_its_worktree():
    """Sub-agent isolation gives each task its own checkout under
    `.ana/worktrees/`. Resolving the scope only against the workspace root
    denied the worker its own copy of the very directory it was granted."""
    scope = Scope(paths=("docs",))

    assert violation(
        "Read", {"file_path": f"{WORKTREE}/docs/plan.md"}, scope, WORKSPACE, WORKTREE
    ) is None
    # The same file in the main tree is still fine, and so is a relative path.
    assert violation(
        "Read", {"file_path": f"{WORKSPACE}/docs/plan.md"}, scope, WORKSPACE, WORKTREE
    ) is None
    assert violation("Read", {"file_path": "docs/plan.md"}, scope, WORKSPACE, WORKTREE) is None


def test_the_scope_still_holds_inside_the_worktree():
    scope = Scope(paths=("docs",))

    assert violation(
        "Edit", {"file_path": f"{WORKTREE}/src/app.py"}, scope, WORKSPACE, WORKTREE
    ) is not None


def test_a_cwd_outside_the_workspace_is_not_a_second_root():
    """cwd comes from the hook payload, which is the CLI's word for where it
    is running. A value outside the session workspace must not widen the
    scope."""
    scope = Scope(paths=("docs",))

    assert violation(
        "Read", {"file_path": "/elsewhere/docs/a.md"}, scope, WORKSPACE, "/elsewhere"
    ) is not None
