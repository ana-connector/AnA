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
        # Drain stderr concurrently with stdout: both streams are piped, and
        # if the child writes more than one OS pipe buffer's worth to stderr
        # (~64KiB on Linux) while stdout is still open, it blocks on that
        # write. Reading stderr only after the stdout loop finishes would
        # then deadlock forever, since the child can't reach stdout EOF while
        # stuck writing stderr that nobody is draining.
        stderr_task = (
            asyncio.create_task(self._process.stderr.read())
            if self._process.stderr is not None
            else None
        )

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

        stderr_bytes = await stderr_task if stderr_task is not None else b""
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
