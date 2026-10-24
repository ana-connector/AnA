"""The branch-management MCP contract, and the gate scoping behind it."""
import io
import json
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.mcp.servers.branch import CREATE, DELETE, LIST, MERGE, RENAME, handle
from packages.orchestrator.branch_store import create_branch
from packages.orchestrator.workspaces import create_workspace, run_workspace_migrations


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


# --- the MCP contract, with no server behind it -----------------------------


def test_lists_the_five_branch_tools():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == [LIST, CREATE, RENAME, MERGE, DELETE]


def test_create_without_a_name_fails_before_any_http_call():
    response = handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": CREATE, "arguments": {"name": "  "}},
    })
    assert response["result"]["isError"] is True


def test_merge_without_an_id_fails_before_any_http_call():
    response = handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": MERGE, "arguments": {}},
    })
    assert response["result"]["isError"] is True


def test_an_unknown_tool_is_a_protocol_error():
    response = handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "drop_everything", "arguments": {}},
    })
    assert response["error"]["code"] == -32602


def test_a_conflict_tells_the_agent_the_markers_are_in_its_own_checkout(monkeypatch):
    """The one result the agent has to act on rather than report."""
    from packages.mcp.servers import branch

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        branch.urllib.request, "urlopen",
        lambda *a, **k: Response(json.dumps({
            "status": "conflict", "detail": "CONFLICT (content)", "paths": ["a.txt"],
        }).encode()),
    )

    result = branch._call_tool(MERGE, {"id": "proj--auth"})

    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "a.txt" in text
    assert "conflict" in text.lower()


def test_a_dirty_project_result_names_the_retry(monkeypatch):
    from packages.mcp.servers import branch

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(
        branch.urllib.request, "urlopen",
        lambda *a, **k: Response(json.dumps({
            "status": "dirty", "detail": " M a.txt", "paths": [],
        }).encode()),
    )

    result = branch._call_tool(MERGE, {"id": "proj--auth"})

    assert "commit_project" in result["content"][0]["text"]


# --- the gate endpoints, where the scoping lives ---------------------------


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


def _token_for(client, thread_id: str) -> str:
    """A gate session for `thread_id`, the way a running turn would have one.
    The registry mints the token, so the caller is handed it back."""

    async def send(_message: dict) -> None:
        return None

    session = client.app.state.gate_registry.register(thread_id, send)
    return session.token


def test_the_gate_lists_the_callers_own_project_and_branches(client, tmp_path, workspace_pool):
    import asyncio

    folder = _repo(tmp_path / "proj")

    async def setup():
        project = await create_workspace(workspace_pool, name="proj", path=str(folder))
        branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
        return project.id, branch.id

    project_id, branch_id = asyncio.run(setup())
    token = _token_for(client, project_id)

    body = client.get("/gate/branches", headers={"x-ana-gate-token": token}).json()

    assert body["project"]["id"] == project_id
    assert [b["id"] for b in body["branches"]] == [branch_id]


def test_a_branchs_own_agent_sees_its_sibling_branches(client, tmp_path, workspace_pool):
    """Called from inside a branch, the project is the parent — otherwise a
    branch agent could see nothing at all, including itself."""
    import asyncio

    folder = _repo(tmp_path / "proj")

    async def setup():
        project = await create_workspace(workspace_pool, name="proj", path=str(folder))
        one = await create_branch(workspace_pool, project_id=project.id, name="auth")
        two = await create_branch(workspace_pool, project_id=project.id, name="pay")
        return project.id, one.id, two.id

    project_id, one, two = asyncio.run(setup())
    token = _token_for(client, one)

    body = client.get("/gate/branches", headers={"x-ana-gate-token": token}).json()

    assert body["project"]["id"] == project_id
    assert sorted(b["id"] for b in body["branches"]) == sorted([one, two])


def test_the_gate_refuses_an_unknown_token(client):
    assert client.get("/gate/branches", headers={"x-ana-gate-token": "nope"}).status_code == 403


def test_an_agent_cannot_touch_another_projects_branch(client, tmp_path, workspace_pool):
    """The whole point of the scoping: a 404, not a 403 — the agent has no
    business learning that an id it cannot reach exists."""
    import asyncio

    mine = _repo(tmp_path / "mine")
    theirs = _repo(tmp_path / "theirs")

    async def setup():
        a = await create_workspace(workspace_pool, name="mine", path=str(mine))
        b = await create_workspace(workspace_pool, name="theirs", path=str(theirs))
        stranger = await create_branch(workspace_pool, project_id=b.id, name="secret")
        return a.id, stranger.id

    my_project, stranger = asyncio.run(setup())
    headers = {"x-ana-gate-token": _token_for(client, my_project)}

    assert client.post(f"/gate/branches/{stranger}/merge", headers=headers).status_code == 404
    assert client.delete(f"/gate/branches/{stranger}", headers=headers).status_code == 404
    assert client.patch(
        f"/gate/branches/{stranger}", json={"name": "x"}, headers=headers
    ).status_code == 404


def test_an_agent_creates_merges_and_deletes_in_its_own_project(client, tmp_path, workspace_pool):
    import asyncio

    folder = _repo(tmp_path / "proj")

    async def setup():
        project = await create_workspace(workspace_pool, name="proj", path=str(folder))
        return project.id

    project_id = asyncio.run(setup())
    headers = {"x-ana-gate-token": _token_for(client, project_id)}

    created = client.post("/gate/branches", json={"name": "auth"}, headers=headers).json()
    assert created["parent_id"] == project_id

    renamed = client.patch(
        f"/gate/branches/{created['id']}", json={"name": "인증"}, headers=headers
    ).json()
    assert renamed["name"] == "인증"

    (Path(created["path"]) / "b.txt").write_text("new\n", encoding="utf-8")
    merged = client.post(f"/gate/branches/{created['id']}/merge", headers=headers).json()
    assert merged["status"] == "merged"
    assert (folder / "b.txt").read_text(encoding="utf-8") == "new\n"

    assert client.delete(f"/gate/branches/{created['id']}", headers=headers).status_code == 200


def test_the_gate_reports_a_refused_delete_rather_than_forcing_it(client, tmp_path, workspace_pool):
    import asyncio

    folder = _repo(tmp_path / "proj")

    async def setup():
        project = await create_workspace(workspace_pool, name="proj", path=str(folder))
        branch = await create_branch(workspace_pool, project_id=project.id, name="auth")
        return project.id, branch

    project_id, branch = asyncio.run(setup())
    (Path(branch.path) / "b.txt").write_text("new\n", encoding="utf-8")
    _git("add", "-A", cwd=Path(branch.path))
    _git("commit", "-m", "work", cwd=Path(branch.path))
    headers = {"x-ana-gate-token": _token_for(client, project_id)}

    refused = client.delete(f"/gate/branches/{branch.id}", headers=headers)

    assert refused.status_code == 409 and refused.json()["detail"]
    assert Path(branch.path).exists()
