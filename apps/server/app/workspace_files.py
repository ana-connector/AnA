"""Browse and edit text files inside a workspace checkout."""

import hashlib
import mimetypes
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from packages.orchestrator.workspaces import get_workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/files")
MAX_TEXT_BYTES = 1024 * 1024
_git_status_cache: dict[str, tuple[float, dict[str, str], bool]] = {}


async def _location(request: Request, workspace_id: str, relative: str) -> tuple[Path, Path]:
    workspace = await get_workspace(request.app.state.pool, workspace_id)
    if workspace is None:
        raise HTTPException(404, "workspace not found")
    root = Path(workspace.path).resolve()
    if relative.startswith("/") or "\\" in relative or "\x00" in relative:
        raise HTTPException(400, "invalid file path")
    parts = Path(relative).parts
    if any(part in ("..", ".git") for part in parts):
        raise HTTPException(400, "invalid file path")
    target = root.joinpath(*parts)
    # Refuse symlinks even if they happen to point back into the checkout.
    if any(root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
        raise HTTPException(400, "symbolic links cannot be opened")
    if not target.resolve().is_relative_to(root):
        raise HTTPException(400, "file path escapes workspace")
    return root, target


def _git_changes(root: Path) -> tuple[dict[str, str], bool]:
    key = str(root)
    cached = _git_status_cache.get(key)
    if cached and time.monotonic() - cached[0] < 1.5:
        return cached[1], cached[2]
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root, capture_output=True, timeout=5, check=False,
        )
        if result.returncode:
            return {}, False
        records = result.stdout.split(b"\0")
        changes = {}
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            status = record[:2].decode("ascii", "replace")
            path = record[3:].decode("utf-8", "replace")
            changes[path] = status
            if "R" in status or "C" in status:
                index += 1  # porcelain -z includes the original path next
        _git_status_cache[key] = (time.monotonic(), changes, True)
        if len(_git_status_cache) > 256:
            oldest = min(_git_status_cache, key=lambda path: _git_status_cache[path][0])
            _git_status_cache.pop(oldest, None)
        return changes, True
    except (OSError, subprocess.TimeoutExpired):
        return {}, False


def _git_ignored(root: Path, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", "-z", "--stdin"], cwd=root,
            input=b"\0".join(path.encode("utf-8") for path in paths) + b"\0",
            capture_output=True, timeout=5, check=False,
        )
        fields = result.stdout.split(b"\0")
        return {
            fields[index + 3].decode("utf-8", "replace"): fields[index + 2].decode("utf-8", "replace")
            for index in range(0, len(fields) - 3, 4)
        }
    except (OSError, subprocess.TimeoutExpired):
        return {}


def _git_branch(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=root,
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_summary(changes: dict[str, str]) -> dict[str, int]:
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicts": 0}
    for status in changes.values():
        if "U" in status or status in ("AA", "DD"):
            counts["conflicts"] += 1
        elif status == "??":
            counts["untracked"] += 1
        else:
            if status[0] != " ":
                counts["staged"] += 1
            if status[1] != " ":
                counts["modified"] += 1
    return counts


@router.get("")
async def list_files(request: Request, workspace_id: str, path: str = "") -> dict:
    root, directory = await _location(request, workspace_id, path)
    if not directory.is_dir():
        raise HTTPException(404, "directory not found")
    changes, git = _git_changes(root)
    dirty_dirs = {
        "/".join(name.split("/")[:index])
        for name in changes for index in range(1, len(name.split("/")))
    }
    children = [child for child in directory.iterdir() if child.name != ".git" and not child.is_symlink()]
    ignored = _git_ignored(root, [child.relative_to(root).as_posix() for child in children]) if git else {}
    entries = []
    for child in children:
        relative = child.relative_to(root).as_posix()
        direct = changes.get(relative)
        entries.append({
            "name": child.name, "path": relative, "directory": child.is_dir(),
            "git_status": ("!!" if relative in ignored else direct or ("*" if relative in dirty_dirs else None)),
            "git_ignore_rule": ignored.get(relative),
        })
    entries.sort(key=lambda entry: (not entry["directory"], entry["name"].lower()))
    return {
        "entries": entries, "git": git,
        "branch": _git_branch(root) if git and not path else None,
        "summary": _git_summary(changes),
    }


@router.get("/content")
async def read_file(request: Request, workspace_id: str, path: str) -> dict:
    _, target = await _location(request, workspace_id, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise HTTPException(413, "file is too large to edit")
    with target.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(413, "file is too large to edit")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "binary file cannot be edited") from exc
    if "\x00" in content:
        raise HTTPException(415, "binary file cannot be edited")
    return {"content": content, "revision": hashlib.sha256(data).hexdigest()}


@router.get("/preview")
async def preview_file(request: Request, workspace_id: str, path: str) -> FileResponse:
    _, target = await _location(request, workspace_id, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    mime, _ = mimetypes.guess_type(target.name)
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/svg+xml"}:
        raise HTTPException(415, "preview is not available for this file")
    if target.stat().st_size > 10 * 1024 * 1024:
        raise HTTPException(413, "file is too large to preview")
    return FileResponse(target, media_type=mime, headers={
        "Cache-Control": "no-store", "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff",
    })


class FileUpdate(BaseModel):
    content: str
    revision: str


@router.put("/content")
async def write_file(request: Request, workspace_id: str, path: str, body: FileUpdate) -> dict:
    root, target = await _location(request, workspace_id, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    data = body.content.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(413, "file is too large to edit")
    if target.stat().st_size > MAX_TEXT_BYTES:
        raise HTTPException(413, "file is too large to edit")
    current = target.read_bytes()
    if hashlib.sha256(current).hexdigest() != body.revision:
        raise HTTPException(409, "file changed since it was opened")
    target.write_bytes(data)
    _git_status_cache.pop(str(root), None)
    return {"revision": hashlib.sha256(data).hexdigest()}
