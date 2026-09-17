# Core Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working end-to-end slice of ANA: a browser chat UI that talks over WebSocket to a FastAPI backend, which drives a single persistent "lead agent" (Claude Code CLI) as a subprocess per turn and streams its reply back live.

**Architecture:** FastAPI exposes one WebSocket endpoint. Each connection gets a `ChatSession` holding an in-memory turn queue. Each turn spawns `claude -p ... --output-format stream-json --resume <id>` as a subprocess (no long-lived OS process — conversation continuity comes from Claude Code CLI's own `--resume`), parses its stream-json lines into typed `AdapterEvent`s, relays them to the browser over the same WebSocket, and appends a transcript record to a JSONL file in the shared workspace directory. The Next.js frontend renders the stream and queues additional user messages while a turn is in flight.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, asyncio subprocess, pytest + pytest-asyncio; Node 20+, Next.js (App Router), TypeScript, Tailwind CSS, shadcn/ui; Claude Code CLI (`claude`) 2.x; Docker + docker-compose.

**Spec:** `docs/plans/2026-09-17-ai-orchestrator-roadmap.md` (section "1. Core Loop" and "확정된 설계 결정")

## Global Constraints

- Bind address `0.0.0.0`, internal network only, no auth layer (per spec).
- Fixed workspace directory: `/home/hello/workspace` (already created).
- Claude Code CLI auto-approval: always pass `--dangerously-skip-permissions`.
- Conversation continuity via `--resume <session-id>` / `--session-id <uuid>`, never a long-lived OS process (per spec, "CLI 프로세스 모델").
- `model` and `effort` are configurable per adapter instance (CLI flags `--model`, `--effort`), defaulting from `.env` (`CLAUDE_MODEL`, `CLAUDE_EFFORT`), never hardcoded — this is what lets the future failover logic (Memory & State subproject) switch model/effort without touching adapter code.
- `use-claude-api` (API billing mode, `ANTHROPIC_API_KEY_SAVED` via `panel.tapie.kr`) is **not verified working as of this plan** — it may be dead/expired. Do not assume it works; verify it explicitly in Task 10, and fall back to `pro` mode if it fails.
- Every turn's prompt + response is appended to a transcript JSONL file in the workspace (per spec, "대화 기록 보관") — failover logic that *uses* this log is out of scope (Memory & State subproject).
- Sub-agent spawning, Codex CLI, multi-agent patterns are explicitly out of scope for this plan (Multi-Agent Orchestration subproject).
- Whole stack runs via a single `docker-compose.yml` at repo root (per spec, "운영").
- `ANTHROPIC_API_KEY_SAVED` and any other secret value must never be written into source files, commit messages, or this plan — only referenced as an env var name.

---

## Empirically Verified CLI Behavior (read before implementing Task 4)

Verified by running the real `claude` CLI against `/home/hello/workspace` during planning:

1. `--output-format stream-json` **requires** `--verbose`, or the CLI exits immediately with `Error: When using --print, --output-format=stream-json requires --verbose`.
2. `--session-id <uuid>` lets us choose our own session id on the first turn; `--resume <uuid>` continues it on later turns. No need to scrape a session id out of CLI output.
3. Streamed JSON lines all have a top-level `"type"` field. Only these matter for the adapter:
   - `"type": "assistant"` — one AI message chunk. Text lives at `message.content[].text` for blocks where `content[].type == "text"`.
   - `"type": "result"` — final event of the turn. Carries `result` (full text), `session_id`, `total_cost_usd`, `is_error`.
   - `"type": "rate_limit_event"` — usage/quota info (`rate_limit_info.unifiedWindows.five_hour.utilization` etc.) — not used yet, but must not crash the parser; future circuit breaker (Sandbox & Governance subproject) will consume this.
   - `"type": "system"` — hook/init noise. **This machine's global `~/.claude/settings.json` hooks (caveman mode, superpowers reminder) fire on every subprocess invocation and inject large `hook_response` payloads into the stream.** Do not try to suppress this with `CLAUDE_CONFIG_DIR` — that was tested and it also breaks OAuth Pro-mode login (falls back to "Not logged in"). The correct fix is a parser that only reacts to `"assistant"`, `"result"`, and `"rate_limit_event"` and silently ignores everything else, which is what `ClaudeCodeAdapter._parse_event` below does.
4. `--model <model>` (alias like `sonnet`/`opus`/`fable` or a full model name) and `--effort <level>` (`low`, `medium`, `high`, `xhigh`, `max`) are both real, independent flags confirmed in `claude --help`. They are included in this plan as optional, configurable adapter parameters (Task 2, Task 4) precisely so a later failover routine can retry a turn with a different model/effort without any adapter code changes.

---

## File Structure

```
ana/
├── .env.example                          # new
├── .gitignore                             # new
├── pyproject.toml                         # new (pytest config)
├── docker-compose.yml                     # new
├── packages/
│   ├── __init__.py                        # new
│   └── adapters/
│       ├── __init__.py                    # new
│       ├── base.py                        # new — AdapterEvent, AgentAdapter ABC
│       ├── env_builder.py                 # new — pro/api env switching
│       ├── claude_code.py                 # new — ClaudeCodeAdapter
│       └── transcript.py                  # new — TranscriptLogger
├── apps/
│   ├── __init__.py                        # new
│   ├── server/
│   │   ├── __init__.py                    # new
│   │   ├── Dockerfile                     # new
│   │   ├── requirements.txt               # new
│   │   ├── app/
│   │   │   ├── __init__.py                # new
│   │   │   ├── main.py                    # new — FastAPI app + /healthz
│   │   │   ├── session_manager.py         # new — ChatSession (turn queue)
│   │   │   └── websocket.py               # new — /ws/chat endpoint
│   │   └── tests/
│   │       ├── __init__.py                # new
│   │       ├── fixtures/
│   │       │   └── fake_claude.py         # new — canned stream-json emitter for tests
│   │       ├── test_main.py               # new
│   │       ├── test_env_builder.py        # new
│   │       ├── test_base.py               # new
│   │       ├── test_claude_code_adapter.py# new
│   │       ├── test_transcript.py         # new
│   │       ├── test_session_manager.py    # new
│   │       └── test_websocket.py          # new
│   └── web/                               # new — Next.js app (scaffolded in Task 9)
│       ├── Dockerfile
│       ├── src/app/page.tsx
│       └── src/hooks/useChatSocket.ts
```

---

### Task 1: Repo scaffolding, `.gitignore`, pytest config, FastAPI health check

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `apps/server/requirements.txt`
- Create: `packages/__init__.py`, `packages/adapters/__init__.py`
- Create: `apps/__init__.py`, `apps/server/__init__.py`, `apps/server/app/__init__.py`, `apps/server/tests/__init__.py`
- Create: `apps/server/app/main.py`
- Test: `apps/server/tests/test_main.py`

**Interfaces:**
- Produces: `apps.server.app.main:app` (FastAPI instance), `GET /healthz -> {"status": "ok"}`

- [ ] **Step 1: Initialize git and add `.gitignore`**

```bash
cd /home/hello/ana
git init
```

`.gitignore`:
```gitignore
__pycache__/
*.pyc
.venv/
.env
node_modules/
.next/
apps/web/.next/
```

- [ ] **Step 2: Add pytest config and empty package markers**

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["apps/server/tests"]
asyncio_mode = "auto"
```

Create empty files: `packages/__init__.py`, `packages/adapters/__init__.py`, `apps/__init__.py`, `apps/server/__init__.py`, `apps/server/app/__init__.py`, `apps/server/tests/__init__.py`.

- [ ] **Step 3: Add requirements and install them**

`apps/server/requirements.txt`:
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
websockets==14.1
python-dotenv==1.0.1
httpx==0.28.1
pytest==8.3.4
pytest-asyncio==0.25.2
```

```bash
cd /home/hello/ana
python3 -m venv .venv
.venv/bin/pip install -r apps/server/requirements.txt
```

- [ ] **Step 4: Write the failing test**

`apps/server/tests/test_main.py`:
```python
from fastapi.testclient import TestClient

from apps.server.app.main import app


def test_healthz_returns_ok():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 5: Run test to verify it fails**

Run: `.venv/bin/pytest apps/server/tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.server.app.main'`

- [ ] **Step 6: Write minimal implementation**

`apps/server/app/main.py`:
```python
from fastapi import FastAPI

app = FastAPI(title="ANA Core Loop")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/pytest apps/server/tests/test_main.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add .gitignore pyproject.toml apps/server/requirements.txt \
  packages/__init__.py packages/adapters/__init__.py \
  apps/__init__.py apps/server/__init__.py apps/server/app/__init__.py \
  apps/server/tests/__init__.py apps/server/app/main.py apps/server/tests/test_main.py
git commit -m "feat: scaffold FastAPI app with health check"
```

---

### Task 2: `env_builder` — pro/api auth mode switching

**Files:**
- Create: `packages/adapters/env_builder.py`
- Test: `apps/server/tests/test_env_builder.py`

**Interfaces:**
- Produces: `EnvBuilder(mode: str).build(base_env: dict | None) -> dict[str, str]`, `default_mode_from_settings(default: str = "api") -> str`, `default_model_from_settings() -> str | None`, `default_effort_from_settings() -> str | None`

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_env_builder.py`:
```python
import pytest

from packages.adapters.env_builder import (
    EnvBuilder,
    default_effort_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
)


def test_pro_mode_strips_api_env_vars():
    base_env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "leftover",
        "ANTHROPIC_AUTH_TOKEN": "leftover",
        "ANTHROPIC_BASE_URL": "https://panel.tapie.kr/api/claude-code",
    }
    env = EnvBuilder("pro").build(base_env)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["PATH"] == "/usr/bin"


