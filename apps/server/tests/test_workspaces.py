"""Workspaces: the folder ANA works in, and the one conversation that owns it."""
from pathlib import Path

import subprocess

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.orchestrator.workspaces import (
    SELF_WORKSPACE_ID,
    WorkspacePathError,
    create_workspace,
    ensure_self_workspace,
    ensure_workspace,
    forget_workspace,
    get_workspace,
    list_workspaces,
    rename_workspace,
    run_workspace_migrations,
    slugify,
)


@pytest_asyncio.fixture
async def workspace_pool(db_pool):
    await run_workspace_migrations(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM workspaces")
    return db_pool


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ANA Server", "ana-server"),
        ("  내 프로젝트  ", "workspace"),  # nothing ASCII survives; the fallback does
        ("a/b/c", "a-b-c"),
        ("", "workspace"),
    ],
)
def test_slugify_produces_folder_safe_ids(name, expected):
    assert slugify(name) == expected


@pytest.mark.asyncio
async def test_create_makes_a_folder_under_the_root(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    workspace = await create_workspace(workspace_pool, name="Demo Project")

    assert workspace.id == "demo-project"
    assert Path(workspace.path).is_dir()
    assert str(tmp_path / "ws") in workspace.path


@pytest.mark.asyncio
async def test_create_adopts_an_existing_directory(workspace_pool, tmp_path):
    existing = tmp_path / "already-here"
    existing.mkdir()
    (existing / "keep.txt").write_text("mine", encoding="utf-8")

    workspace = await create_workspace(workspace_pool, name="Adopted", path=str(existing))

    assert workspace.path == str(existing)
    # Adopting a project must never touch what is already in it.
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_duplicate_names_get_distinct_ids(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    first = await create_workspace(workspace_pool, name="Same Name")
    second = await create_workspace(workspace_pool, name="Same Name")

    assert first.id != second.id
    assert second.id.startswith("same-name")


@pytest.mark.asyncio
async def test_ensure_adopts_an_unregistered_id(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    # Conversations created before workspaces existed still have a directory;
    # an unknown id is an unregistered workspace, not an error.
    workspace = await ensure_workspace(workspace_pool, "legacy-thread-1")

    assert workspace.id == "legacy-thread-1"
    assert Path(workspace.path).is_dir()
    assert await get_workspace(workspace_pool, "legacy-thread-1") is not None


@pytest.mark.asyncio
async def test_ensure_returns_the_registered_path(workspace_pool, tmp_path):
    adopted = tmp_path / "project"
    adopted.mkdir()
    created = await create_workspace(workspace_pool, name="Project", path=str(adopted))

    again = await ensure_workspace(workspace_pool, created.id)

    assert again.path == str(adopted)


@pytest.mark.asyncio
async def test_rename_and_forget(workspace_pool, tmp_path):
    adopted = tmp_path / "keepme"
    adopted.mkdir()
    workspace = await create_workspace(workspace_pool, name="Old", path=str(adopted))

    assert await rename_workspace(workspace_pool, workspace.id, "New") is True
    assert (await get_workspace(workspace_pool, workspace.id)).name == "New"

    assert await forget_workspace(workspace_pool, workspace.id) is True
    assert await get_workspace(workspace_pool, workspace.id) is None
    # Unregistering manages the pointer, not the files.
    assert adopted.is_dir()

    assert await forget_workspace(workspace_pool, workspace.id) is False


@pytest.mark.asyncio
async def test_listing_reports_conversation_activity(workspace_pool, tmp_path, monkeypatch):
    from packages.memory.embeddings import EmbeddingModel
    from packages.memory.rag import store_turn

    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    workspace = await create_workspace(workspace_pool, name="Busy")
    await store_turn(
        workspace_pool, EmbeddingModel(),
        thread_id=workspace.id, session_id="sid", prompt="p", response="r",
        cost_usd=None, agent="codex",
    )

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}

    # The workspace *is* the thread, so its turns are its activity.
    assert rows[workspace.id]["turn_count"] == 1
    assert rows[workspace.id]["last_agent"] == "codex"


@pytest.mark.asyncio
async def test_a_workspace_with_no_turns_still_lists(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    await create_workspace(workspace_pool, name="Empty One")

    rows = await list_workspaces(workspace_pool)

    assert rows[0]["turn_count"] == 0
    assert rows[0]["last_agent"] is None


@pytest.fixture
def client(workspace_pool, postgres_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    with TestClient(app) as test_client:
        yield test_client


def test_api_create_list_rename_delete(client):
    created = client.post("/workspaces", json={"name": "API Project"}).json()
    assert created["id"] == "api-project"

    listed = client.get("/workspaces").json()["workspaces"]
    assert [w["id"] for w in listed] == ["api-project"]

    assert client.patch("/workspaces/api-project", json={"name": "Renamed"}).status_code == 200
    assert client.get("/workspaces").json()["workspaces"][0]["name"] == "Renamed"

    assert client.delete("/workspaces/api-project").status_code == 200
    assert client.get("/workspaces").json()["workspaces"] == []


def test_api_rejects_a_nameless_workspace(client):
    assert client.post("/workspaces", json={"name": "   "}).status_code == 422


def test_api_404s_on_unknown_workspace(client):
    assert client.patch("/workspaces/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/workspaces/nope").status_code == 404


def test_websocket_opens_a_workspace_by_id(client, tmp_path):
    adopted = tmp_path / "adopted"
    adopted.mkdir()
    client.post("/workspaces", json={"name": "Adopted", "path": str(adopted)})

    with client.websocket_connect("/ws/chat?workspace=adopted") as ws:
        frame = ws.receive_json()

    # The conversation runs in the registered folder, and its id is the
    # workspace id.
    assert frame["workspace"] == str(adopted)
    assert frame["workspace_id"] == "adopted"
    assert frame["workspace_name"] == "Adopted"
    assert frame["text"] == "adopted"


def test_websocket_still_accepts_thread_id(client):
    # The older spelling of the same parameter keeps working.
    with client.websocket_connect("/ws/chat?thread_id=legacy-one") as ws:
        frame = ws.receive_json()

    assert frame["workspace_id"] == "legacy-one"
    assert client.get("/workspaces").json()["workspaces"][0]["id"] == "legacy-one"


async def test_a_new_workspace_is_a_git_repository(workspace_pool, tmp_path, monkeypatch):
    """Sub-agent isolation is a worktree per task, and a plain directory has
    nothing to branch from — without this every worker would still share one
    tree."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "root"))

    workspace = await create_workspace(workspace_pool, name="git me")

    assert (Path(workspace.path) / ".git").exists()
    head = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace.path, capture_output=True, text=True
    )
    assert "ANA: workspace created" in head.stdout


async def test_an_adopted_repository_is_left_alone(workspace_pool, tmp_path):
    """An existing project is someone else's repository; ANA registers a
    pointer to it and restructures nothing."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "trunk"], cwd=project, capture_output=True)

    workspace = await create_workspace(workspace_pool, name="adopted", path=str(project))

    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=workspace.path, capture_output=True, text=True,
    )
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace.path, capture_output=True, text=True
    )
    assert branch.stdout.strip() == "trunk"
    # No ANA commit was added to someone else's repository.
    assert log.stdout.strip() == ""


async def test_git_init_can_be_switched_off(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ANA_WORKSPACE_GIT_INIT", "off")

    workspace = await create_workspace(workspace_pool, name="plain")

    assert not (Path(workspace.path) / ".git").exists()


async def test_an_unreachable_path_is_refused_with_a_reason(workspace_pool, tmp_path):
    """It used to be a bare 500, which reads as "ANA is broken" rather than
    "that folder is not reachable from the server"."""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)

    with pytest.raises(WorkspacePathError) as caught:
        await create_workspace(workspace_pool, name="nope", path=str(blocked / "child"))

    assert "서버가 볼 수 있는 경로" in str(caught.value)


async def test_forgetting_a_workspace_really_purges_its_conversation(workspace_pool, tmp_path, monkeypatch):
    """Regression: the purge loop also hit `checkpoint_migrations`, which has
    no thread_id. The raise was caught with a rollback — which discarded the
    turns delete that had already run, so purging removed the pointer and left
    the conversation behind."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "root"))
    workspace = await create_workspace(workspace_pool, name="purge me")
    async with workspace_pool.connection() as conn:
        from packages.memory.db import EMBEDDING_DIMENSIONS

        await conn.execute(
            "INSERT INTO turns (thread_id, agent, prompt, response, embedding)"
            " VALUES (%s, %s, %s, %s, %s)",
            (workspace.id, "claude", "q", "a", str([0.0] * EMBEDDING_DIMENSIONS)),
        )

    assert await forget_workspace(workspace_pool, workspace.id, purge=True)

    async with workspace_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM turns WHERE thread_id = %s", (workspace.id,)
        )
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_underscore_in_a_name_does_not_widen_the_id_search(
    workspace_pool, tmp_path, monkeypatch
):
    """`_` is a LIKE wildcard, so "my_app" used to collect every "myXapp" as a
    taken id. Uniqueness survived that, but the query was quietly wrong."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    await create_workspace(workspace_pool, name="myXapp")

    second = await create_workspace(workspace_pool, name="my_app")

    # Not "my_app-2": nothing with that id exists.
    assert second.id == "my_app"


@pytest.mark.asyncio
async def test_self_workspace_points_at_anas_own_tree(workspace_pool, tmp_path, monkeypatch):
    """ANA can be pointed at its own repository, so the agent that edits ANA is
    reachable from the same list, under the same gate, as every other project."""
    repo = tmp_path / "ana"
    repo.mkdir()
    monkeypatch.setenv("ANA_SELF_WORKSPACE", str(repo))
    monkeypatch.setenv("ANA_WORKSPACE_GIT_INIT", "off")

    registered = await ensure_self_workspace(workspace_pool)

    assert registered is not None
    assert registered.id == SELF_WORKSPACE_ID
    assert registered.path == str(repo)
    # Registering twice is what every server start does.
    assert (await ensure_self_workspace(workspace_pool)).id == SELF_WORKSPACE_ID


@pytest.mark.asyncio
async def test_self_workspace_keeps_a_rename_but_follows_the_mount(
    workspace_pool, tmp_path, monkeypatch
):
    first = tmp_path / "ana"
    moved = tmp_path / "ana-moved"
    first.mkdir()
    moved.mkdir()
    monkeypatch.setenv("ANA_WORKSPACE_GIT_INIT", "off")
    monkeypatch.setenv("ANA_SELF_WORKSPACE", str(first))
    await ensure_self_workspace(workspace_pool)
    await rename_workspace(workspace_pool, SELF_WORKSPACE_ID, "내 ANA")

    monkeypatch.setenv("ANA_SELF_WORKSPACE", str(moved))
    again = await ensure_self_workspace(workspace_pool)

    assert again.path == str(moved)
    assert again.name == "내 ANA"


@pytest.mark.asyncio
async def test_no_self_workspace_without_the_mount(workspace_pool, tmp_path, monkeypatch):
    """A pointer to a folder the server cannot see would only fail at the first
    turn, so an unset or missing path registers nothing."""
    monkeypatch.delenv("ANA_SELF_WORKSPACE", raising=False)
    assert await ensure_self_workspace(workspace_pool) is None

    monkeypatch.setenv("ANA_SELF_WORKSPACE", str(tmp_path / "not-there"))
    assert await ensure_self_workspace(workspace_pool) is None


@pytest.mark.asyncio
async def test_the_self_workspace_id_is_reserved(workspace_pool, tmp_path, monkeypatch):
    """A user workspace called "ANA self" must not take the id the server
    registers on startup, or that insert becomes a unique violation."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    created = await create_workspace(workspace_pool, name="ANA self")

    assert created.id != SELF_WORKSPACE_ID


def test_capabilities_report_the_self_workspace(client, tmp_path, monkeypatch):
    payload = client.get("/workspaces/capabilities").json()
    assert "containers" in payload
    # Nothing is mounted in the test environment, so there is no recursive
    # workspace to mark.
    assert payload["self_workspace"] is None


@pytest.mark.asyncio
async def test_a_minted_conversation_gets_a_readable_name(
    workspace_pool, tmp_path, monkeypatch
):
    """A brand-new conversation's id is a uuid, and a sidebar row reading
    "9f1c2b0e-4a1d-…" says nothing about which conversation it is."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    minted = await ensure_workspace(workspace_pool, "9f1c2b0e-4a1d-4c77-9f31-0b8c5a12e340")
    named = await ensure_workspace(workspace_pool, "legacy-thread")

    assert minted.name == "새 대화 (9f1c2b)"
    # An id a human chose is still its own name.
    assert named.name == "legacy-thread"
