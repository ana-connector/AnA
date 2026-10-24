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
    assert names == {
        "git", "postgres", "github", "ask", "schedule", "delegate", "branch", "figma",
    }
    # Branch management belongs to the lead: a worker runs inside one task of
    # one turn, and two workers of a fan-out creating branches is how a
    # project grows checkouts nobody asked for.
    assert "branch" in {entry["name"] for entry in body["inactive"]}
    # Figma needs a token registered first, so it is listed with that reason
    # rather than failing at the agent's first call.
    assert "figma" in {entry["name"] for entry in body["inactive"]}
    # Routed delegation belongs to the lead; workers cannot recursively
    # launch another routed worker through this MCP server.
    assert "delegate" in {entry["name"] for entry in body["inactive"]}
    # Every inactive server says why, so the settings page never shows a
    # server that is simply missing with no explanation.
    assert all(entry["reason"] for entry in body["inactive"])


def test_the_lead_has_routed_delegation_but_github_stays_worker_only(client):
    body = client.get("/mcp/servers?role=lead").json()

    assert set(entry["name"] for entry in body["active"]) == {
        "git", "postgres", "ask", "schedule", "delegate", "branch",
    }
    assert "github" in {entry["name"] for entry in body["inactive"]}
    # Figma is a lead server too, but no token is registered in the test
    # environment, so it is inactive with that as its reason.
    assert "figma" in {entry["name"] for entry in body["inactive"]}


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