def test_api_mode_injects_billing_env_vars():
    base_env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY_SAVED": "test-key-value"}
    env = EnvBuilder("api").build(base_env)
    assert env["ANTHROPIC_API_KEY"] == "test-key-value"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-key-value"
    assert env["ANTHROPIC_BASE_URL"] == "https://panel.tapie.kr/api/claude-code"


def test_api_mode_without_saved_key_raises():
    with pytest.raises(RuntimeError):
        EnvBuilder("api").build({"PATH": "/usr/bin"})


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        EnvBuilder("bogus")


def test_default_mode_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTH_MODE", "pro")
    assert default_mode_from_settings() == "pro"


def test_default_mode_from_settings_falls_back(monkeypatch):
    monkeypatch.delenv("CLAUDE_AUTH_MODE", raising=False)
    assert default_mode_from_settings(default="api") == "api"


def test_default_model_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "opus")
    assert default_model_from_settings() == "opus"


def test_default_model_from_settings_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert default_model_from_settings() is None


def test_default_effort_from_settings_reads_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    assert default_effort_from_settings() == "high"


def test_default_effort_from_settings_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)
    assert default_effort_from_settings() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_env_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.adapters.env_builder'`

- [ ] **Step 3: Write minimal implementation**

`packages/adapters/env_builder.py`:
```python
import os


class EnvBuilder:
    """Builds the environment injected into a Claude Code CLI subprocess,
    mirroring ~/.bashrc's use-claude-pro / use-claude-api functions."""

    def __init__(self, mode: str):
        if mode not in ("pro", "api"):
            raise ValueError(f"unknown auth mode: {mode!r}")
        self.mode = mode

    def build(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base_env if base_env is not None else os.environ)
        if self.mode == "pro":
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_BASE_URL", None)
        else:
            api_key = env.get("ANTHROPIC_API_KEY_SAVED")
            if not api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY_SAVED is not set; cannot build API-mode env"
                )
            env["ANTHROPIC_API_KEY"] = api_key
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env["ANTHROPIC_BASE_URL"] = "https://panel.tapie.kr/api/claude-code"
        return env


def default_mode_from_settings(default: str = "api") -> str:
    mode = os.environ.get("CLAUDE_AUTH_MODE", default)
    return mode if mode in ("pro", "api") else default


def default_model_from_settings() -> str | None:
    """Reads CLAUDE_MODEL from the environment. None means: let the claude
    CLI use its own default (no --model flag passed)."""
    return os.environ.get("CLAUDE_MODEL") or None


def default_effort_from_settings() -> str | None:
    """Reads CLAUDE_EFFORT from the environment. None means: let the claude
    CLI use its own default (no --effort flag passed)."""
    return os.environ.get("CLAUDE_EFFORT") or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_env_builder.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/env_builder.py apps/server/tests/test_env_builder.py
git commit -m "feat: add env_builder for pro/api auth mode switching"
```

