"""The MCP registry: role scoping, placeholder substitution, and the reasons
a server is left out."""
import textwrap

import pytest

from packages.mcp.registry import Registry, load_registry

SAMPLE = textwrap.dedent(
    """
    servers:
      - name: git
        description: local git
        command: /bin/sh
        args: ["-c", "mcp-server-git --repository {workspace}"]
        roles: [worker]
      - name: postgres
        command: /bin/sh
        env:
          DATABASE_URI: "{database_url}"
        roles: [worker]
      - name: github
        command: /bin/sh
        env:
          TOKEN: "{secret:GITHUB_PERSONAL_ACCESS_TOKEN}"
        requires_env: [GITHUB_PERSONAL_ACCESS_TOKEN]
        roles: [worker]
      - name: notes
        command: /bin/sh
        roles: [lead, worker]
        enabled: false
    """
)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return Registry.from_yaml(path)


def test_worker_gets_scoped_servers(registry):
    resolved, _ = registry.resolve(
        "worker", workspace_dir="/w", database_url="postgres://x", secrets={}
    )
    assert [server.name for server in resolved] == ["git", "postgres"]


def test_lead_gets_nothing_it_is_not_scoped_for(registry):
    resolved, skipped = registry.resolve("lead", secrets={})
    assert resolved == []
    reasons = {entry.name: entry.reason for entry in skipped}
    assert "lead 역할 범위 밖" in reasons["git"]


def test_placeholders_are_substituted(registry):
    resolved, _ = registry.resolve(
        "worker", workspace_dir="/home/hello/workspace", database_url="postgres://ana"
    )
    git = next(server for server in resolved if server.name == "git")
    postgres = next(server for server in resolved if server.name == "postgres")
    assert git.args[-1].endswith("--repository /home/hello/workspace")
    assert postgres.env["DATABASE_URI"] == "postgres://ana"


def test_server_needing_an_unregistered_secret_is_skipped(registry):
    _, skipped = registry.resolve("worker", secrets={})
    reasons = {entry.name: entry.reason for entry in skipped}
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in reasons["github"]


def test_registered_secret_enables_the_server_and_never_lands_in_argv(registry):
    resolved, _ = registry.resolve(
        "worker", secrets={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret"}
    )
    github = next(server for server in resolved if server.name == "github")
    assert github.env["TOKEN"] == "ghp_secret"
    assert all("ghp_secret" not in arg for arg in github.args)


def test_disabled_server_is_skipped_with_that_reason(registry):
    _, skipped = registry.resolve("worker", secrets={})
    reasons = {entry.name: entry.reason for entry in skipped}
    assert "비활성화" in reasons["notes"]


def test_missing_executable_is_a_skip_not_a_failure(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n  - name: ghost\n    command: definitely-not-installed\n", encoding="utf-8"
    )
    resolved, skipped = Registry.from_yaml(path).resolve("worker")
    assert resolved == []
    assert "실행 파일 없음" in skipped[0].reason


def test_secret_names_collects_every_reference(registry):
    assert registry.secret_names() == ("GITHUB_PERSONAL_ACCESS_TOKEN",)


def test_duplicate_names_are_rejected(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n  - name: git\n    command: /bin/sh\n  - name: git\n    command: /bin/sh\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        Registry.from_yaml(path)


def test_broken_registry_file_yields_an_empty_registry_not_an_exception(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    path.write_text("servers:\n  - name: nameless\n", encoding="utf-8")  # no command
    monkeypatch.setenv("ANA_MCP_REGISTRY", str(path))
    assert load_registry().names() == ()


def test_shipped_registry_scopes_the_tooling_to_workers():
    """The lead plans and delegates, so every server that touches the machine is
    worker-scoped. The one exception is ANA's own `ask`, which touches nothing —
    it puts a question to the user — and is the lead's alone."""
    registry = load_registry()
    assert registry.names_for("lead") == ("ask",)
    assert set(registry.names_for("worker")) == {"git", "postgres", "github"}
