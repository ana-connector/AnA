"""The HTTP surface for branches, and who gets handed a conflict."""
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.orchestrator.workspaces import run_workspace_migrations


def _git(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@localhost", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "base", cwd=path)
    return path


@pytest_asyncio.fixture
async def workspace_pool(db_pool):
    await run_workspace_migrations(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM workspaces")
    return db_pool


@pytest.fixture
def client(workspace_pool, postgres_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project_in_repo(client, tmp_path):
    folder = _repo(tmp_path / "proj")
    return client.post(
        "/workspaces", json={"name": "proj", "path": str(folder)}
    ).json()["id"], folder


@pytest.fixture
def plain_project(client, tmp_path, monkeypatch):
    """A folder that is not a repository: ANA git-inits new workspaces, so the
    only way to have one is to adopt a folder with the init turned off."""
    monkeypatch.setenv("ANA_WORKSPACE_GIT_INIT", "off")
    folder = tmp_path / "plain"
    folder.mkdir()
    return client.post(
        "/workspaces", json={"name": "plain", "path": str(folder)}
    ).json()["id"]


@pytest.fixture
def submitted(monkeypatch):
    """Every prompt the server hands to a conversation, with whose it is."""
    calls: list[tuple[str, str]] = []

    class Runner:
        busy = False

        def __init__(self, workspace_id):
            self.workspace_id = workspace_id

        async def submit(self, prompt, agent, auto):
            calls.append((self.workspace_id, prompt))

    async def ensure_runner(app, workspace_id):
        return Runner(workspace_id)

    monkeypatch.setattr("apps.server.app.websocket.ensure_runner", ensure_runner)
    return calls


def test_create_branch_endpoint_returns_the_child_workspace(client, project_in_repo):
    project_id, _ = project_in_repo

    response = client.post(f"/workspaces/{project_id}/branches", json={"name": "auth"})

    assert response.status_code == 200
    body = response.json()
    assert body["parent_id"] == project_id
    assert body["branch"] == "ana/branch/auth"
    assert body["base_branch"] == "main"


def test_a_branch_is_listed_with_its_parent(client, project_in_repo):
    project_id, _ = project_in_repo
    client.post(f"/workspaces/{project_id}/branches", json={"name": "auth"})

    rows = client.get("/workspaces").json()["workspaces"]

    assert sorted((r["id"], r["parent_id"]) for r in rows) == [
        (project_id, None),
        (f"{project_id}--auth", project_id),
    ]


def test_create_branch_on_non_git_project_is_422(client, plain_project):
    response = client.post(f"/workspaces/{plain_project}/branches", json={"name": "auth"})

    assert response.status_code == 422
    assert response.json()["detail"]


def test_branch_capability_says_why_a_plain_folder_cannot_branch(client, plain_project):
    body = client.get(f"/workspaces/{plain_project}/branch-capability").json()

    assert body["can_branch"] is False and body["reason"]


def test_branch_capability_is_true_for_a_repository(client, project_in_repo):
    project_id, _ = project_in_repo

    body = client.get(f"/workspaces/{project_id}/branch-capability").json()

    assert body == {"can_branch": True, "reason": ""}


def test_merge_endpoint_brings_the_work_in(client, project_in_repo):
    project_id, folder = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()
    (Path(branch["path"]) / "b.txt").write_text("new\n", encoding="utf-8")

    body = client.post(f"/workspaces/{branch['id']}/merge").json()

    assert body["status"] == "merged"
    assert (folder / "b.txt").read_text(encoding="utf-8") == "new\n"


def test_merge_endpoint_reports_a_conflict_without_merging(client, project_in_repo, submitted):
    project_id, folder = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()
    (Path(branch["path"]) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (folder / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=folder)

    body = client.post(f"/workspaces/{branch['id']}/merge").json()

    assert body["status"] == "conflict"
    assert body["paths"] == ["a.txt"]
    assert (folder / "a.txt").read_text(encoding="utf-8") == "from main\n"


def test_conflict_is_submitted_to_the_branchs_own_lead(client, project_in_repo, submitted):
    project_id, folder = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()
    (Path(branch["path"]) / "a.txt").write_text("from branch\n", encoding="utf-8")
    (folder / "a.txt").write_text("from main\n", encoding="utf-8")
    _git("commit", "-am", "main moves", cwd=folder)

    client.post(f"/workspaces/{branch['id']}/merge")

    # The branch's own conversation, not the project's: it has the context of
    # the work that conflicts, and it is the only one allowed in that checkout.
    assert [workspace for workspace, _ in submitted] == [branch["id"]]
    assert "a.txt" in submitted[0][1]


def test_a_dirty_project_merges_only_when_asked(client, project_in_repo):
    project_id, folder = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()
    (Path(branch["path"]) / "b.txt").write_text("new\n", encoding="utf-8")
    (folder / "a.txt").write_text("main is mid-edit\n", encoding="utf-8")

    first = client.post(f"/workspaces/{branch['id']}/merge").json()
    assert first["status"] == "dirty"

    second = client.post(
        f"/workspaces/{branch['id']}/merge", json={"commit_project": True}
    ).json()

    assert second["status"] == "merged"
    assert (folder / "a.txt").read_text(encoding="utf-8") == "main is mid-edit\n"
    assert (folder / "b.txt").read_text(encoding="utf-8") == "new\n"


def test_deleting_a_branch_with_unmerged_work_needs_force(client, project_in_repo):
    project_id, _ = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()
    (Path(branch["path"]) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(branch["path"]))
    _git("commit", "-m", "work", cwd=Path(branch["path"]))

    refused = client.delete(f"/workspaces/{branch['id']}")
    assert refused.status_code == 409 and refused.json()["detail"]
    assert Path(branch["path"]).exists()

    assert client.delete(f"/workspaces/{branch['id']}?force=true").status_code == 200
    assert not Path(branch["path"]).exists()


def test_deleting_a_project_removes_its_branches(client, project_in_repo):
    project_id, _ = project_in_repo
    branch = client.post(
        f"/workspaces/{project_id}/branches", json={"name": "auth"}
    ).json()

    assert client.delete(f"/workspaces/{project_id}").status_code == 200

    assert client.get("/workspaces").json()["workspaces"] == []
    assert not Path(branch["path"]).exists()