---

### Task 3: `AgentAdapter` base interface

**Files:**
- Create: `packages/adapters/base.py`
- Test: `apps/server/tests/test_base.py`

**Interfaces:**
- Produces: `AdapterEvent` dataclass (`type: str`, `text: str = ""`, `session_id: str | None = None`, `cost_usd: float | None = None`, `is_error: bool = False`, `raw: dict | None = None`), `AgentAdapter` ABC with abstract `run_stream(prompt, session_id) -> AsyncIterator[AdapterEvent]` and abstract `async terminate() -> None`.
- Note: the roadmap doc's adapter sketch also lists `send_input()`. It is intentionally **not** part of this interface: Core Loop uses one-shot `--print` calls per turn (see spec, "CLI 프로세스 모델"), so there is no live stdin to write to mid-turn. If a future subproject needs interactive stdin, add it there.

- [ ] **Step 1: Write the failing test**

`apps/server/tests/test_base.py`:
```python
import pytest

from packages.adapters.base import AdapterEvent, AgentAdapter


def test_adapter_event_defaults():
    event = AdapterEvent(type="text", text="hi")
    assert event.session_id is None
    assert event.cost_usd is None
    assert event.is_error is False
    assert event.raw is None


def test_agent_adapter_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        AgentAdapter()  # type: ignore[abstract]


def test_concrete_subclass_must_implement_both_methods():
    class Incomplete(AgentAdapter):
        async def terminate(self) -> None:
            return None

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest apps/server/tests/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.adapters.base'`

- [ ] **Step 3: Write minimal implementation**

`packages/adapters/base.py`:
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class AdapterEvent:
    """One structured event out of an agent CLI turn."""

    type: str  # "text" | "result" | "usage" | "error"
    text: str = ""
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    is_error: bool = False
    raw: Optional[dict] = None


class AgentAdapter(ABC):
    """Common interface every CLI-backed agent adapter must implement."""

    @abstractmethod
    def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        """Run one turn, yielding AdapterEvents as the CLI streams output."""
        raise NotImplementedError

    @abstractmethod
    async def terminate(self) -> None:
        """Kill any in-flight subprocess for this adapter instance."""
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest apps/server/tests/test_base.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/base.py apps/server/tests/test_base.py
git commit -m "feat: add AgentAdapter abstract interface"
```

---

### Task 4: `ClaudeCodeAdapter`

**Files:**
- Create: `packages/adapters/claude_code.py`
- Create: `apps/server/tests/fixtures/fake_claude.py`
- Test: `apps/server/tests/test_claude_code_adapter.py`

**Interfaces:**
- Consumes: `AgentAdapter`, `AdapterEvent` from `packages.adapters.base`
- Produces: `ClaudeCodeAdapter(workspace_dir: str, env: dict[str, str], executable: str = "claude", model: str | None = None, effort: str | None = None)` implementing `AgentAdapter`. `model`/`effort` map straight to the CLI's `--model`/`--effort` flags (see "Empirically Verified CLI Behavior" above) and exist so a future failover routine can construct a new adapter with a different model/effort with no code changes here.

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/fixtures/fake_claude.py` (a stand-in for the real `claude` binary — emits the exact stream-json shapes captured from the real CLI during planning, so tests never call the real API):

