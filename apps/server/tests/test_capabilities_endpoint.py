"""The read-only capability endpoints: /mcp/servers and /skills."""
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from apps.server.app.main import app
from packages.governance.secrets import run_secret_migrations


@pytest_asyncio.fixture
async def prepared_pool(db_pool):
    await run_secret_migrations(db_pool)
    return db_pool


@pytest.fixture
def client(prepared_pool, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    with TestClient(app) as test_client:
        yield test_client


def test_mcp_servers_reports_active_and_why_not(client):
    body = client.get("/mcp/servers?role=worker").json()

    assert body["role"] == "worker"
    names = {entry["name"] for entry in body["active"]} | {
        entry["name"] for entry in body["inactive"]
    }
    assert names == {"git", "postgres", "github", "ask"}
    # `ask` is the lead's own server, so a worker sees it listed with the reason
    # it does not have it rather than not at all.
    assert "ask" in {entry["name"] for entry in body["inactive"]}
    # Every inactive server says why, so the settings page never shows a
    # server that is simply missing with no explanation.
    assert all(entry["reason"] for entry in body["inactive"])


def test_the_leads_only_mcp_server_is_asking_the_user(client):
    """The lead does no work of its own, so it gets no tooling — except the one
    server that is not tooling: `ask`, which reaches the user."""
    body = client.get("/mcp/servers?role=lead").json()

    assert [entry["name"] for entry in body["active"]] == ["ask"]


def test_skills_lists_the_library_with_schemas(client):
    body = client.get("/skills").json()

    names = {skill["name"] for skill in body["skills"]}
    assert {"code-review", "db-inspect", "git-commit", "github-pr"} <= names
    assert body["errors"] == []
    review = next(skill for skill in body["skills"] if skill["name"] == "code-review")
    assert [field["name"] for field in review["inputs"]] == ["target", "focus"]


def test_skill_detail_and_404(client):
    assert client.get("/skills/git-commit").json()["name"] == "git-commit"
    assert client.get("/skills/no-such-skill").status_code == 404
