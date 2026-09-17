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
