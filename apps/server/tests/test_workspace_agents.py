"""The branch agent index includes every saved worker with its turn identity."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from apps.server.app import main


@pytest.mark.asyncio
async def test_agent_index_returns_lightweight_tasks_from_all_turns(monkeypatch):
    async def get_workspace(_pool, _id):
        return SimpleNamespace(id="branch")

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, query, params):
            assert "meta->'subagents'" in query
            assert params == ("branch",)
            return self

        async def fetchall(self):
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            return [
                (5, now, [{"id": "t1", "title": "New", "agent": "codex", "status": "done", "blocks": [{"text": "large"}]}]),
                (2, now, [{"id": "t1", "title": "Old", "agent": "claude", "status": "done"}]),
            ]

    class Pool:
        def connection(self):
            return Connection()

    monkeypatch.setattr(main, "get_workspace", get_workspace)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool())))
    result = await main.workspace_agents(request, "branch")
    assert [(agent["turn_id"], agent["title"]) for agent in result["agents"]] == [(5, "New"), (2, "Old")]
    assert "blocks" not in result["agents"][0]