```python
#!/usr/bin/env python3
"""Fake `claude` CLI for tests: ignores its arguments and prints a canned
stream-json turn, matching the real CLI's observed output shape."""
import json
import sys

SESSION_ID = "test-session-id"

EVENTS = [
    {"type": "system", "subtype": "init", "session_id": SESSION_ID},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "pong"}]},
        "session_id": SESSION_ID,
    },
    {
        "type": "rate_limit_event",
        "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.11}}},
        "session_id": SESSION_ID,
    },
    {
        "type": "result",
        "result": "pong",
        "session_id": SESSION_ID,
        "total_cost_usd": 0.07,
        "is_error": False,
    },
]


def main() -> None:
    for event in EVENTS:
        print(json.dumps(event))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

Make it executable: `chmod +x apps/server/tests/fixtures/fake_claude.py`

`apps/server/tests/test_claude_code_adapter.py`:
```python
import sys
from pathlib import Path

import pytest

from packages.adapters.claude_code import ClaudeCodeAdapter

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_claude.py")


def test_parse_event_assistant_text():
    event = ClaudeCodeAdapter._parse_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        session_id="sid",
    )
    assert event is not None
    assert event.type == "text"
    assert event.text == "hi"


def test_parse_event_result():
    event = ClaudeCodeAdapter._parse_event(
        {
            "type": "result",
            "result": "hi",
            "session_id": "sid-from-event",
            "total_cost_usd": 0.01,
            "is_error": False,
        },
        session_id="sid",
    )
    assert event is not None
    assert event.type == "result"
    assert event.session_id == "sid-from-event"
    assert event.cost_usd == 0.01
    assert event.is_error is False


def test_parse_event_rate_limit():
    event = ClaudeCodeAdapter._parse_event(
        {"type": "rate_limit_event", "rate_limit_info": {}}, session_id="sid"
    )
    assert event is not None
    assert event.type == "usage"


def test_parse_event_system_is_ignored():
    event = ClaudeCodeAdapter._parse_event({"type": "system", "subtype": "init"}, session_id="sid")
    assert event is None


@pytest.mark.asyncio
async def test_run_stream_yields_text_then_result():
    adapter = ClaudeCodeAdapter(
        workspace_dir="/home/hello/workspace",
        env={"PATH": "/usr/bin:/bin"},
        executable=sys.executable,
    )
    # fake_claude.py ignores argv, so passing it as the first arg after the
    # interpreter works: executable=python, and we monkeypatch args via subclassing
    adapter._extra_args_prefix = [FAKE_CLI]  # see implementation note below

    events = [event async for event in adapter.run_stream("ping")]

    text_events = [e for e in events if e.type == "text"]
    result_events = [e for e in events if e.type == "result"]
    usage_events = [e for e in events if e.type == "usage"]

    assert [e.text for e in text_events] == ["pong"]
    assert len(result_events) == 1
    assert result_events[0].cost_usd == 0.07
    assert len(usage_events) == 1


@pytest.mark.asyncio
async def test_terminate_without_running_process_is_a_noop():
    adapter = ClaudeCodeAdapter(workspace_dir="/home/hello/workspace", env={})
    await adapter.terminate()  # must not raise


def test_build_args_omits_model_and_effort_when_unset():
    adapter = ClaudeCodeAdapter(workspace_dir="/home/hello/workspace", env={})
    args, _ = adapter._build_args("hi", session_id=None)
    assert "--model" not in args
    assert "--effort" not in args


def test_build_args_includes_model_and_effort_when_set():
    adapter = ClaudeCodeAdapter(
        workspace_dir="/home/hello/workspace", env={}, model="opus", effort="high"
    )
    args, _ = adapter._build_args("hi", session_id=None)
    assert "--model" in args
    assert args[args.index("--model") + 1] == "opus"
    assert "--effort" in args
    assert args[args.index("--effort") + 1] == "high"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_claude_code_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.adapters.claude_code'`

- [ ] **Step 3: Write minimal implementation**

`packages/adapters/claude_code.py`:
```python
import asyncio
import json
import uuid
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter


class ClaudeCodeAdapter(AgentAdapter):
    """Wraps the `claude` CLI as a one-subprocess-per-turn adapter, using
    --resume to carry conversation context across turns instead of keeping
    an OS process alive (see roadmap doc, 'CLI 프로세스 모델')."""

    def __init__(
        self,
        workspace_dir: str,
        env: dict[str, str],
        executable: str = "claude",
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable
        self.model = model
        self.effort = effort
        self._process: Optional[asyncio.subprocess.Process] = None
        # test-only hook: extra argv inserted between the executable and the
        # rest of the args, used by tests to point at fake_claude.py without
        # changing production argv construction.
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> tuple[list[str], str]:
        args = [self.executable, *self._extra_args_prefix, "-p", prompt]
        args += ["--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if session_id:
            args += ["--resume", session_id]
            resolved_session_id = session_id
        else:
            resolved_session_id = str(uuid.uuid4())
            args += ["--session-id", resolved_session_id]
        return args, resolved_session_id

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        args, resolved_session_id = self._build_args(prompt, session_id)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace_dir,
            env=self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert self._process.stdout is not None
        async for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = self._parse_event(raw_event, resolved_session_id)
            if parsed is not None:
                yield parsed

        stderr_bytes = await self._process.stderr.read() if self._process.stderr else b""
        return_code = await self._process.wait()
        if return_code != 0:
            yield AdapterEvent(
                type="error",
                text=stderr_bytes.decode("utf-8", errors="replace"),
                session_id=resolved_session_id,
                is_error=True,
            )

    @staticmethod
    def _parse_event(event: dict, session_id: str) -> Optional[AdapterEvent]:
        event_type = event.get("type")
        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            text = "".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
            if not text:
                return None
            return AdapterEvent(type="text", text=text, session_id=session_id, raw=event)
        if event_type == "result":
            return AdapterEvent(
                type="result",
                text=event.get("result", ""),
                session_id=event.get("session_id", session_id),
                cost_usd=event.get("total_cost_usd"),
                is_error=bool(event.get("is_error")),
                raw=event,
            )
        if event_type == "rate_limit_event":
            return AdapterEvent(type="usage", session_id=session_id, raw=event)
        return None  # "system" (hook/init) noise — see planning notes at top of this file

    async def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_claude_code_adapter.py -v`
