"""OpenCode CLI adapter.

Fifth CLI on the same interface as ClaudeCodeAdapter / CodexAdapter /
KimiAdapter / CursorAdapter: one subprocess per turn, `-s <sessionID>` to carry
context.

Event mapping (verified against opencode 1.18.26 by running the CLI and by
reading its `run` command, which is the authority on what `--format json`
emits: `step_start`, `text`, `reasoning`, `tool_use`, `step_finish`, `error`):

    {"type":"step_start","sessionID":"ses_...","part":{"type":"step-start",...}}
    {"type":"text","sessionID":"ses_...","part":{"id":"prt_...","text":"OK",
     "time":{"start":...,"end":...}}}
    {"type":"reasoning","sessionID":"ses_...","part":{"id":"prt_...","text":"..."}}
    {"type":"tool_use","sessionID":"ses_...","part":{"type":"tool","tool":"read",
     "callID":"call_...","state":{"status":"completed",...}}}
    {"type":"step_finish","sessionID":"ses_...","part":{"type":"step-finish",
     "reason":"stop","tokens":{"input":N,"output":N,"reasoning":N,
     "cache":{"write":N,"read":N}},"cost":0}}
    {"type":"error","sessionID":"ses_...","error":{...}}

Three shapes differ from the other CLIs and drive the design here:

- **There is no result event.** Like Kimi, the stream simply ends, so the
  `result` AdapterEvent the rest of ANA expects is synthesized: the text the
  turn produced, the session id, and the token/cost totals.
- **Tokens arrive per step, not per turn.** A turn that calls tools emits one
  `step_finish` per step, so the counts are summed here. `tokens.total` is a
  running session figure rather than this turn's, so it is not used.
- **Reasoning is opt-in**: without `--thinking` the CLI drops reasoning parts
  entirely, so ANA asks for them and maps them to `reasoning` events.

A model is `provider/model` (`opencode/gpt-6-astra`), and the model half may
itself contain slashes (`omniroute/tapie/claude-opus-5`) — nothing here splits
a model name. Effort is `--variant`, OpenCode's per-provider reasoning level.

OpenCode is an HTTP server with clients bolted on, including its own CLI — but
`opencode run` without `--attach` drives that server in-process, over a direct
fetch rather than a listening socket, so a turn here opens no port and needs no
`OPENCODE_SERVER_PASSWORD`.

Auth is whatever the host `opencode providers` login wrote under the data
directory; `prepare_opencode_home` symlinks that file into a prepared HOME and
gives the subprocess its own session database so ANA's turns do not land in
the user's interactive session list. The database is also where a Console
(organization) login is kept, so that one is *not* shared by the symlink; an
operator who needs it can point OPENCODE_DB at the host database, which this
adapter passes through from the environment untouched. The CLI has no PreToolUse hook, so tool
calls are auto-approved with `--auto` (parity with the other CLIs' blanket
flags: without it the CLI *rejects* every permission request and the turn
stalls); the container boundary remains the enforcement point when workspace
containers are on.
"""
import asyncio
import json
import os
from functools import lru_cache
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter


def default_executable() -> str:
    return os.environ.get("OPENCODE_EXECUTABLE") or "opencode"


@lru_cache(maxsize=1)
def opencode_available(executable: Optional[str] = None) -> bool:
    """Whether the OpenCode CLI is on PATH (or OPENCODE_EXECUTABLE)."""
    import shutil

    return shutil.which(executable or default_executable()) is not None


def _tokens_from_step(part: dict) -> dict[str, int]:
    """One `step-finish` part's token counts, under the snake_case names the
    rest of ANA already stores for Claude/Codex/Kimi/Cursor turns."""
    tokens = part.get("tokens") or {}
    cache = tokens.get("cache") or {}

    def _int(value) -> int:
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "input_tokens": _int(tokens.get("input")),
        "output_tokens": _int(tokens.get("output")),
        "reasoning_tokens": _int(tokens.get("reasoning")),
        "cache_read_input_tokens": _int(cache.get("read")),
        "cache_creation_input_tokens": _int(cache.get("write")),
    }


def _error_message(error) -> str:
    """The readable half of a `session.error` payload.

    The CLI itself prints `error.data.message` when there is one and falls back
    to `error.name`, so the same order is used here rather than dumping JSON at
    the user."""
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if error.get("name"):
            return str(error["name"])
        if error.get("message"):
            return str(error["message"])
    return str(error) if error else "opencode 오류"


