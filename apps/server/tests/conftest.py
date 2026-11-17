import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

# The only database these tests may ever touch. Checked against the live
# connection before any migration runs: a DSN that points somewhere else would
# run migrations and TRUNCATE against it.
TEST_DB_NAME = "ana_test"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _own_container_network() -> str | None:
    """The Docker network of the container these tests run in, if any.

    The suite's Docker client talks to a daemon that is usually *not* in this
    process's network namespace — under Compose it is the host's. A published
    port therefore lands on the host, where `localhost` means something else
    than it does here, which is why connecting to a published port failed from
    inside the server container. Sharing a network instead means the test
    database is reachable at its own address and published nowhere at all."""
    if not Path("/.dockerenv").exists():
        return None
    result = _docker(
        "inspect", socket.gethostname(),
        "--format", "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}\n{{end}}",
    )
    if result.returncode != 0:
        # A container the daemon does not know (different daemon, or the
        # hostname is not the container id): fall back to publishing a port.
        return None
    networks = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    # "host" and "none" have no address of their own to dial.
    usable = [name for name in networks if name not in ("host", "none")]
    return usable[0] if usable else None


def _container_address(container_name: str, network: str) -> str:
    result = _docker(
        "inspect", container_name,
        "--format", "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}",
        check=True,
    )
    address = result.stdout.strip()
    if not address:
        raise RuntimeError(f"{container_name} has no address on {network}")
    return address


# Every container this fixture starts carries it, so the ones a killed run
# left behind can be found without guessing from the name.
TEST_CONTAINER_LABEL = "ana-test-pg=1"
# How long a labelled container is left alone. It has to outlast the slowest
# legitimate run (the full suite is ~160s) by enough that a concurrent pytest
# is never swept, while still being short enough that yesterday's leak is gone.
STALE_CONTAINER_SECONDS = 30 * 60


def _sweep_stale_test_containers() -> list[str]:
    """Removes test databases that an earlier run could not clean up.

    The fixture's own `finally` handles every ordinary exit, but not a SIGKILL:
    a run killed by a `timeout` that was set too low, or by the OOM killer,
    leaves its container behind, and five of them accumulated that way. So the
    leak is also collected from this side, by age, because a container younger
    than the cut-off may belong to a pytest running right now in another
    terminal -- the suite is run that way constantly.
    """
    listed = _docker(
        "ps", "-aq", "--filter", f"label={TEST_CONTAINER_LABEL}",
        "--format", "{{.ID}} {{.CreatedAt}}",
    )
    if listed.returncode != 0:
        return []
    removed: list[str] = []
    for line in listed.stdout.splitlines():
        container_id = line.split(" ", 1)[0].strip()
        if not container_id:
            continue
        started = _docker("inspect", "-f", "{{.State.StartedAt}}", container_id).stdout.strip()
        if _age_seconds(started) < STALE_CONTAINER_SECONDS:
            continue
        if _docker("rm", "-fv", container_id).returncode == 0:
            removed.append(container_id)
    return removed


def _age_seconds(started_at: str) -> float:
    """Seconds since a Docker timestamp, or 0.0 when it cannot be read.

    0.0 means "too young to sweep": an unreadable timestamp must not be the
    reason a running test's database is removed."""
    from datetime import datetime, timezone

    import re

    text = started_at.strip()
    if not text or text.startswith("0001-01-01"):
        return 0.0
    # Docker prints nanoseconds; fromisoformat takes at most microseconds. The
    # zone has to survive the trim -- dropping a "+09:00" would make a
    # container that started a minute ago look nine hours old, and sweeping a
    # database out from under a running test is the one failure this must not
    # have.
    match = re.match(r"^(.*?)\.(\d+)(.*)$", text)
    if match:
        text = f"{match.group(1)}.{match.group(2)[:6]}{match.group(3)}"
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def _container_running(container_name: str) -> bool:
    return _docker("inspect", "-f", "{{.State.Running}}", container_name).stdout.strip() == "true"


def _wait_until_reachable(
    dsn: str, container_name: str | None = None, timeout: float = 60.0
) -> None:
    """Waits for a connection this process can actually open.

    `pg_isready` inside the container only says the server started; it says
    nothing about whether the test process can reach it. Postgres also restarts
    once during first-time initialization, so an early success is not enough
    either — this is the check that matters, so it is the one that gates."""
    import psycopg

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                database = conn.execute("SELECT current_database()").fetchone()[0]
            if database != TEST_DB_NAME:
                raise RuntimeError(
                    f"refusing to run tests against {database!r}; expected {TEST_DB_NAME!r}"
                )
            return
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - not up yet, or not reachable yet
            last_error = exc
            # Waiting out the full timeout on a container that already died
            # reports "timed out" for what is really a startup failure.
            if container_name and not _container_running(container_name):
                logs = _docker("logs", "--tail", "20", container_name).stderr
                raise RuntimeError(f"{container_name} exited before accepting connections:\n{logs}")
            time.sleep(0.5)
    raise TimeoutError(
        f"could not reach the test database at {dsn.split('@')[-1]} in {timeout:.0f}s: {last_error}"
    )


