"""Lead-triggered workers use the worker ladder, including provider failover."""
import asyncio
from pathlib import Path

from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.governance.secrets import Secret
from packages.orchestrator.activity import ActivityTracker
from packages.orchestrator import context as shared_context
from packages.orchestrator.context import SharedContext
from apps.server.app.delegation import DelegationManager, run_routed_task


async def test_long_delegation_poll_and_retry_keep_one_worker():
    finished = asyncio.Event()
    active: set[asyncio.Task] = set()
    manager = DelegationManager(active, poll_seconds=0.01)
    starts = 0

    async def worker():
        nonlocal starts
        starts += 1
        await finished.wait()
        return {"status": "done", "output": "completed"}

    first = await manager.request(1, "작업", "same prompt", worker)
    retry = await manager.request(1, "작업", "same prompt", worker)
    assert first == retry
    assert first["status"] == "running"
    assert starts == 1

    finished.set()
    result = await manager.request(1, "", "", worker, first["task_id"])
    assert result == {"status": "done", "output": "completed", "task_id": first["task_id"]}
    assert (await manager.request(1, "작업", "same prompt", worker)) == result
    assert starts == 1


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
    assert [frame["task"]["status"] for frame in frames] == ["running", "running", "done"]
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


async def test_a_delegated_worker_that_goes_quiet_returns_what_it_did(tmp_path, monkeypatch):
    """The lead used to wait a fixed 30 minutes and then be told only
    "timeout" — so it redid the worker's work by hand, verification and all.
    Now the wait ends when the worker stops reporting, and what it said goes
    back with the reason it stopped."""
    rules = tmp_path / "routing.yaml"
    rules.write_text("lead:\n  - agent: claude\nworkers:\n  default:\n    - agent: codex\n")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(rules))
    monkeypatch.setenv("ANA_SUBAGENT_IDLE_TIMEOUT_SECONDS", "0.3")
    frames = []
    terminated = []

    class Stalling(AgentAdapter):
        def __init__(self):
            self.env = {}

        async def run_stream(self, prompt, session_id=None):
            yield AdapterEvent(type="text", text="스캐폴딩을 시작합니다.")
            await asyncio.sleep(10)  # the CLI is alive and says nothing more
            yield AdapterEvent(type="result", text="never arrives")

        async def terminate(self):
            terminated.append(True)

    async def send(frame):
        frames.append(frame)

    activity = ActivityTracker()
    result = await run_routed_task(
        "기반 세팅", "scaffold it", worker_factory=lambda *args: Stalling(),
        workspace_dir=str(tmp_path), usage_tracker=FakeUsage(),
        native_agents={}, send=send, activity=activity,
    )

    assert result["status"] == "error"
    assert "활동이 없어" in result["error"]
    # What it managed to say, not nothing.
    assert result["output"] == "스캐폴딩을 시작합니다."
    assert any(
        frame["task"]["status"] == "running"
        and frame["task"]["blocks"] == [{"kind": "text", "text": "스캐폴딩을 시작합니다."}]
        for frame in frames
    ), "partial worker narration should reach the UI before the worker finishes"
    assert frames[-1]["task"]["status"] == "timeout"
    assert frames[-1]["task"]["blocks"] == [
        {"kind": "text", "text": "스캐폴딩을 시작합니다."}
    ]
    # And its CLI is gone: an abandoned worker keeps working, and billing.
    assert terminated == [True]


def _git(*args, cwd) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(path):
    """A workspace a worktree can actually branch from."""
    path.mkdir()
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@localhost", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "a.txt").write_text("base\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-m", "base", cwd=path)
    return path


def _routing(tmp_path, monkeypatch):
    rules = tmp_path / "routing.yaml"
    rules.write_text("lead:\n  - agent: claude\nworkers:\n  default:\n"
                     "    - agent: codex\n      model: gpt-6-sol\n")
    monkeypatch.setenv("ANA_ROUTING_RULES", str(rules))


