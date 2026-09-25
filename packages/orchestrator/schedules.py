"""Turns that start without anybody typing.

A schedule is a prompt, a workspace and a cron expression. When it comes due
the scheduler opens that workspace's conversation — the same one the browser
opens, with the same gate token and CLI sessions — and submits the prompt as if
a person had. The answer lands in the conversation, where it can be read later.

The cron parser is written here rather than taken from a library for the same
reason `ask_user` writes MCP by hand: five fields, a handful of forms each, and
the alternative is a dependency in the server image for sixty lines of code.
What it implements is the classic five-field syntax — minute, hour, day of
month, month, day of week — including cron's own oddity that day-of-month and
day-of-week are a union when both are restricted.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    -- "user" or "agent": a schedule an agent made for itself is worth telling
    -- apart from one a person asked for.
    created_by TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_status TEXT NOT NULL DEFAULT '',
    last_detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules (enabled, next_run_at);
"""

# Bounds per field, in cron's order.
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)

# How far ahead the parser will look for a match before giving up. Four years
# covers a leap day named explicitly ("0 0 29 2 *").
_SEARCH_LIMIT_MINUTES = 366 * 4 * 24 * 60


class CronError(ValueError):
    """The expression is not something this parser can run."""


@dataclass(frozen=True)
class Schedule:
    id: str
    workspace_id: str
    name: str
    cron: str
    prompt: str
    enabled: bool
    created_by: str
    created_at: datetime
    next_run_at: Optional[datetime]
    last_run_at: Optional[datetime]
    last_status: str
    last_detail: str


def _parse_field(raw: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty {name} field")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step in {name}: {step_text!r}")
            step = int(step_text)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start, end = _number(start_text, name, low, high), _number(end_text, name, low, high)
            if start > end:
                raise CronError(f"reversed range in {name}: {part!r}")
        else:
            start = end = _number(part, name, low, high)
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"no values for {name}")
    return values


def _number(text: str, name: str, low: int, high: int) -> int:
    text = text.strip()
    if not text.isdigit():
        raise CronError(f"{name} must be a number: {text!r}")
    value = int(text)
    # Sunday is both 0 and 7 in every cron; accepting only one of them is a
    # schedule that silently never fires.
    if name == "weekday" and value == 7:
        return 0
    if not low <= value <= high:
        raise CronError(f"{name} out of range: {value}")
    return value


def parse_cron(expression: str) -> tuple[set[int], ...]:
    """The five fields as sets of allowed values. Raises CronError."""
    parts = (expression or "").split()
    if len(parts) != 5:
        raise CronError("a cron expression has five fields: minute hour day month weekday")
    fields = []
    for raw, (name, low, high) in zip(parts, _FIELDS):
        # 7 is accepted as Sunday on the way in and folded to 0 on the way out,
        # so "*" is the six-plus-one days and not eight of them -- an eighth
        # value made every weekday field look restricted, and cron's
        # day/weekday union then matched every day of the month.
        values = _parse_field(raw, name, low, 7 if name == "weekday" else high)
        if name == "weekday":
            values = {0 if value == 7 else value for value in values}
        fields.append(values)
    return tuple(fields)


def next_run_at(expression: str, after: Optional[datetime] = None) -> datetime:
    """The first minute strictly after `after` that the expression matches.

    Strictly after, because the tick asks once a minute: answering with the
    current minute would run the same job forever."""
    minutes, hours, days, months, weekdays = parse_cron(expression)
    moment = (after or datetime.now(timezone.utc)).astimezone(timezone.utc)
    moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Whether the two day fields are restricted decides how they combine.
    day_restricted = days != set(range(1, 32))
    weekday_restricted = weekdays != set(range(0, 7))

    for _ in range(_SEARCH_LIMIT_MINUTES):
        if moment.month in months and moment.minute in minutes and moment.hour in hours:
            # Python's Monday=0; cron's Sunday=0.
            weekday = (moment.weekday() + 1) % 7
            day_ok = moment.day in days
            weekday_ok = weekday in weekdays
            matches = (
                (day_ok or weekday_ok)
                if day_restricted and weekday_restricted
                else (day_ok and weekday_ok)
            )
            if matches:
                return moment
        moment += timedelta(minutes=1)
    raise CronError(f"no run time within four years for {expression!r}")


async def run_schedules_migrations(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)


def _row(record) -> Schedule:
    return Schedule(
        id=record[0],
        workspace_id=record[1],
        name=record[2],
        cron=record[3],
        prompt=record[4],
        enabled=bool(record[5]),
        created_by=record[6],
        created_at=record[7],
        next_run_at=record[8],
        last_run_at=record[9],
        last_status=record[10],
        last_detail=record[11],
    )


_COLUMNS = """id, workspace_id, name, cron, prompt, enabled, created_by,
              created_at, next_run_at, last_run_at, last_status, last_detail"""