def pytest_collection_modifyitems(config, items):
    """Marks every test that needs Postgres `db`, by what it asks for.

    Half the suite (411 of 801 tests) takes the database fixture, and that one
    container is most of what makes a full run take minutes. Marking by
    fixture rather than by hand means `-m "not db"` stays honest as tests are
    added: a test that starts using `db_pool` becomes a database test without
    anybody remembering to say so."""
    for item in items:
        if {"db_pool", "postgres_dsn"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def postgres_dsn():
    """Spins up a throwaway pgvector-enabled Postgres in Docker for the
    whole test session. Requires a working Docker daemon (already a hard
    dependency of this project's deployment model — see docker-compose.yml).

    ANA_TEST_DATABASE_URL overrides the whole fixture, for a runner that
    cannot reach a Docker daemon. It must still name `ana_test`."""
    override = os.environ.get("ANA_TEST_DATABASE_URL")
    if override:
        _wait_until_reachable(override, container_name="", timeout=10.0)
        yield override
        return

    leaked = _sweep_stale_test_containers()
    if leaked:
        print(f"removed {len(leaked)} leaked test database container(s)")
    container_name = f"ana-test-pg-{uuid.uuid4().hex[:8]}"
    network = _own_container_network()
    if network:
        placement = ["--network", network]
    else:
        # Loopback only: nothing on the machine's other interfaces should be
        # able to reach a database the suite migrates and truncates.
        port = _find_free_port()
        placement = ["-p", f"127.0.0.1:{port}:5432"]
    _docker(
        "run", "-d", "--rm", "--name", container_name,
        "--label", TEST_CONTAINER_LABEL,
        "-e", "POSTGRES_PASSWORD=test",
        "-e", f"POSTGRES_DB={TEST_DB_NAME}",
        *placement,
        "pgvector/pgvector:pg16",
        check=True,
    )
    try:
        host = (
            f"{_container_address(container_name, network)}:5432"
            if network
            else f"127.0.0.1:{port}"
        )
        dsn = f"postgresql://postgres:test@{host}/{TEST_DB_NAME}"
        _wait_until_reachable(dsn, container_name)
        yield dsn
    finally:
        _docker("rm", "-fv", container_name)


@pytest_asyncio.fixture
async def db_pool(postgres_dsn):
    from packages.memory.db import create_pool, run_migrations

    pool = await create_pool(postgres_dsn)
    await run_migrations(pool)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE turns")
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def forget_ambient_ana_config(monkeypatch):
    """Every test starts from ANA's documented defaults, not from wherever it
    is being run.

    The suite is usually run inside ANA's own server container, which has its
    whole configuration in the environment — the gate token, the allowed
    origins, the self-workspace path, the secret key. Tests that assert on a
    default therefore passed or failed depending on the machine: the workspace
    listing came back with `ana-self` in it, an allowed origin the test never
    set was rejected, and a gate token the test expected to be absent was
    inherited. CI would have been a third set of answers again.

    Declared before the fixtures below so they can still set the few variables
    they own; a test that wants a value sets it itself."""
    for name in [name for name in os.environ if name.startswith("ANA_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path_factory, monkeypatch):
    """Every test gets its own ANA state directory.

    ANA_STATE_DIR is where the running system keeps the things a test can
    quietly poison: manually entered usage estimates, the provider-reported
    figures persisted across restarts, the prepared CLI homes, the MCP
    launcher scripts. Without this, a test that records a usage figure writes
    it into the developer's real ~/.ana, where it then steers routing for
    every later test — and for the server running on the same machine. Tests
    that need a specific directory still set the variable themselves; this
    only keeps the default from being the real one."""
    monkeypatch.setenv("ANA_STATE_DIR", str(tmp_path_factory.mktemp("ana-state")))


@pytest.fixture(autouse=True)
def isolated_workspace_root(tmp_path_factory, monkeypatch):
    """Every test's conversations get their directories under tmp.

    A conversation's directory is created for it on first use, under
    `workspace_root()` — which falls back to ANA_WORKSPACE_DIR and then to the
    real /home/hello/workspace. So a suite that opened a socket for thread
    "gate-ws-1" left a `gate-ws-1` folder in the developer's actual workspace,
    and one run of the whole suite left hundreds: every thread id the tests
    invent, and a UUID for every one they do not. Tests that need a particular
    root still set the variable themselves; this only keeps the default from
    being the real one."""
    root = tmp_path_factory.mktemp("ana-workspace")
    monkeypatch.setenv("ANA_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ANA_WORKSPACE_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def forget_rung_cooldowns():
    """Cooldowns are process-wide on purpose (a spent window is spent for every
    worker), which in a test run means one test's refusal would steer another
    test's routing."""
    from packages.orchestrator.cooldown import shared

    shared().clear()
    yield
    shared().clear()