async def test_a_delegated_worker_works_in_its_own_worktree_and_merges_back(
    tmp_path, monkeypatch
):
    _routing(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "workspace")
    seen = []

    def factory(agent, model, effort, scope, cwd):
        seen.append(cwd)

        class Writer(FakeWorker):
            async def run_stream(self, prompt, session_id=None):
                (Path(cwd) / "b.txt").write_text("worker wrote this\n", encoding="utf-8")
                async for event in super().run_stream(prompt, session_id):
                    yield event

        return Writer("작업했습니다.")

    async def send(frame):
        return None

    result = await run_routed_task(
        "파일 추가", "Add b.txt", worker_factory=factory, workspace_dir=str(repo),
        usage_tracker=FakeUsage(), native_agents={}, send=send, thread_id="th",
    )

    # Not the workspace itself: its own checkout under .ana/worktrees/.
    assert seen and seen[0] != str(repo)
    assert ".ana/worktrees/" in seen[0].replace("\\", "/")
    assert result["status"] == "done"
    # And the work is in the workspace once it finished.
    assert (repo / "b.txt").read_text(encoding="utf-8") == "worker wrote this\n"
    # The checkout itself is gone, and so is its branch.
    assert not Path(seen[0]).exists()
    assert "ana/" not in _git("branch", "--format=%(refname:short)", cwd=repo)


async def test_a_conflicted_delegation_tells_the_lead_which_branch_holds_the_work(
    tmp_path, monkeypatch
):
    _routing(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "workspace")

    def factory(agent, model, effort, scope, cwd):
        class Conflicting(FakeWorker):
            async def run_stream(self, prompt, session_id=None):
                (Path(cwd) / "a.txt").write_text("worker's version\n", encoding="utf-8")
                # Meanwhile the lead changed the same line in the workspace.
                (repo / "a.txt").write_text("lead's version\n", encoding="utf-8")
                _git("add", "-A", cwd=repo)
                _git("commit", "-m", "lead", cwd=repo)
                async for event in super().run_stream(prompt, session_id):
                    yield event

        return Conflicting("고쳤습니다.")

    async def send(frame):
        return None

    result = await run_routed_task(
        "같은 줄 수정", "Edit a.txt", worker_factory=factory, workspace_dir=str(repo),
        usage_tracker=FakeUsage(), native_agents={}, send=send, thread_id="th",
    )

    # The lead reads one string, so the branch has to be in it — otherwise it
    # answers the user as though the change had been applied.
    assert "병합되지 못했습니다" in result["output"]
    assert "ana/" in result["output"]
    # The workspace is untouched and the work survives on its branch.
    assert (repo / "a.txt").read_text(encoding="utf-8") == "lead's version\n"
    assert "ana/" in _git("branch", "--format=%(refname:short)", cwd=repo)


async def test_a_plain_directory_shares_the_workspace_as_before(tmp_path, monkeypatch):
    _routing(tmp_path, monkeypatch)
    workspace = tmp_path / "plain"
    workspace.mkdir()
    seen = []

    def factory(agent, model, effort, scope, cwd):
        seen.append(cwd)
        return FakeWorker("했습니다.")

    async def send(frame):
        return None

    result = await run_routed_task(
        "작업", "Do it", worker_factory=factory, workspace_dir=str(workspace),
        usage_tracker=FakeUsage(), native_agents={}, send=send, thread_id="th",
    )

    assert seen == [str(workspace)]  # nothing to branch from, so nothing pretends to
    assert result["status"] == "done"


async def test_a_delegated_worker_is_handed_what_the_workspace_remembers(
    tmp_path, monkeypatch
):
    _routing(tmp_path, monkeypatch)
    workspace = tmp_path / "plain"
    workspace.mkdir()
    shared_context.remember(
        str(workspace),
        SharedContext(notes="The gate owns path confinement.", remember=True),
        title="게이트",
    )
    prompts = []

    def factory(agent, model, effort, scope, cwd):
        class Reader(FakeWorker):
            async def run_stream(self, prompt, session_id=None):
                prompts.append(prompt)
                async for event in super().run_stream(prompt, session_id):
                    yield event

        return Reader("읽었습니다.")

    async def send(frame):
        return None

    await run_routed_task(
        "작업", "Do it", worker_factory=factory, workspace_dir=str(workspace),
        usage_tracker=FakeUsage(), native_agents={}, send=send, thread_id="th",
    )

    assert "The gate owns path confinement." in prompts[0]
    assert prompts[0].rstrip().endswith("Do it")
