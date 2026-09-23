"""Append-only audit log for gate decisions.

Every tool call that reaches the gate is recorded before anything else
happens — including before the user is asked — so a denied, approved or
timed-out call all leave the same trail. Append-only is enforced by the
database (see AUDIT_MIGRATIONS), not by application convention: code that
could delete its own audit trail is not an audit trail.
"""
import json
from typing import Optional

from psycopg_pool import AsyncConnectionPool

AUDIT_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    thread_id TEXT,
    agent TEXT,
    cli_session_id TEXT,
    tool_name TEXT NOT NULL,
    tool_input JSONB NOT NULL,
    rule_id TEXT,
    category TEXT,
    verdict TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS audit_events_thread_idx ON audit_events (thread_id, created_at DESC);
-- The CLI's id for one tool call. A call that needs approval writes a
-- "pending" row and then its decision, and this is what ties the two together
-- so a reader can show one call instead of two.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS tool_use_id TEXT;

CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();

-- TRUNCATE is not an UPDATE or DELETE and would otherwise walk straight past
-- the row-level trigger above, emptying the log in one statement.
DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events;
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();
"""


async def run_audit_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(AUDIT_MIGRATIONS)


async def record_event(
    pool: AsyncConnectionPool,
    *,
    thread_id: Optional[str],
    agent: Optional[str],
    cli_session_id: Optional[str],
    tool_name: str,
    tool_input: dict,
    verdict: str,
    decided_by: str,
    tool_use_id: Optional[str] = None,
    rule_id: Optional[str] = None,
    category: Optional[str] = None,
    reason: str = "",
) -> int:
    """Appends one row and returns its id.

    `decided_by` says where the verdict came from: "rule" (matched the rule
    set), "user" (answered the approval prompt), "timeout" (nobody answered)
    or "error" (the gate itself failed, which always denies)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO audit_events
                (thread_id, agent, cli_session_id, tool_name, tool_input,
                 rule_id, category, verdict, decided_by, reason, tool_use_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                thread_id, agent, cli_session_id, tool_name,
                json.dumps(tool_input, ensure_ascii=False),
                rule_id, category, verdict, decided_by, reason, tool_use_id,
            ),
        )
        row = await cur.fetchone()
    return row[0]


async def recent_events(
    pool: AsyncConnectionPool, *, thread_id: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Newest-first audit rows, for the UI and for after-the-fact review."""
    query = """
        SELECT id, created_at, thread_id, agent, tool_name, tool_input,
               rule_id, category, verdict, decided_by, reason, tool_use_id
        FROM audit_events
    """
    params: tuple = ()
    if thread_id is not None:
        query += " WHERE thread_id = %s"
        params = (thread_id,)
    query += " ORDER BY created_at DESC, id DESC LIMIT %s"
    params = params + (limit,)

    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "created_at": r[1].isoformat(),
            "thread_id": r[2],
            "agent": r[3],
            "tool_name": r[4],
            "tool_input": r[5],
            "rule_id": r[6],
            "category": r[7],
            "verdict": r[8],
            "decided_by": r[9],
            "reason": r[10],
            "tool_use_id": r[11],
        }
        for r in rows
    ]