Expected: PASS (8 tests). Note this test suite spawns real subprocesses (the fake CLI script) but makes zero real Claude API calls.

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/claude_code.py apps/server/tests/fixtures/fake_claude.py \
  apps/server/tests/test_claude_code_adapter.py
git commit -m "feat: add ClaudeCodeAdapter wrapping claude CLI as subprocess-per-turn"
```

---

### Task 5: `TranscriptLogger`

**Files:**
- Create: `packages/adapters/transcript.py`
- Test: `apps/server/tests/test_transcript.py`

**Interfaces:**
- Produces: `TranscriptLogger(workspace_dir: str, filename: str = ".ana_transcript.jsonl")` with `append(session_id, prompt, response, cost_usd=None) -> None` and `read_all() -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`apps/server/tests/test_transcript.py`:
```python
from packages.adapters.transcript import TranscriptLogger


def test_append_and_read_all_round_trip(tmp_path):
    logger = TranscriptLogger(str(tmp_path))

    logger.append(session_id="s1", prompt="hi", response="hello", cost_usd=0.01)
    logger.append(session_id="s1", prompt="how are you", response="good", cost_usd=0.02)

    records = logger.read_all()
    assert len(records) == 2
    assert records[0]["prompt"] == "hi"
    assert records[0]["response"] == "hello"
    assert records[0]["cost_usd"] == 0.01
    assert records[1]["prompt"] == "how are you"
    assert all("timestamp" in r and r["session_id"] == "s1" for r in records)


def test_read_all_on_missing_file_returns_empty_list(tmp_path):
    logger = TranscriptLogger(str(tmp_path))
    assert logger.read_all() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest apps/server/tests/test_transcript.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'packages.adapters.transcript'`

- [ ] **Step 3: Write minimal implementation**

`packages/adapters/transcript.py`:
```python
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class TranscriptLogger:
    """Appends one JSON line per turn to <workspace_dir>/<filename> so a
    different model can reconstruct context if the lead agent dies (see
    roadmap doc, '대화 기록(transcript) 보관')."""

    def __init__(self, workspace_dir: str, filename: str = ".ana_transcript.jsonl"):
        self.path = Path(workspace_dir) / filename

    def append(
        self,
        session_id: str,
        prompt: str,
        response: str,
        cost_usd: Optional[float] = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "prompt": prompt,
            "response": response,
            "cost_usd": cost_usd,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest apps/server/tests/test_transcript.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/transcript.py apps/server/tests/test_transcript.py
git commit -m "feat: add TranscriptLogger for turn-by-turn audit log"
```

---

### Task 6: `ChatSession` (turn queue)

**Files:**
- Create: `apps/server/app/session_manager.py`
- Test: `apps/server/tests/test_session_manager.py`

**Interfaces:**
- Consumes: `AgentAdapter`, `AdapterEvent` (`packages.adapters.base`), `TranscriptLogger` (`packages.adapters.transcript`)
- Produces: `ChatSession(adapter: AgentAdapter, transcript: TranscriptLogger)` with `async send(prompt: str) -> None`, `async run() -> AsyncIterator[AdapterEvent]`, `async close() -> None`

- [ ] **Step 1: Write the failing test**

`apps/server/tests/test_session_manager.py`:
```python
import asyncio
from typing import AsyncIterator, Optional

import pytest

from apps.server.app.session_manager import ChatSession
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


class FakeAdapter(AgentAdapter):
    """Yields one canned text event + one result event per call, echoing
    the prompt back so tests can assert per-turn ordering."""

    def __init__(self):
        self.terminated = False
        self.calls: list[tuple[str, Optional[str]]] = []

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        self.calls.append((prompt, session_id))
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.01)

    async def terminate(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_processes_queued_prompts_in_order(tmp_path):
    adapter = FakeAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.send("first")
    await session.send("second")

    collected: list[AdapterEvent] = []
    run_iter = session.run()
    for _ in range(4):  # 2 events per turn x 2 turns
        collected.append(await run_iter.__anext__())

    texts = [e.text for e in collected if e.type == "text"]
    assert texts == ["echo:first", "echo:second"]
    assert adapter.calls[0] == ("first", None)
    # second call resumes the session id learned from the first turn
    assert adapter.calls[1] == ("second", "sid-1")

    records = transcript.read_all()
    assert [r["prompt"] for r in records] == ["first", "second"]
    assert [r["response"] for r in records] == ["echo:first", "echo:second"]


@pytest.mark.asyncio
async def test_close_terminates_adapter(tmp_path):
    adapter = FakeAdapter()
    transcript = TranscriptLogger(str(tmp_path))
    session = ChatSession(adapter=adapter, transcript=transcript)

    await session.close()

    assert adapter.terminated is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest apps/server/tests/test_session_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.server.app.session_manager'`

- [ ] **Step 3: Write minimal implementation**