async def create_schedule(
    pool: AsyncConnectionPool,
    *,
    workspace_id: str,
    name: str,
    cron: str,
    prompt: str,
    created_by: str = "user",
    enabled: bool = True,
) -> Schedule:
    """Stores one schedule. The expression is parsed first, so a typo is a 400
    on the way in rather than a job that never fires."""
    upcoming = next_run_at(cron)
    schedule_id = uuid.uuid4().hex[:12]
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            INSERT INTO schedules
                (id, workspace_id, name, cron, prompt, enabled, created_by, next_run_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (schedule_id, workspace_id, name, cron, prompt, enabled, created_by, upcoming),
        )
        return _row(await cur.fetchone())


async def list_schedules(
    pool: AsyncConnectionPool, workspace_id: Optional[str] = None
) -> list[Schedule]:
    query = f"SELECT {_COLUMNS} FROM schedules"
    params: tuple = ()
    if workspace_id:
        query += " WHERE workspace_id = %s"
        params = (workspace_id,)
    query += " ORDER BY next_run_at NULLS LAST, created_at"
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        return [_row(record) for record in await cur.fetchall()]


async def get_schedule(pool: AsyncConnectionPool, schedule_id: str) -> Optional[Schedule]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM schedules WHERE id = %s", (schedule_id,)
        )
        record = await cur.fetchone()
        return _row(record) if record else None


async def update_schedule(
    pool: AsyncConnectionPool,
    schedule_id: str,
    *,
    name: Optional[str] = None,
    cron: Optional[str] = None,
    prompt: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Optional[Schedule]:
    """Only the fields given. A new expression reschedules immediately, so the
    list never shows a next run the rules no longer produce."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if prompt is not None:
        sets.append("prompt = %s")
        params.append(prompt)
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(enabled)
    if cron is not None:
        upcoming = next_run_at(cron)
        sets += ["cron = %s", "next_run_at = %s"]
        params += [cron, upcoming]
    if not sets:
        return await get_schedule(pool, schedule_id)
    params.append(schedule_id)
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = %s RETURNING {_COLUMNS}",
            tuple(params),
        )
        record = await cur.fetchone()
        return _row(record) if record else None


async def set_enabled(pool: AsyncConnectionPool, schedule_id: str, enabled: bool):
    return await update_schedule(pool, schedule_id, enabled=enabled)


async def delete_schedule(pool: AsyncConnectionPool, schedule_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        return cur.rowcount > 0


async def due_schedules(
    pool: AsyncConnectionPool, now: Optional[datetime] = None
) -> list[Schedule]:
    """Enabled schedules whose next run has passed."""
    moment = now or datetime.now(timezone.utc)
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            SELECT {_COLUMNS} FROM schedules
            WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= %s
            ORDER BY next_run_at
            """,
            (moment,),
        )
        return [_row(record) for record in await cur.fetchall()]


async def record_run(
    pool: AsyncConnectionPool,
    schedule_id: str,
    *,
    ok: bool,
    detail: str = "",
    now: Optional[datetime] = None,
) -> Optional[Schedule]:
    """What happened, and when it goes again.

    A failed run reschedules like any other: a job that could not run once is
    not a job that should stop, and the reason is kept where the list shows
    it."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current = await get_schedule(pool, schedule_id)
    if current is None:
        return None
    try:
        upcoming = next_run_at(current.cron, moment)
    except CronError:
        upcoming = None
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE schedules
               SET last_run_at = %s, last_status = %s, last_detail = %s, next_run_at = %s
             WHERE id = %s
         RETURNING {_COLUMNS}
            """,
            (moment, "ok" if ok else "error", detail[:2000], upcoming, schedule_id),
        )
        record = await cur.fetchone()
        return _row(record) if record else None


def describe(expression: str) -> str:
    """A short human reading of the expression, for a list that should not
    require knowing cron. Falls back to the expression itself."""
    try:
        minutes, hours, days, months, weekdays = parse_cron(expression)
    except CronError:
        return expression
    every_minute = len(minutes) == 60
    every_hour = len(hours) == 24
    if every_minute and every_hour:
        return "매분"
    if every_hour and len(minutes) == 1:
        return f"매시 {next(iter(minutes))}분"
    # Every phrase below says nothing about the month, so a restricted month
    # field has to fall through to the expression rather than be dropped:
    # "0 4 1 1 *" is once a year, not every month.
    if len(hours) == 1 and len(minutes) == 1 and len(months) == 12:
        clock = f"{next(iter(hours)):02d}:{next(iter(minutes)):02d}"
        if len(days) == 31 and len(weekdays) == 7:
            return f"매일 {clock}"
        if len(weekdays) < 7 and len(days) == 31:
            names = "일월화수목금토"
            picked = "".join(names[value] for value in sorted(weekdays))
            return f"{picked}요일 {clock}"
        if len(days) < 31 and len(weekdays) == 7:
            return f"매월 {', '.join(str(d) for d in sorted(days))}일 {clock}"
    return expression
