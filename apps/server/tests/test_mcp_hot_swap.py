"""An existing conversation picks up secret-gated MCP servers on its next turn."""

import asyncio
from types import SimpleNamespace

import pytest

from apps.server.app import websocket
from packages.governance.secrets import Secret
from packages.governance.session import GateRegistry


class FakeAdapter:
    def __init__(self, env):
        self.env = dict(env)
        self.command_prefix = []
        self.model = None
        self.effort = None
        self._process = None

    async def terminate(self):
        return None


class FakeGraph:
    def __init__(self):
        self.cursor_envs = []

    async def aget_state(self, _config):
        return SimpleNamespace(values={})

    async def astream(self, _state, config=None, stream_mode=None):
        self.cursor_envs.append(dict(config["configurable"]["adapters"]["cursor"].env))
        yield {"type": "result", "text": "", "agent": "cursor"}


@pytest.mark.asyncio
async def test_secret_addition_and_removal_hot_swap_an_existing_runner(monkeypatch):
    current_secrets = []

    async def load(_pool, _cipher):
        return list(current_secrets)

    monkeypatch.setattr(websocket, "load_secrets", load)
    monkeypatch.setattr(websocket, "enabled_agents", lambda: ["cursor"])
    monkeypatch.setattr(websocket, "build_adapter", lambda _token, **kwargs: FakeAdapter(kwargs["extra_env"]))
    monkeypatch.setattr(websocket, "build_cursor_adapter", lambda _token, **kwargs: FakeAdapter(kwargs["extra_env"]))
    monkeypatch.setattr(websocket, "build_transcript", lambda: None)
    graph = FakeGraph()
    state = SimpleNamespace(
        gate_registry=GateRegistry(), pool=object(), secret_cipher=object(),
        graph=graph, tracer=None, usage_tracker=None, embedding_model=None,
    )
    runner = await websocket._build_runner(
        SimpleNamespace(state=state), thread_id="hot-swap", workspace_dir="/tmp",
        workspace_name="test", secret_env={}, registered_secrets=[],
        container_prefix=None, container_spec=None, container_error="",
    )
    try:
        async def turn(expected: int):
            await runner.submit("check", "cursor", False)
            async with asyncio.timeout(2):
                while runner.busy or len(graph.cursor_envs) < expected:
                    await asyncio.sleep(0.01)

        await turn(1)
        assert "FIGMA_API_KEY" not in graph.cursor_envs[-1]

        current_secrets.append(Secret("global", "FIGMA_API_KEY", "test-token"))
        await turn(2)
        assert graph.cursor_envs[-1]["FIGMA_API_KEY"] == "test-token"

        current_secrets.clear()
        await turn(3)
        assert "FIGMA_API_KEY" not in graph.cursor_envs[-1]
    finally:
        await runner.close()