`apps/server/app/session_manager.py`:
```python
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


@dataclass
class ChatSession:
    """One browser connection's lead-agent conversation. Owns the turn
    queue so concurrent user messages are processed in order instead of
    interrupting an in-flight turn (see roadmap doc, '동시 메시지 처리')."""

    adapter: AgentAdapter
    transcript: TranscriptLogger
    session_id: Optional[str] = None
    _queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)

    async def send(self, prompt: str) -> None:
        await self._queue.put(prompt)

    async def run(self) -> AsyncIterator[AdapterEvent]:
        """Consumes queued prompts forever, yielding every AdapterEvent
        from each turn. The caller (websocket handler) drives this loop."""
        while True:
            prompt = await self._queue.get()
            response_text = ""
            cost_usd = None
            async for event in self.adapter.run_stream(prompt, session_id=self.session_id):
                if event.session_id:
                    self.session_id = event.session_id
                if event.type == "text":
                    response_text += event.text
                if event.type == "result":
                    cost_usd = event.cost_usd
                yield event
            self.transcript.append(
                session_id=self.session_id or "",
                prompt=prompt,
                response=response_text,
                cost_usd=cost_usd,
            )

    async def close(self) -> None:
        await self.adapter.terminate()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest apps/server/tests/test_session_manager.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/server/app/session_manager.py apps/server/tests/test_session_manager.py
git commit -m "feat: add ChatSession turn queue coordinating adapter and transcript"
```

---

### Task 7: WebSocket endpoint

**Files:**
- Create: `apps/server/app/websocket.py`
- Modify: `apps/server/app/main.py`
- Test: `apps/server/tests/test_websocket.py`

**Interfaces:**
- Consumes: `ChatSession` (`apps.server.app.session_manager`), `ClaudeCodeAdapter` (`packages.adapters.claude_code`), `EnvBuilder`/`default_mode_from_settings` (`packages.adapters.env_builder`), `TranscriptLogger` (`packages.adapters.transcript`)
- Produces: `router: APIRouter` with `WS /ws/chat`; `build_session() -> ChatSession` (module-level factory, patched in tests)

- [ ] **Step 1: Write the failing test**

`apps/server/tests/test_websocket.py`:
```python
from typing import AsyncIterator, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.server.app.main import app
from apps.server.app.session_manager import ChatSession
from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.adapters.transcript import TranscriptLogger


class FakeAdapter(AgentAdapter):
    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent(type="text", text=f"echo:{prompt}", session_id="sid-1")
        yield AdapterEvent(type="result", text=f"echo:{prompt}", session_id="sid-1", cost_usd=0.0)

    async def terminate(self) -> None:
        return None


def test_websocket_echoes_prompt_back(tmp_path):
    fake_session = ChatSession(adapter=FakeAdapter(), transcript=TranscriptLogger(str(tmp_path)))

    with patch("apps.server.app.websocket.build_session", return_value=fake_session):
        client = TestClient(app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text("hello")
            first = ws.receive_json()
            second = ws.receive_json()

    assert first == {
        "type": "text",
        "text": "echo:hello",
        "session_id": "sid-1",
        "is_error": False,
    }
    assert second["type"] == "result"
    assert second["text"] == "echo:hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest apps/server/tests/test_websocket.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.server.app.websocket'`

- [ ] **Step 3: Write minimal implementation**

`apps/server/app/websocket.py`:
```python
import asyncio
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from apps.server.app.session_manager import ChatSession
from packages.adapters.claude_code import ClaudeCodeAdapter
from packages.adapters.env_builder import (
    EnvBuilder,
    default_effort_from_settings,
    default_mode_from_settings,
    default_model_from_settings,
)
from packages.adapters.transcript import TranscriptLogger

router = APIRouter()

WORKSPACE_DIR = os.environ.get("ANA_WORKSPACE_DIR", "/home/hello/workspace")


def build_session() -> ChatSession:
    mode = default_mode_from_settings()
    env = EnvBuilder(mode).build()
    adapter = ClaudeCodeAdapter(
        workspace_dir=WORKSPACE_DIR,
        env=env,
        model=default_model_from_settings(),
        effort=default_effort_from_settings(),
    )
    transcript = TranscriptLogger(WORKSPACE_DIR)
    return ChatSession(adapter=adapter, transcript=transcript)


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session = build_session()

    async def relay_events() -> None:
        async for event in session.run():
            await websocket.send_json(
                {
                    "type": event.type,
                    "text": event.text,
                    "session_id": event.session_id,
                    "is_error": event.is_error,
                }
            )

    relay_task = asyncio.create_task(relay_events())
    try:
        while True:
            data = await websocket.receive_text()
            await session.send(data)
    except WebSocketDisconnect:
        pass
    finally:
        relay_task.cancel()
        await session.close()
```

