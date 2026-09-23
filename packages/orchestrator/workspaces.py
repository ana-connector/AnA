"""Workspaces: a folder, and the one lead agent that works in it.

The unit people actually manage is a workspace, not a chat session — "the
folder I am working in" is what has an identity worth naming, and a lead agent
belongs to it. So a workspace owns exactly one ongoing conversation: its id is
the LangGraph thread id, which means the CLI sessions, the checkpointed state,
the turn history and the directory all line up behind one name.

Registering a workspace never creates the folder's contents, and deleting one
never deletes the folder: ANA manages the pointer, the user owns the files.
"""
import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from packages.governance.workspace import session_workspace, workspace_root

WORKSPACE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per-workspace container settings (see docs/plans/2026-09-23-workspace-containers.md).
-- Added after the table shipped, so these are ALTERs rather than columns above.
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS container_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS container_image TEXT;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS container_ports JSONB NOT NULL DEFAULT '[]'::jsonb;
"""

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class WorkspacePathError(ValueError):
    """The requested folder cannot be created or reached from the server."""


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    path: str
    container_enabled: bool = False
    container_image: Optional[str] = None
    container_ports: tuple = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "container_enabled": self.container_enabled,
            "container_image": self.container_image,
            "container_ports": list(self.container_ports),
        }


def slugify(name: str, fallback: str = "workspace") -> str:
    """A folder- and URL-safe id from a human name.

    Ids end up as a directory name and as a thread id in the database, so the
    same sanitization the session directories already use applies here."""
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-._")
    return (slug or fallback)[:48]


async def run_workspace_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(WORKSPACE_MIGRATIONS)


async def _unique_id(pool: AsyncConnectionPool, base: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM workspaces WHERE id LIKE %s", (f"{base}%",))
        taken = {row[0] for row in await cur.fetchall()}
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"cannot find a free id for {base!r}")


async def create_workspace(
    pool: AsyncConnectionPool, *, name: str, path: Optional[str] = None
) -> Workspace:
    """Registers a workspace. `path` defaults to a folder named after it under
    ANA_WORKSPACE_ROOT; an explicit path is used as given, so an existing
    project directory can be adopted."""
    workspace_id = await _unique_id(pool, slugify(name))
    resolved = (
        str(Path(path).expanduser())
        if path
        else str(session_workspace(workspace_root(), workspace_id, create=False))
    )
    # Created if missing, never emptied: adopting an existing project must not
    # touch what is already in it.
    try:
        Path(resolved).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # The server only sees what is mounted into it. A path outside those
        # mounts fails here with "permission denied" on a root-owned parent,
        # and used to surface as a bare 500 — which reads as "ANA is broken"
        # rather than "that folder is not reachable from the server".
        raise WorkspacePathError(
            f"{resolved!r} 폴더를 만들 수 없다 ({exc.strerror or exc}). "
            f"서버가 볼 수 있는 경로만 쓸 수 있다 — 기본 루트는 {workspace_root()} 이고, "
            "그 밖의 경로를 쓰려면 docker-compose.yml에 해당 디렉토리를 마운트해야 한다."
        ) from exc

    await _ensure_git(resolved)

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO workspaces (id, name, path) VALUES (%s, %s, %s)",
            (workspace_id, name.strip() or workspace_id, resolved),
        )
    return Workspace(id=workspace_id, name=name.strip() or workspace_id, path=resolved)


async def _ensure_git(path: str) -> bool:
    """Makes a new workspace a git repository, with one empty commit.

    Not decoration: sub-agent isolation is a `git worktree` per task
    (packages/orchestrator/worktrees.py), and a plain directory has nothing to
    branch from — so without this, every worker of every turn would still
    share one tree. An adopted directory that is already inside a repository
    is left alone, and so is one where git is unavailable or refuses.

    ANA_WORKSPACE_GIT_INIT=off turns it off."""
    if os.environ.get("ANA_WORKSPACE_GIT_INIT", "on").lower() == "off":
        return False
    if shutil.which("git") is None:
        return False

    async def git(*args: str) -> int:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", *args, cwd=path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except OSError:
            return 1
        try:
            return await asyncio.wait_for(process.wait(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            return 1

    if await git("rev-parse", "--is-inside-work-tree") == 0:
        return False  # already someone's repository: not ANA's to restructure
    if await git("init", "-b", "main") != 0:
        return False
    # A worktree needs a commit to branch from, and a fresh repository has
    # none. The identity is set per command rather than written into the
    # repo's config, which belongs to whoever works in it.
    await git(
        "-c", "user.name=ANA", "-c", "user.email=ana@localhost",
        "commit", "--allow-empty", "-m", "ANA: workspace created",
    )
    return True


def _row_to_workspace(row) -> Workspace:
    return Workspace(
        id=row[0],
        name=row[1],
        path=row[2],
        container_enabled=bool(row[3]),
        container_image=row[4],
        container_ports=tuple(row[5] or ()),
    )


async def get_workspace(pool: AsyncConnectionPool, workspace_id: str) -> Optional[Workspace]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, name, path, container_enabled, container_image, container_ports
            FROM workspaces WHERE id = %s
            """,
            (workspace_id,),
        )
        row = await cur.fetchone()
    return _row_to_workspace(row) if row else None


