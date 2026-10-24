"""Workspaces: the folder ANA works in, and the one conversation that owns it."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import subprocess

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.orchestrator.settings import apply_workspace_prompt
from packages.orchestrator.workspaces import (
    SELF_WORKSPACE_ID,
    WorkspacePathError,
    create_workspace,
    ensure_self_workspace,
    ensure_workspace,
    forget_workspace,
    get_workspace,
    list_branches,
    list_workspaces,
    rename_workspace,
    run_workspace_migrations,
    normalize_icon,
    set_workspace_archived,
    set_workspace_icon,
    set_workspace_system_prompt,
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


def test_workspace_prompt_refreshes_without_accumulating_old_text():
    adapter = SimpleNamespace(system_prompt="Global rules")
    apply_workspace_prompt(adapter, "First workspace")
    apply_workspace_prompt(adapter, "Second workspace")
    assert adapter.system_prompt == "Global rules\n\n[WORKSPACE INSTRUCTIONS]\nSecond workspace"


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
        cost_usd=None, agent="codex", meta={"tokens": 1200},
    )

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}

    # The workspace *is* the thread, so its turns are its activity.
    assert rows[workspace.id]["turn_count"] == 1
    assert rows[workspace.id]["last_agent"] == "codex"
    assert rows[workspace.id]["total_tokens"] == 1200


@pytest.mark.asyncio
async def test_tokens_are_summed_over_the_conversation(workspace_pool, tmp_path, monkeypatch):
    """What a workspace has cost so far, which is what the sidebar shows under
    its name. Turns that never reported a token count contribute nothing rather
    than breaking the sum."""
    from packages.memory.embeddings import EmbeddingModel
    from packages.memory.rag import store_turn

    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    workspace = await create_workspace(workspace_pool, name="Counted")
    for meta in ({"tokens": 1000}, {"tokens": 500}, {}, {"tokens": None}):
        await store_turn(
            workspace_pool, EmbeddingModel(),
            thread_id=workspace.id, session_id="sid", prompt="p", response="r",
            cost_usd=None, agent="claude", meta=meta,
        )

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}

    assert rows[workspace.id]["total_tokens"] == 1500


@pytest.mark.asyncio
async def test_a_workspace_with_no_turns_reports_no_tokens(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    await create_workspace(workspace_pool, name="Untouched")

    assert (await list_workspaces(workspace_pool))[0]["total_tokens"] == 0


@pytest.mark.asyncio
async def test_a_workspace_with_no_turns_still_lists(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    await create_workspace(workspace_pool, name="Empty One")

    rows = await list_workspaces(workspace_pool)

    assert rows[0]["turn_count"] == 0
    assert rows[0]["last_agent"] is None


@pytest.mark.asyncio
async def test_workspace_system_prompt_is_isolated_and_listed(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    first = await create_workspace(workspace_pool, name="First")
    second = await create_workspace(workspace_pool, name="Second")
    assert await set_workspace_system_prompt(workspace_pool, first.id, "Use concise answers")
    assert (await get_workspace(workspace_pool, first.id)).system_prompt == "Use concise answers"
    assert (await get_workspace(workspace_pool, second.id)).system_prompt == ""
    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}
    assert rows[first.id]["system_prompt"] == "Use concise answers"
    assert rows[second.id]["system_prompt"] == ""


@pytest.mark.asyncio
async def test_archiving_hides_nothing_but_the_flag(workspace_pool, tmp_path, monkeypatch):
    """Archiving is a label, not a delete: the row, its path and its
    conversation all survive, and unarchiving puts it back."""
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    kept = await create_workspace(workspace_pool, name="Kept")
    put_away = await create_workspace(workspace_pool, name="Put away")

    assert (await get_workspace(workspace_pool, put_away.id)).archived is False
    assert await set_workspace_archived(workspace_pool, put_away.id, True)

    stored = await get_workspace(workspace_pool, put_away.id)
    assert stored.archived is True
    assert stored.path == put_away.path and stored.name == put_away.name

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}
    # Still listed — the sidebar splits the list, the server does not.
    assert rows[put_away.id]["archived"] is True
    assert rows[kept.id]["archived"] is False

    assert await set_workspace_archived(workspace_pool, put_away.id, False)
    assert (await get_workspace(workspace_pool, put_away.id)).archived is False


@pytest.mark.parametrize("icon", [
    "🐙",
    "🏳️‍🌈",                                   # several code points, one glyph
    "data:image/png;base64,iVBORw0KGgo=",
    "data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E",
])
def test_an_icon_may_be_an_emoji_or_an_image(icon):
    assert normalize_icon(icon) == icon


@pytest.mark.parametrize("icon,because", [
    ("https://example.com/logo.png", "a remote address would be fetched on every render"),
    ("data:text/html,<script>", "only images are icons"),
    ("data:image/png,", "a data URL with no data is not an image"),
    ("워크스페이스 이름처럼 긴 글자", "text that long is a label, not an icon"),
    ("x" * 300_000, "a picture that big travels with every listing"),
])
def test_an_icon_that_cannot_be_shown_is_refused(icon, because):
    with pytest.raises(ValueError):
        normalize_icon(icon), because


def test_no_icon_stays_no_icon():
    assert normalize_icon(None) == "" and normalize_icon("  ") == ""


@pytest.mark.asyncio
async def test_an_icon_is_stored_and_listed(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    workspace = await create_workspace(workspace_pool, name="Iconed")

    assert (await get_workspace(workspace_pool, workspace.id)).icon == ""
    assert await set_workspace_icon(workspace_pool, workspace.id, "🛠️")
    assert (await get_workspace(workspace_pool, workspace.id)).icon == "🛠️"

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}
    assert rows[workspace.id]["icon"] == "🛠️"

    # Clearing it is how you take the icon off again.
    assert await set_workspace_icon(workspace_pool, workspace.id, "")
    assert (await get_workspace(workspace_pool, workspace.id)).icon == ""


@pytest.mark.asyncio
async def test_an_unusable_icon_is_never_stored(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    workspace = await create_workspace(workspace_pool, name="Iconed")
    await set_workspace_icon(workspace_pool, workspace.id, "🐙")
    with pytest.raises(ValueError):
        await set_workspace_icon(workspace_pool, workspace.id, "https://example.com/x.png")
    assert (await get_workspace(workspace_pool, workspace.id)).icon == "🐙"


@pytest.mark.asyncio
async def test_archiving_an_unknown_workspace_reports_it(workspace_pool):
    assert await set_workspace_archived(workspace_pool, "no-such-workspace", True) is False


@pytest.mark.asyncio
async def test_ensure_workspace_preserves_archive_state(workspace_pool, tmp_path):
    folder = tmp_path / "archived-ensure"
    folder.mkdir()
    workspace = await create_workspace(workspace_pool, name="Archived ensure", path=str(folder))
    await set_workspace_archived(workspace_pool, workspace.id, True)

    ensured = await ensure_workspace(workspace_pool, workspace.id)

    assert ensured.archived is True
    assert (await get_workspace(workspace_pool, workspace.id)).archived is True


@pytest.mark.asyncio
async def test_forget_archived_workspace_purges_turns_but_keeps_folder(workspace_pool, tmp_path):
    from packages.memory.db import EMBEDDING_DIMENSIONS

    folder = tmp_path / "archived-forget"
    folder.mkdir()
    (folder / "keep.txt").write_text("mine", encoding="utf-8")
    workspace = await create_workspace(workspace_pool, name="Archived forget", path=str(folder))
    await set_workspace_archived(workspace_pool, workspace.id, True)
    async with workspace_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO turns (thread_id, agent, prompt, response, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            (workspace.id, "claude", "q", "a", str([0.0] * EMBEDDING_DIMENSIONS)),
        )

    assert await forget_workspace(workspace_pool, workspace.id)
    async with workspace_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM turns WHERE thread_id = %s", (workspace.id,))
        assert (await cur.fetchone())[0] == 0
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "mine"


@pytest.fixture
def client(workspace_pool, postgres_dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))
    with TestClient(app) as test_client:
        yield test_client


def test_api_create_list_rename_delete(client):
    created = client.post("/workspaces", json={"name": "API Project"}).json()
    assert created["id"] == "api-project"

    prompt_update = client.patch(
        "/workspaces/api-project", json={"system_prompt": "Use Korean"}
    )
    assert prompt_update.status_code == 200
    assert prompt_update.json()["system_prompt"] == "Use Korean"

    listed = client.get("/workspaces").json()["workspaces"]
    assert [w["id"] for w in listed] == ["api-project"]

    assert client.patch("/workspaces/api-project", json={"name": "Renamed"}).status_code == 200
    assert client.get("/workspaces").json()["workspaces"][0]["name"] == "Renamed"

    assert client.patch("/workspaces/api-project", json={"icon": "📦"}).json()["icon"] == "📦"
    # The reason an icon is refused is something the user can act on, so it
    # comes back as a 422 with the reason in it rather than a 500.
    refused = client.patch("/workspaces/api-project", json={"icon": "http://x/y.png"})
    assert refused.status_code == 422 and refused.json()["detail"]
    assert client.get("/workspaces").json()["workspaces"][0]["icon"] == "📦"

    assert client.delete("/workspaces/api-project").status_code == 200
    assert client.get("/workspaces").json()["workspaces"] == []


def test_api_archive_round_trip_preserves_workspace_fields_and_turn_count(client, workspace_pool, tmp_path):
    from packages.memory.db import EMBEDDING_DIMENSIONS

    folder = tmp_path / "archived-folder"
    folder.mkdir()
    created = client.post("/workspaces", json={
        "name": "Archived Project", "path": str(folder),
    }).json()
    workspace_id = created["id"]
    assert client.patch(f"/workspaces/{workspace_id}", json={
        "system_prompt": "Keep context",
    }).status_code == 200

    async def add_turn():
        async with workspace_pool.connection() as conn:
            await conn.execute(
                "INSERT INTO turns (thread_id, agent, prompt, response, embedding) "
                "VALUES (%s, %s, %s, %s, %s)",
                (workspace_id, "claude", "q", "a", str([0.0] * EMBEDDING_DIMENSIONS)),
            )

    asyncio.run(add_turn())
    before = next(row for row in client.get("/workspaces").json()["workspaces"] if row["id"] == workspace_id)
    assert before["turn_count"] == 1

    for archived in (True, False):
        response = client.patch(f"/workspaces/{workspace_id}", json={"archived": archived})
        assert response.status_code == 200
        row = response.json()
        listed = next(row for row in client.get("/workspaces").json()["workspaces"] if row["id"] == workspace_id)
        for actual in (row, listed):
            assert actual["archived"] is archived
            assert (actual["name"], actual["path"], actual["system_prompt"]) == (
                before["name"], before["path"], "Keep context"
            )
        assert listed["turn_count"] == 1


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
async def test_ensure_self_workspace_preserves_archive_state(workspace_pool, tmp_path, monkeypatch):
    repo = tmp_path / "ana-self"
    repo.mkdir()
    monkeypatch.setenv("ANA_SELF_WORKSPACE", str(repo))
    monkeypatch.setenv("ANA_WORKSPACE_GIT_INIT", "off")
    await ensure_self_workspace(workspace_pool)
    await set_workspace_archived(workspace_pool, SELF_WORKSPACE_ID, True)

    ensured = await ensure_self_workspace(workspace_pool)

    assert ensured.archived is True
    assert (await get_workspace(workspace_pool, SELF_WORKSPACE_ID)).archived is True


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


@pytest.mark.asyncio
async def test_web_page_paths_are_reserved_for_session_urls(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    created = await create_workspace(workspace_pool, name="settings")

    assert created.id == "settings-2"


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


@pytest.mark.asyncio
async def test_a_plain_workspace_has_no_parent_and_no_branch(workspace_pool, tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(tmp_path / "ws"))

    workspace = await create_workspace(workspace_pool, name="proj")

    assert workspace.parent_id is None
    assert workspace.branch == ""
    assert workspace.base_branch == ""
    assert (await get_workspace(workspace_pool, workspace.id)).parent_id is None


@pytest.mark.asyncio
async def test_a_branch_row_records_its_parent_and_git_branch(workspace_pool, tmp_path):
    project = await create_workspace(workspace_pool, name="proj", path=str(tmp_path / "proj"))

    child = await create_workspace(
        workspace_pool,
        name="auth",
        path=str(tmp_path / "proj" / ".ana" / "branches" / "auth"),
        parent_id=project.id,
        branch="ana/branch/auth",
        base_branch="main",
    )

    stored = await get_workspace(workspace_pool, child.id)
    assert stored.parent_id == project.id
    assert stored.branch == "ana/branch/auth"
    assert stored.base_branch == "main"
    assert stored.as_dict()["parent_id"] == project.id


@pytest.mark.asyncio
async def test_listing_carries_parent_and_branch(workspace_pool, tmp_path):
    project = await create_workspace(workspace_pool, name="proj", path=str(tmp_path / "proj"))
    await create_workspace(
        workspace_pool,
        name="auth",
        path=str(tmp_path / "proj" / "x"),
        parent_id=project.id,
        branch="ana/branch/auth",
        base_branch="main",
    )

    rows = {row["id"]: row for row in await list_workspaces(workspace_pool)}

    assert rows[project.id]["parent_id"] is None
    branch_rows = [r for r in rows.values() if r["parent_id"] == project.id]
    assert branch_rows[0]["branch"] == "ana/branch/auth"
    assert branch_rows[0]["base_branch"] == "main"


@pytest.mark.asyncio
async def test_list_branches_returns_only_this_projects_branches(workspace_pool, tmp_path):
    one = await create_workspace(workspace_pool, name="one", path=str(tmp_path / "one"))
    two = await create_workspace(workspace_pool, name="two", path=str(tmp_path / "two"))
    await create_workspace(
        workspace_pool, name="a", path=str(tmp_path / "one" / "a"),
        parent_id=one.id, branch="ana/branch/a", base_branch="main",
    )
    await create_workspace(
        workspace_pool, name="b", path=str(tmp_path / "two" / "b"),
        parent_id=two.id, branch="ana/branch/b", base_branch="main",
    )

    assert [w.name for w in await list_branches(workspace_pool, one.id)] == ["a"]


@pytest.mark.asyncio
async def test_a_caller_can_supply_the_id_base(workspace_pool, tmp_path):
    project = await create_workspace(workspace_pool, name="proj", path=str(tmp_path / "proj"))

    child = await create_workspace(
        workspace_pool,
        name="인증",
        path=str(tmp_path / "proj" / "x"),
        parent_id=project.id,
        branch="ana/branch/auth",
        base_branch="main",
        workspace_id=f"{project.id}--auth",
    )

    # Without this, slugify("인증") keeps nothing ASCII, so every branch of
    # every project would be fighting for the id "workspace".
    assert child.id == "proj--auth"