Modify `apps/server/app/main.py`:
```python
from fastapi import FastAPI

from apps.server.app.websocket import router as chat_router

app = FastAPI(title="ANA Core Loop")
app.include_router(chat_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest apps/server/tests/test_websocket.py apps/server/tests/test_main.py -v`
Expected: PASS (both files)

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/pytest apps/server/tests -v`
Expected: PASS (all tests from Tasks 1–7)

- [ ] **Step 6: Commit**

```bash
git add apps/server/app/websocket.py apps/server/app/main.py apps/server/tests/test_websocket.py
git commit -m "feat: add /ws/chat websocket endpoint wiring ChatSession to the browser"
```

---

### Task 8: `.env`, docker-compose, Dockerfile for the server

**Files:**
- Create: `.env.example`
- Create: `apps/server/Dockerfile`
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: `apps.server.app.main:app` (Task 7)
- Produces: `docker-compose.yml` services `server` (port 8000) and `web` (port 3000, built in Task 9)

- [ ] **Step 1: Write `.env.example`**

```
CLAUDE_AUTH_MODE=api
ANTHROPIC_API_KEY_SAVED=
ANA_WORKSPACE_DIR=/home/hello/workspace
# Optional: pin the CLI to a specific model/effort. Leave blank to let the
# claude CLI use its own defaults.
CLAUDE_MODEL=
CLAUDE_EFFORT=
```

`CLAUDE_AUTH_MODE=api` is the default here to match this machine's current `~/.bashrc` behavior, **but this has not been confirmed working during planning** (the saved `ANTHROPIC_API_KEY_SAVED` / `panel.tapie.kr` proxy may be dead). Task 10 verifies both modes and, if `api` fails, switches `.env` to `CLAUDE_AUTH_MODE=pro` for local testing.

- [ ] **Step 2: Write the server Dockerfile**

`apps/server/Dockerfile`:
```dockerfile
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/ packages/
COPY apps/server/ apps/server/
RUN pip install --no-cache-dir -r apps/server/requirements.txt

ENV ANA_WORKSPACE_DIR=/home/hello/workspace
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.server.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Write `docker-compose.yml`**

```yaml
services:
  server:
    build:
      context: .
      dockerfile: apps/server/Dockerfile
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - /home/hello/workspace:/home/hello/workspace
      - ${HOME}/.claude:/root/.claude:ro
    environment:
      - ANA_WORKSPACE_DIR=/home/hello/workspace

  web:
    build:
      context: apps/web
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws/chat
    depends_on:
      - server
```

- [ ] **Step 4: Verify the server image builds**

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY_SAVED locally, never commit .env
docker compose build server
```

Expected: build succeeds (the `web` service is not buildable yet — that's Task 9).

- [ ] **Step 5: Commit**

```bash
git add .env.example apps/server/Dockerfile docker-compose.yml
git commit -m "feat: add docker-compose skeleton and server Dockerfile"
```

---

### Task 9: Next.js frontend (chat UI)

**Files:**
- Create: `apps/web/` (scaffolded by `create-next-app`)
- Create: `apps/web/src/hooks/useChatSocket.ts`
- Modify: `apps/web/src/app/page.tsx`
- Create: `apps/web/Dockerfile`

**Interfaces:**
- Consumes: `WS /ws/chat` (Task 7), env var `NEXT_PUBLIC_WS_URL`
- Produces: a chat page rendering streamed messages, sending user input over the socket

- [ ] **Step 1: Scaffold the Next.js app**

```bash
cd /home/hello/ana
npx create-next-app@latest apps/web \
  --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --no-turbopack --yes
cd apps/web
npx shadcn@latest init -d
```

- [ ] **Step 2: Write the WebSocket hook**

`apps/web/src/hooks/useChatSocket.ts`:
```typescript
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ChatEvent = {
  type: "text" | "result" | "usage" | "error";
  text: string;
  session_id: string | null;
  is_error: boolean;
};

export type ChatMessage = {
  role: "user" | "agent";
  text: string;
};

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws/chat";

export function useChatSocket() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const currentAgentTextRef = useRef<string>("");

  useEffect(() => {
    const socket = new WebSocket(WS_URL);
    socketRef.current = socket;

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);

    socket.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ChatEvent;
      if (event.type === "text") {
        currentAgentTextRef.current += event.text;
        setMessages((prev) => {
          const withoutLastAgentDraft = prev.filter(
            (m, i) => !(i === prev.length - 1 && m.role === "agent" && m.text === "")
          );
          const withoutRunningDraft = withoutLastAgentDraft.slice(
            0,
            withoutLastAgentDraft.length - (prev.length > 0 && prev[prev.length - 1].role === "agent" && prev[prev.length - 1].text !== currentAgentTextRef.current.slice(0, -event.text.length) ? 0 : (prev.length > 0 && prev[prev.length - 1].role === "agent" ? 1 : 0))
          );
          return [...withoutRunningDraft, { role: "agent", text: currentAgentTextRef.current }];
        });
      }
      if (event.type === "result") {
        currentAgentTextRef.current = "";
      }
    };

    return () => socket.close();
  }, []);

  const sendMessage = useCallback((text: string) => {
    setMessages((prev) => [...prev, { role: "user", text }]);
    socketRef.current?.send(text);
  }, []);

  return { messages, connected, sendMessage };
}
```

- [ ] **Step 3: Simplify the streaming-accumulation bug from Step 2**

The nested filter/slice above is unreadable and wrong. Replace the whole hook body's `onmessage` handler with a simpler accumulator that tracks the in-progress agent message by index instead of by content-matching:

```typescript
  useEffect(() => {
    const socket = new WebSocket(WS_URL);
    socketRef.current = socket;
    let agentMessageIndex: number | null = null;

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);

    socket.onmessage = (raw) => {
      const event = JSON.parse(raw.data) as ChatEvent;

      if (event.type === "text") {
        setMessages((prev) => {
          if (agentMessageIndex === null) {
            agentMessageIndex = prev.length;
            return [...prev, { role: "agent", text: event.text }];
          }
          const next = [...prev];
          next[agentMessageIndex] = {
            role: "agent",
            text: next[agentMessageIndex].text + event.text,
          };
          return next;
        });
      }

      if (event.type === "result") {
        agentMessageIndex = null;
      }
    };

    return () => socket.close();
  }, []);
