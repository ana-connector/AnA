import asyncio
import json
import subprocess
import uuid
from functools import lru_cache
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter


@lru_cache(maxsize=4)
def supports_permission_prompts(executable: str = "claude") -> bool:
    """Whether this build of the CLI accepts --permission-prompts.

    Builds of the *same version* differ: the native binary (2.1.197) has the
    flag while the npm package of that version does not, and passing it there
    kills the run with "error: unknown option '--permission-prompts'". Probed
    once per process rather than assumed, so the same ANA image works against
    either install."""
    try:
        result = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, timeout=30
        )
    except Exception:  # noqa: BLE001 - probe failure just means "don't pass it"
        return False
    return "--permission-prompts" in (result.stdout + result.stderr)


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
        settings_path: Optional[str] = None,
        permission_prompts: Optional[str] = None,
        command_prefix: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
        mcp_config_path: Optional[str] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable
        self.model = model
        self.effort = effort
        # Path to the --settings file carrying ANA's PreToolUse hook. When
        # set, the CLI runs *without* --dangerously-skip-permissions and every
        # tool call is screened by the governance gate instead.
        self.settings_path = settings_path
        # "none" makes anything the hook did not explicitly allow a denial
        # rather than a prompt nobody can answer. Not every build has the
        # flag (the npm package lacks it while the native installer's binary
        # of the same version has it), so callers pass it only when the
        # installed CLI advertises it — see supports_permission_prompts().
        self.permission_prompts = permission_prompts
        # Prepended to argv so the CLI runs somewhere else — `docker exec` into
        # the workspace's own container. Empty means "run it here".
        self.command_prefix = list(command_prefix or [])
        # Appended to the CLI's own system prompt, which is what the flag is
        # for — ANA adds instructions rather than replacing the agent's.
        self.system_prompt = system_prompt
        # MCP servers this role is scoped for, as a --mcp-config file. Passed
        # with --strict-mcp-config so the CLI does not *also* load the host
        # user's own MCP servers, which would make the scoping advisory
        # (see packages/mcp/).
        self.mcp_config_path = mcp_config_path
        self._process: Optional[asyncio.subprocess.Process] = None
        # test-only hook: extra argv inserted between the executable and the
        # rest of the args, used by tests to point at fake_claude.py without
        # changing production argv construction.
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> tuple[list[str], str]:
        args = [*self.command_prefix, self.executable, *self._extra_args_prefix, "-p", prompt]
        args += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if self.settings_path:
            # The hook decides every tool call, so the CLI must not bypass
            # permissions.
            args += ["--settings", self.settings_path]
            if self.permission_prompts:
                args += ["--permission-prompts", self.permission_prompts]
        else:
            args += ["--dangerously-skip-permissions"]
        if self.mcp_config_path:
            args += ["--mcp-config", self.mcp_config_path, "--strict-mcp-config"]
        if self.system_prompt:
            args += ["--append-system-prompt", self.system_prompt]
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
            limit=10 * 1024 * 1024,  # 10 MiB; default 64KiB is exceeded by
            # this machine's global Claude Code hooks, which inject large
            # hook_response payloads into every stream-json line (see plan's
            # "Empirically Verified CLI Behavior"), and by long assistant
            # replies / result events independently.
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

        streamed_text = False
        async for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw_event.get("type") == "stream_event":
                delta = raw_event.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed_text = True
                    yield AdapterEvent(type="text", text=delta["text"], session_id=resolved_session_id, raw=raw_event)
                continue
            parsed = self._parse_event(raw_event, resolved_session_id)
            if streamed_text and raw_event.get("type") == "assistant" and parsed and parsed.type == "text":
                # With partial messages enabled Claude also sends the complete
                # assistant message. Its text has already reached the browser.
                streamed_text = False
                continue
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
        if event_type == "system" and event.get("subtype") == "init":
            # The only place the CLI states which model this session runs.
            model = event.get("model")
            if model:
                return AdapterEvent(type="model", text=model, session_id=session_id, model=model, raw=event)
            return None
        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            text = "".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
            if text:
                return AdapterEvent(type="text", text=text, session_id=session_id, raw=event)
            # Extended thinking arrives as its own block type. It is shown
            # separately from the answer rather than dropped: "what is it doing
            # right now" is most of what a user wants during a long turn.
            thinking = "".join(
                block.get("thinking", "")
                for block in content
                if block.get("type") == "thinking"
            )
            if thinking:
                return AdapterEvent(
                    type="reasoning", text=thinking, session_id=session_id, raw=event
                )
            return None
        if event_type == "result":
            return AdapterEvent(
                type="result",
                text=event.get("result", ""),
                session_id=event.get("session_id", session_id),
                cost_usd=event.get("total_cost_usd"),
                is_error=bool(event.get("is_error")),
                usage=event.get("usage"),
                raw=event,
            )
        if event_type == "rate_limit_event":
            return AdapterEvent(
                type="usage",
                session_id=session_id,
                # rate_limit_event carries window utilization, not token
                # counts; pass whatever it has through unchanged.
                usage=event.get("rate_limit_info"),
                raw=event,
            )
        return None  # remaining "system" (hook) noise — see planning notes at top of this file

    async def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