async def set_container_settings(
    pool: AsyncConnectionPool,
    workspace_id: str,
    *,
    enabled: bool,
    image: Optional[str],
    ports: list,
) -> bool:
    """Stores the container settings. Nothing starts here: the container comes
    up when the workspace is next used, so editing settings cannot leave a
    half-started container behind."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE workspaces
            SET container_enabled = %s, container_image = %s, container_ports = %s::jsonb
            WHERE id = %s
            """,
            (enabled, image or None, json.dumps(ports or []), workspace_id),
        )
        return cur.rowcount > 0


async def ensure_workspace(pool: AsyncConnectionPool, workspace_id: str) -> Workspace:
    """Returns the workspace, registering one if this id is new.

    Conversations created before workspaces existed (and any id a client
    invents) still have a directory on disk under the root, so they are
    adopted rather than rejected — an unknown id is not an error, it is an
    unregistered workspace."""
    existing = await get_workspace(pool, workspace_id)
    if existing is not None:
        await touch_workspace(pool, workspace_id)
        return existing

    path = str(session_workspace(workspace_root(), workspace_id, create=True))
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO workspaces (id, name, path) VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET last_used_at = now()
            """,
            (workspace_id, workspace_id, path),
        )
    return Workspace(id=workspace_id, name=workspace_id, path=path)


async def touch_workspace(pool: AsyncConnectionPool, workspace_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE workspaces SET last_used_at = now() WHERE id = %s", (workspace_id,)
        )


async def rename_workspace(pool: AsyncConnectionPool, workspace_id: str, name: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE workspaces SET name = %s WHERE id = %s", (name.strip(), workspace_id)
        )
        return cur.rowcount > 0


async def forget_workspace(
    pool: AsyncConnectionPool, workspace_id: str, *, purge: bool = True
) -> bool:
    """Removes the workspace.

    The folder and its contents are always left alone — ANA manages the
    pointer, the user owns the files. With `purge` (the default) the
    *conversation* goes too: its turns and its LangGraph checkpoints, so
    reopening the same id starts empty instead of resurrecting a workspace the
    user just deleted. The audit log is deliberately not touched: it is
    append-only, and "what this agent did to my machine" outlives the
    conversation that caused it."""
    async with pool.connection() as conn:
        if purge:
            await conn.execute("DELETE FROM turns WHERE thread_id = %s", (workspace_id,))
            # LangGraph's own tables. Named explicitly rather than cascaded:
            # they belong to the checkpointer, and a missing one (an older
            # schema) must not fail the delete. `checkpoint_migrations` is not
            # in this list on purpose: it is the checkpointer's own schema
            # version table and has no thread_id — deleting from it raised,
            # and the rollback that caught the error took the *turns* delete
            # down with it, so purging quietly removed nothing but the pointer.
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                try:
                    # A savepoint, so a table this checkpointer version does
                    # not have cannot discard the deletes that already ran.
                    async with conn.transaction():
                        await conn.execute(
                            f"DELETE FROM {table} WHERE thread_id = %s", (workspace_id,)
                        )
                except Exception:  # noqa: BLE001 - table may not exist here
                    pass
        cur = await conn.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
        return cur.rowcount > 0


async def list_workspaces(pool: AsyncConnectionPool, *, limit: int = 500) -> list[dict]:
    """Every workspace with its conversation's activity, ordered by name.

    By name rather than by recency: this is a list of the projects you work on,
    and one that reshuffles itself after every turn is hard to navigate. The
    turn counts come from `turns` keyed by the workspace id, because the
    workspace *is* the thread."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT w.id, w.name, w.path, w.last_used_at,
                   w.container_enabled, w.container_image, w.container_ports,
                   COUNT(t.id) AS turn_count,
                   MAX(t.created_at) AS last_turn_at,
                   (ARRAY_AGG(t.agent ORDER BY t.created_at DESC, t.id DESC)
                    FILTER (WHERE t.agent IS NOT NULL))[1] AS last_agent
            FROM workspaces w
            LEFT JOIN turns t ON t.thread_id = w.id
            GROUP BY w.id, w.name, w.path, w.last_used_at,
                     w.container_enabled, w.container_image, w.container_ports
            ORDER BY lower(w.name), w.id
            LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "path": r[2],
            "last_used_at": r[3].isoformat(),
            "container_enabled": bool(r[4]),
            "container_image": r[5],
            "container_ports": list(r[6] or ()),
            "turn_count": r[7],
            "last_turn_at": r[8].isoformat() if r[8] else None,
            "last_agent": r[9],
        }
        for r in rows
    ]
