"""File browsing stays inside a branch checkout and saves against a revision."""

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.server.app import workspace_files


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text("note\n", encoding="utf-8")

    async def get_workspace(_pool, _id):
        return SimpleNamespace(path=str(root))

    monkeypatch.setattr(workspace_files, "get_workspace", get_workspace)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=None)))
    return root, request


@pytest.mark.asyncio
async def test_tree_read_save_and_conflict(checkout):
    root, request = checkout
    tree = await workspace_files.list_files(request, "branch")
    assert [entry["name"] for entry in tree["entries"]] == ["docs", "README.md"]
    assert tree["git"] is True
    assert tree["entries"][1]["git_status"] == "??"

    opened = await workspace_files.read_file(request, "branch", "README.md")
    assert opened["content"] == "hello\n"
    saved = await workspace_files.write_file(
        request, "branch", "README.md",
        workspace_files.FileUpdate(content="edited\n", revision=opened["revision"]),
    )
    assert saved["revision"] == hashlib.sha256(b"edited\n").hexdigest()
    assert (root / "README.md").read_text() == "edited\n"
    with pytest.raises(HTTPException) as error:
        await workspace_files.write_file(
            request, "branch", "README.md",
            workspace_files.FileUpdate(content="stale", revision=opened["revision"]),
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_parent_git_and_symlink_paths_are_rejected(checkout, tmp_path):
    root, request = checkout
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (root / "link.txt").symlink_to(secret)
    for path in ("../secret.txt", ".git/config", "link.txt", "/etc/passwd"):
        with pytest.raises(HTTPException) as error:
            await workspace_files.read_file(request, "branch", path)
        assert error.value.status_code == 400
    assert "link.txt" not in [item["name"] for item in (await workspace_files.list_files(request, "branch"))["entries"]]


@pytest.mark.asyncio
async def test_image_preview_only_serves_supported_files(checkout):
    root, request = checkout
    (root / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    response = await workspace_files.preview_file(request, "branch", "icon.png")
    assert response.media_type == "image/png"
    assert response.headers["content-security-policy"] == "sandbox"
    with pytest.raises(HTTPException) as error:
        await workspace_files.preview_file(request, "branch", "README.md")
    assert error.value.status_code == 415


@pytest.mark.asyncio
async def test_gitignore_marks_ignored_files_and_nested_entries(checkout):
    root, request = checkout
    (root / ".gitignore").write_text("secret.txt\ncache/\n", encoding="utf-8")
    (root / "secret.txt").write_text("hidden", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "inside.txt").write_text("hidden", encoding="utf-8")
    tree = await workspace_files.list_files(request, "branch")
    entries = {entry["name"]: entry for entry in tree["entries"]}
    assert entries["secret.txt"]["git_status"] == "!!"
    assert entries["secret.txt"]["git_ignore_rule"] == "secret.txt"
    assert entries["cache"]["git_status"] == "!!"
    nested = await workspace_files.list_files(request, "branch", "cache")
    assert nested["entries"][0]["git_status"] == "!!"
    assert tree["summary"]["untracked"] >= 1


@pytest.mark.asyncio
async def test_git_summary_distinguishes_staged_and_worktree_changes(checkout):
    root, request = checkout
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    (root / "README.md").write_text("changed after staging\n", encoding="utf-8")
    tree = await workspace_files.list_files(request, "branch")
    readme = next(entry for entry in tree["entries"] if entry["name"] == "README.md")
    assert readme["git_status"] == "AM"
    assert tree["summary"]["staged"] == 1
    assert tree["summary"]["modified"] == 1
