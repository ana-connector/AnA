"""Lead-triggered workers use the worker ladder, including provider failover."""
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.governance.secrets import Secret
from apps.server.app.delegation import run_routed_task


class FakeUsage:
    def __init__(self):
        self.recorded = []

    def snapshot(self):
        return {}

    def record_tokens(self, agent, tokens, usd=0.0):
        self.recorded.append((agent, tokens, usd))


class FakeWorker(AgentAdapter):
    def __init__(self, answer, *, failed=False):
        self.answer = answer
        self.failed = failed
        self.env = {}

    async def run_stream(self, prompt, session_id=None):
        yield AdapterEvent(type="result", text=self.answer, is_error=self.failed,
                           usage={"input_tokens": 2, "output_tokens": 3})

    async def terminate(self):
        return None


async def test_delegation_uses_worker_route_and_shows_korean_title(tmp_path, monkeypatch):
    rules = tmp_path / "routing.yaml"
    rules.write_text("lead:\n  - agent: claude\nworkers:\n  default:\n"
                     "    - agent: codex\n      model: gpt-6-sol\n")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(rules))
    seen = []
    built = []
    frames = []
    tasks = {}

    def factory(agent, model, effort, scope, cwd):
        seen.append((agent, model, cwd))
        worker = FakeWorker("목록을 확인했습니다.")
        worker.command_prefix = ["docker", "exec", "-i", "ana-ws"]
        built.append(worker)
        return worker

    async def send(frame):
        frames.append(frame)

    result = await run_routed_task(
        "저장소 조회", "List my repositories", worker_factory=factory,
        workspace_dir=str(tmp_path), usage_tracker=FakeUsage(),
        native_agents=tasks, send=send,
    )

    assert seen == [("codex", "gpt-6-sol", str(tmp_path))]
    assert result == {"status": "done", "output": "목록을 확인했습니다.", "agent": "codex"}
    assert [frame["task"]["status"] for frame in frames] == ["running", "done"]
    assert frames[-1]["task"]["title"] == "저장소 조회"
    assert next(iter(tasks.values()))["model"] == "gpt-6-sol"
    assert any(part.startswith("ANA_GATE_AGENT_ID=routed:")
               for part in built[0].command_prefix)


async def test_delegated_worker_falls_back_after_auth_failure(tmp_path, monkeypatch):
    rules = tmp_path / "routing.yaml"
    rules.write_text("lead:\n  - agent: claude\nworkers:\n  default:\n"
                     "    - agent: codex\n      model: gpt-6-sol\n"
                     "    - agent: kimi\n      model: kimi-code/k3\n")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(rules))
    seen = []
    frames = []

    def factory(agent, model, effort, scope, cwd):
        worker = FakeWorker(
            "API Error: 403 Failed to authenticate" if agent == "codex" else "성공",
            failed=agent == "codex",
        )
        seen.append((agent, worker))
        return worker

    async def send(frame):
        frames.append(frame)

    result = await run_routed_task(
        "작업", "do it", worker_factory=factory, workspace_dir=str(tmp_path),
        usage_tracker=FakeUsage(), native_agents={}, send=send,
    )

    assert [agent for agent, _ in seen] == ["codex", "kimi"]
    assert all(worker.env.get("ANA_GATE_AGENT_ID") for _, worker in seen)
    assert result["status"] == "done" and result["agent"] == "kimi"
    assert frames[-1]["task"]["model"] == "kimi-code/k3"


async def test_delegated_output_masks_registered_secrets(tmp_path, monkeypatch):
    rules = tmp_path / "routing.yaml"
    rules.write_text("lead:\n  - agent: claude\nworkers:\n  default:\n"
                     "    - agent: codex\n")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(rules))
    frames = []

    async def send(frame):
        frames.append(frame)

    result = await run_routed_task(
        "조회", "read", worker_factory=lambda *args: FakeWorker("key=secret-value-123"),
        workspace_dir=str(tmp_path), usage_tracker=FakeUsage(),
        native_agents={}, send=send,
        secrets=[Secret(scope="global", name="TEST_KEY", value="secret-value-123")],
    )

    assert "secret-value-123" not in result["output"]
    assert "secret-value-123" not in frames[-1]["task"]["output"]
