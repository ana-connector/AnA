import socket
import subprocess
import time
import uuid

import pytest
import pytest_asyncio


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_postgres(container_name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"postgres in {container_name} did not become ready in {timeout}s")


@pytest.fixture(scope="session")
def postgres_dsn():
    """Spins up a throwaway pgvector-enabled Postgres in Docker for the
    whole test session. Requires a working Docker daemon (already a hard
    dependency of this project's deployment model — see docker-compose.yml)."""
    container_name = f"ana-test-pg-{uuid.uuid4().hex[:8]}"
    port = _find_free_port()
    subprocess.run(
        [
            "docker", "run", "-d", "--name", container_name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=ana_test",
            "-p", f"{port}:5432",
            "pgvector/pgvector:pg16",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres:test@localhost:{port}/ana_test"
    try:
        _wait_for_postgres(container_name)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


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
