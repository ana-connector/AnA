"""The tick that turns a schedule into a turn.

One task, one minute apart. It asks the store what is due, opens each job's
conversation the same way a browser would, and submits the prompt. Nothing
about the turn is special after that: it runs on the same runner, through the
same gate, into the same transcript, and whoever opens the workspace next sees
what happened.

Deliberately not a thread per job: a schedule that fires while its own previous
run is still going is skipped rather than queued, because two copies of "clean
up the branch" running at once is how a scheduled job becomes an incident.
"""
import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from packages.orchestrator.schedules import due_schedules, record_run

logger = logging.getLogger(__name__)

# The resolution the expressions have. A shorter tick would only re-ask the
# same question; a longer one would make "every minute" a lie.
TICK_SECONDS = 60.0


async def run_due(app, now: datetime | None = None) -> list[str]:
    """Runs whatever is due. Returns the ids it started, for the tests."""
    pool = app.state.pool
    started: list[str] = []
    try:
        due = await due_schedules(pool, now=now or datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - a database blip is not worth a crash loop
        logger.warning("could not read due schedules", exc_info=True)
        return started

    # Imported here rather than at module import: websocket.py pulls in the
    # adapters and the MCP wiring, and the scheduler is started from the same
    # lifespan that builds those.
    from apps.server.app.websocket import ensure_runner

    for schedule in due:
        try:
            runner = await ensure_runner(app, schedule.workspace_id)
            if runner.busy:
                # Still working on something. Leave next_run_at alone so the
                # next tick tries again rather than silently skipping a slot.
                logger.info("schedule %s skipped: conversation busy", schedule.id)
                continue
            await runner.submit(schedule.prompt, "auto", True)
            started.append(schedule.id)
            await record_run(pool, schedule.id, ok=True, detail="")
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            logger.warning("schedule %s failed", schedule.id, exc_info=True)
            with contextlib.suppress(Exception):
                await record_run(pool, schedule.id, ok=False, detail=str(exc))
    return started


async def scheduler_loop(app) -> None:
    """Forever, once a minute, until the task is cancelled."""
    while True:
        try:
            await run_due(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop outlives any one failure
            logger.warning("scheduler tick failed", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)


def start(app) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(app), name="ana-scheduler")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
