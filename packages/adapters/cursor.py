"""Cursor Agent CLI adapter.

Fourth CLI on the same interface as ClaudeCodeAdapter / CodexAdapter /
KimiAdapter: one subprocess per turn, `--resume <chatId>` to carry context.

Event mapping (verified against agent 2026.10.16-e3cb207):

    {"type":"system","subtype":"init","session_id":"...","model":"Auto",
     "apiKeySource":"login",...}
    {"type":"thinking","subtype":"delta","text":"..."}
    {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
    {"type":"result","subtype":"success","result":"...","session_id":"...",
     "usage":{"inputTokens":N,"outputTokens":N,...}}

Auth is the host login session (`~/.config/cursor/auth.json`), not an API key:
prepare_cursor_home() points HOME at an ANA directory that symlinks that file
so an OAuth refresh stays shared. The CLI has no PreToolUse hook, so tool
calls are auto-approved with `--force` (parity with the other CLIs'
blanket-approval flags); the container boundary remains the enforcement
point when workspace containers are on.
"""
import asyncio
import json
import os
from functools import lru_cache
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter


def default_executable() -> str:
    return os.environ.get("CURSOR_EXECUTABLE") or "agent"


@lru_cache(maxsize=1)
def cursor_available(executable: Optional[str] = None) -> bool:
    """Whether the Cursor Agent CLI is on PATH (or CURSOR_EXECUTABLE)."""
    import shutil

    return shutil.which(executable or default_executable()) is not None


def _normalize_usage(usage: Optional[dict]) -> Optional[dict]:
    """Maps Cursor's camelCase token fields onto the snake_case names the
    rest of ANA already stores for Claude/Codex/Kimi turns."""
    if not usage:
        return usage
    out = dict(usage)
    for src, dst in (
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("cacheReadTokens", "cache_read_input_tokens"),
        ("cacheWriteTokens", "cache_creation_input_tokens"),
    ):
        if src in usage and dst not in out:
            out[dst] = usage[src]
    return out


class CursorAdapter(AgentAdapter):
    """Wraps the `agent` (Cursor Agent) CLI as a one-subprocess-per-turn adapter."""

    def __init__(
        self,
        workspace_dir: str,
        env: dict[str, str],
        executable: Optional[str] = None,
        model: Optional[str] = None,
        command_prefix: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable or default_executable()
        self.model = model
        # Cursor has no separate --effort flag; effort rides inside a
        # parameterized --model string when the caller wants one. Kept so the
        # failover path can assign `.effort` the way it does on the others.
        self.effort: Optional[str] = None
        self.command_prefix = list(command_prefix or [])
        # No --append-system-prompt on a one-shot print run, so the same
        # instructions ride in front of the prompt (Codex/Kimi pattern).
        self.system_prompt = system_prompt
        self._process: Optional[asyncio.subprocess.Process] = None
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> list[str]:
        args = [*self.command_prefix, self.executable, *self._extra_args_prefix]
        args += [
            "-p",
            "--output-format",
            "stream-json",
            # Auto-approve tool calls and trust the workspace: non-interactive
            # parity with --dangerously-skip-permissions / --force on the
            # other CLIs. Cursor has no PreToolUse hook for ANA's gate.
            "--force",
            "--trust",
            "--sandbox",
            "disabled",
            "--approve-mcps",
            "--workspace",
            self.workspace_dir,
        ]
        if self.model:
            args += ["--model", self.model]
        if session_id:
            args += ["--resume", session_id]
        args.append(
            f"[시스템 지시]\n{self.system_prompt}\n\n{prompt}"
            if self.system_prompt
            else prompt
        )
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

        async for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_sid = raw_event.get("session_id") or resolved_session_id
            if event_sid:
                resolved_session_id = event_sid
            parsed = self._parse_event(raw_event, resolved_session_id or "")
            if parsed is not None:
                if parsed.session_id:
                    resolved_session_id = parsed.session_id
                yield parsed

        stderr_bytes = await stderr_task if stderr_task is not None else b""
        return_code = await self._process.wait()
        if return_code != 0:
            yield AdapterEvent(
                type="error",
                text=stderr_bytes.decode("utf-8", errors="replace")
                or f"agent exited with code {return_code}",
                session_id=resolved_session_id,
                is_error=True,
            )

    @staticmethod
    def _parse_event(event: dict, session_id: str) -> Optional[AdapterEvent]:
        event_type = event.get("type")
        sid = event.get("session_id") or session_id

        if event_type == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if model:
                return AdapterEvent(
                    type="model", text=model, session_id=sid, model=model, raw=event
                )
            return None

        if event_type == "thinking":
            if event.get("subtype") == "delta" and event.get("text"):
                return AdapterEvent(
                    type="reasoning", text=event["text"], session_id=sid, raw=event
                )
            return None

        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text:
                return AdapterEvent(type="text", text=text, session_id=sid, raw=event)
            return None

        if event_type == "result":
            return AdapterEvent(
                type="result",
                text=event.get("result", "") or "",
                session_id=sid,
                is_error=bool(event.get("is_error")),
                usage=_normalize_usage(event.get("usage")),
                raw=event,
            )

        return None

    async def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