class OpenCodeAdapter(AgentAdapter):
    """Wraps the `opencode run` CLI as a one-subprocess-per-turn adapter."""

    def __init__(
        self,
        workspace_dir: str,
        env: dict[str, str],
        executable: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        command_prefix: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable or default_executable()
        self.model = model
        # `--variant` is OpenCode's reasoning level, and which names a model
        # accepts is the model's own business (`opencode models --verbose`
        # lists each one's `variants`). An unknown name is rejected by the CLI,
        # which is why the routing editor only offers the discovered ones.
        self.effort = effort
        self.command_prefix = list(command_prefix or [])
        # No --append-system-prompt on a one-shot run, so the same instructions
        # ride in front of the prompt (Codex/Kimi/Cursor pattern).
        self.system_prompt = system_prompt
        self._process: Optional[asyncio.subprocess.Process] = None
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> list[str]:
        args = [*self.command_prefix, self.executable, *self._extra_args_prefix]
        args += [
            "run",
            "--format",
            "json",
            # Auto-approve tool calls: non-interactive parity with
            # --dangerously-skip-permissions / --force on the other CLIs.
            # Without it the CLI answers its own permission prompts with
            # "reject" and the turn produces nothing.
            "--auto",
            # Reasoning parts are dropped entirely unless asked for.
            "--thinking",
            "--dir",
            self.workspace_dir,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--variant", self.effort]
        if session_id:
            args += ["--session", session_id]
        text = (
            f"[시스템 지시]\n{self.system_prompt}\n\n{prompt}"
            if self.system_prompt
            else prompt
        )
        # After `--`, so a prompt that happens to start with a dash is a
        # message rather than an unknown flag. The CLI joins the positional
        # message and everything past `--` into one prompt.
        args += ["--", text]
        return args

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        args = self._build_args(prompt, session_id)
        resolved_session_id = session_id

        self._process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace_dir,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
        )

        assert self._process.stdout is not None
        stderr_task = (
            asyncio.create_task(self._process.stderr.read())
            if self._process.stderr is not None
            else None
        )

        # There is no result event, so the turn's answer and its cost are
        # accumulated as the stream goes by.
        answer: list[str] = []
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        cost_usd = 0.0
        steps = 0
        # How much of each text/reasoning part has already been sent. The CLI
        # emits a part once its `time.end` is set, but a part is identified by
        # id and nothing promises it is emitted exactly once — so a repeat
        # yields only what is new instead of the whole paragraph twice.
        emitted: dict[str, int] = {}
        failed = False
        announced_model = False

        async for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            sid = event.get("sessionID") or resolved_session_id
            if sid:
                resolved_session_id = sid
            event_type = event.get("type")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}

            if event_type == "step_start":
                # The JSON stream never names the model, so the one ANA asked
                # for is what it reports — honest, since an unknown name fails
                # the turn rather than being quietly substituted.
                if self.model and not announced_model:
                    announced_model = True
                    yield AdapterEvent(
                        type="model",
                        text=self.model,
                        session_id=resolved_session_id,
                        model=self.model,
                        raw=event,
                    )
                continue

            if event_type in ("text", "reasoning"):
                text = part.get("text") or ""
                if not text:
                    continue
                part_id = str(part.get("id") or "")
                already = emitted.get(part_id, 0) if part_id else 0
                if already >= len(text):
                    continue
                chunk = text[already:]
                if part_id:
                    emitted[part_id] = len(text)
                if event_type == "text":
                    answer.append(chunk)
                yield AdapterEvent(
                    type="text" if event_type == "text" else "reasoning",
                    text=chunk,
                    session_id=resolved_session_id,
                    raw=event,
                )
                continue

            if event_type == "step_finish":
                steps += 1
                for key, value in _tokens_from_step(part).items():
                    totals[key] += value
                step_cost = part.get("cost")
                if isinstance(step_cost, (int, float)):
                    cost_usd += float(step_cost)
                continue

            if event_type == "error":
                failed = True
                yield AdapterEvent(
                    type="error",
                    text=_error_message(event.get("error")),
                    session_id=resolved_session_id,
                    is_error=True,
                    raw=event,
                )
                continue

            # tool_use is left to the gate and the transcript: ANA shows tool
            # calls from its own records, and OpenCode's payload carries whole
            # file contents in `state.output`.

        stderr_bytes = await stderr_task if stderr_task is not None else b""
        return_code = await self._process.wait()

        if return_code != 0 and not failed:
            yield AdapterEvent(
                type="error",
                text=stderr_bytes.decode("utf-8", errors="replace")
                or f"opencode exited with code {return_code}",
                session_id=resolved_session_id,
                is_error=True,
            )
            return

        yield AdapterEvent(
            type="result",
            text="".join(answer),
            session_id=resolved_session_id,
            is_error=failed,
            # No step means no measurement: an empty usage dict would be read
            # as "this turn cost nothing", which is a different claim.
            usage=dict(totals) if steps else None,
            cost_usd=cost_usd if steps else None,
        )

    async def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