```

Replace the whole `useEffect` block in `useChatSocket.ts` with this version (the file's final state has only this one `onmessage` implementation, not both).

- [ ] **Step 4: Write the chat page**

`apps/web/src/app/page.tsx`:
```tsx
"use client";

import { useState, type FormEvent } from "react";
import { useChatSocket } from "@/hooks/useChatSocket";

export default function ChatPage() {
  const { messages, connected, sendMessage } = useChatSocket();
  const [draft, setDraft] = useState("");

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!draft.trim()) return;
    sendMessage(draft);
    setDraft("");
  };

  return (
    <main className="mx-auto flex h-screen max-w-2xl flex-col p-4">
      <header className="mb-2 text-sm text-gray-500">
        {connected ? "connected" : "disconnected"}
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto">
        {messages.map((m, i) => (
          <div
            key={i}
            className={
              m.role === "user"
                ? "ml-auto max-w-[80%] rounded-lg bg-blue-600 px-3 py-2 text-white"
                : "mr-auto max-w-[80%] rounded-lg bg-gray-200 px-3 py-2 text-gray-900"
            }
          >
            {m.text}
          </div>
        ))}
      </div>
      <form onSubmit={handleSubmit} className="mt-3 flex gap-2">
        <input
          className="flex-1 rounded border px-3 py-2"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Message the lead agent..."
        />
        <button type="submit" className="rounded bg-blue-600 px-4 py-2 text-white">
          Send
        </button>
      </form>
    </main>
  );
}
```

Note: the send button and input stay enabled while a turn is in flight — queuing happens server-side (Task 6's `ChatSession`), so the UI does not need to disable input.

- [ ] **Step 5: Write the frontend Dockerfile**

`apps/web/Dockerfile`:
```dockerfile
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]
```

- [ ] **Step 6: Manual verification (this is the test for this task)**

```bash
cd /home/hello/ana/apps/server
../../.venv/bin/uvicorn apps.server.app.main:app --host 0.0.0.0 --port 8000 --app-dir /home/hello/ana &
cd /home/hello/ana/apps/web
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws/chat npm run dev
```

Open `http://localhost:3000` in a browser. Type a message and send it. Confirm:
1. Header shows "connected".
2. The user message appears right-aligned immediately.
3. An agent reply streams in on the left within a few seconds.
4. Sending a second message while the first is still streaming does not error — it appears in order once the first turn's `result` event arrives.

- [ ] **Step 7: Commit**

```bash
cd /home/hello/ana
git add apps/web
git commit -m "feat: add Next.js chat UI with streaming websocket hook"
```

---

### Task 10: End-to-end verification against the real CLI

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite one more time**

Run: `.venv/bin/pytest apps/server/tests -v`
Expected: PASS, all tests, zero real API calls made (Task 4's tests use the fake CLI fixture).

- [ ] **Step 2: Verify both auth modes directly with the CLI first**

Before wiring up the full stack, confirm which auth mode actually works right now (per Global Constraints, `api` mode is unverified):

```bash
cd /home/hello/workspace
claude -p "reply with exactly the word: pong" --output-format stream-json --verbose \
  --dangerously-skip-permissions --session-id "$(python3 -c 'import uuid;print(uuid.uuid4())')"
```

This uses whatever auth is already active in the shell (Pro/OAuth if `use-claude-pro` was run, API mode if `use-claude-api` was run). If it returns `pong`, that mode works — set `CLAUDE_AUTH_MODE` in `.env` accordingly. If `api` mode errors out (auth failure, non-2xx from `panel.tapie.kr`), set `CLAUDE_AUTH_MODE=pro` in `.env` and proceed with Pro mode for this verification; file a follow-up note rather than blocking Core Loop on fixing the API proxy.

- [ ] **Step 3: Start the real stack against the real `claude` CLI**

```bash
cd /home/hello/ana
cp .env.example .env   # set ANTHROPIC_API_KEY_SAVED (if using api mode) and CLAUDE_AUTH_MODE; never commit .env
.venv/bin/uvicorn apps.server.app.main:app --host 0.0.0.0 --port 8000 --app-dir /home/hello/ana &
cd apps/web && npm run dev
```

- [ ] **Step 4: Send a real prompt through the browser UI**

Open `http://localhost:3000`, send: `reply with exactly the word: pong`

Expected: the agent's reply streams in and reads `pong`.

- [ ] **Step 5: Confirm the transcript was written**

```bash
cat /home/hello/workspace/.ana_transcript.jsonl
```

Expected: one JSON line with `"prompt"` containing the test prompt and `"response"` containing `"pong"`.

- [ ] **Step 6: Confirm queueing works against the real CLI**

Send two prompts back to back before the first one finishes streaming. Expected: both replies eventually arrive, in order, and the transcript file ends up with two lines.

- [ ] **Step 7: Commit the final state (if anything changed)**

```bash
git status
# if the manual run touched any tracked files, review with git diff, then:
git add -A
git commit -m "chore: verify core loop end-to-end against real claude CLI"
```

This task has no code changes expected under normal circumstances — it is the acceptance check for "Core Loop" as defined in the roadmap: *a prompt goes in through the browser, the lead agent (real Claude Code CLI) replies, streamed live, and every turn is durably logged.*
