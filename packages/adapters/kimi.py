"""Kimi Code CLI adapter.

Third CLI on the same interface as ClaudeCodeAdapter and CodexAdapter: one
subprocess per turn, `--session <id>` to carry context instead of keeping a
process alive.

Event mapping (verified against kimi 2.0.2 on 2026-09-23):

    {"role":"meta","type":"system.version","version":"2.0.2"}
    {"role":"assistant","tool_calls":[{"function":{"name":"Bash", ...}}]}
    {"role":"tool","tool_call_id":"...","content":"hello from file\\n"}
    {"role":"assistant","content":"The command printed: ..."}
    {"role":"meta","type":"session.resume_hint","session_id":"session_...",
     "command":"kimi -r session_..."}

Two shapes differ from the other CLIs and drive the design here:

- **There is no result event.** The stream simply ends. The adapter
  synthesizes the `result` AdapterEvent the rest of ANA expects, carrying the
  session id and whatever text the turn produced.
- **There is no usage event either**, but the CLI records token counts in its
  session log, the way Codex records rate limits in its rollout. They are read
  back once the turn is over (see packages/observability/kimi_usage.py), so a
  Kimi turn still counts against the thread's token budget.

Ordinary tool calls are surfaced by the gate. AgentSwarm additionally emits
sub-agent lifecycle events here because Kimi does not send SubagentStart/Stop
hooks to ANA for its swarm children.
"""
import asyncio
import contextlib
import json
import os
import signal
import time
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter
from packages.observability.kimi_usage import read_last_error, read_session_tokens


class KimiAdapter(AgentAdapter):
    """Wraps the `kimi` CLI as a one-subprocess-per-turn adapter."""

    # How long output written around the CLI's exit is still waited for. Short
    # on purpose: past this point what is left on the pipe belongs to a child
    # that outlived its parent, not to the turn.
    EXIT_GRACE_SECONDS = 2.0
    # How often the CLI's liveness is rechecked while a read is outstanding.
    POLL_SECONDS = 0.1

    def __init__(
        self,
        workspace_dir: str,
        env: dict[str, str],
        executable: str = "kimi",
        model: Optional[str] = None,
        command_prefix: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable
        self.model = model
        # Prepended to argv so the CLI runs somewhere else — `docker exec` into
        # the workspace's own container. Empty means "run it here".
        self.command_prefix = list(command_prefix or [])
        # Kimi has no --append-system-prompt for a one-shot run (the closest
        # thing, --agent-file, replaces the agent), so the instructions ride in
        # front of the prompt as they do for Codex.
        self.system_prompt = system_prompt
        # Effort is not a flag: it lives in config.toml under [thinking], which
        # is why each effort level gets its own prepared KIMI_CODE_HOME (see
        # packages/governance/cli_config.py). Kept for the failover path, which
        # sets `.effort` on an adapter it reuses.
        self.effort: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        # test-only hook, same contract as the other two adapters.
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> list[str]:
        args = [*self.command_prefix, self.executable, *self._extra_args_prefix]
        if session_id:
            # `--session <id>`; `-r` is a hidden alias for the same flag.
            args += ["--session", session_id]
        if self.model:
            args += ["--model", self.model]
        # -p implies the `auto` permission policy: no approval prompts, and
        # the PreToolUse hook is what screens each call. The CLI rejects
        # --yolo/--auto together with -p, so there is nothing else to pass.
        args += ["--output-format", "stream-json", "-p"]
        args += [
            f"[시스템 지시]\n{self.system_prompt}\n\n{prompt}"
            if self.system_prompt
            else prompt
        ]
        return args

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        args = self._build_args(prompt, session_id)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace_dir,
            env=self.env,
            # DEVNULL for the same reason as CodexAdapter: an inherited stdin
            # is a pipe nobody writes to, and a CLI that decides to read it
            # waits for input that never comes.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,  # same rationale as the other adapters: a
            # single JSONL line carries a whole assistant message.
            # Its own process group, so terminate() can take the MCP servers
            # and shells kimi spawned with it. Those children inherit this
            # process's stdout and stderr, and an orphan holding the write end
            # open is what used to keep the stream below from ever ending.
            start_new_session=True,
        )

        assert self._process.stdout is not None
        # Drained concurrently for the same reason as the other two adapters:
        # a child that fills the stderr pipe while stdout is still open blocks
        # forever if nobody reads it.
        stderr_task = (
            asyncio.create_task(self._process.stderr.read())
            if self._process.stderr is not None
            else None
        )

        resolved_session_id = session_id
        answer: list[str] = []
        swarms: dict[str, list[dict]] = {}

        async for raw_line in self._readlines():
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("role") == "assistant":
                for call in event.get("tool_calls") or []:
                    function = call.get("function") or {}
                    if function.get("name") != "AgentSwarm":
                        continue
                    call_id = call.get("id")
                    if not call_id:
                        continue
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        continue
                    items = arguments.get("items") or []
                    if not isinstance(items, list):
                        continue
                    tasks = []
                    for index, item in enumerate(items):
                        title = str(item).strip().splitlines()[0][:120] or "하위 에이전트"
                        task = {
                            "id": f"native:kimi:{call_id}:{index}",
                            "title": title,
                            "agent": "kimi",
                            "status": "running",
                            "output": "",
                            "tools": [],
                        }
                        tasks.append(task)
                        yield AdapterEvent(type="subagent", raw={"task": dict(task)})
                    swarms[call_id] = tasks
            elif event.get("role") == "tool":
                call_id = event.get("tool_call_id")
                if call_id in swarms:
                    result = event.get("content") or ""
                    failed = bool(event.get("is_error"))
                    for task in swarms.pop(call_id):
                        task.update(status="error" if failed else "done", output=str(result)[:4000])
                        yield AdapterEvent(type="subagent", raw={"task": dict(task)})
            parsed, resolved_session_id = self._parse_event(event, resolved_session_id)
            if parsed is None:
                continue
            if parsed.type == "text":
                answer.append(parsed.text)
            yield parsed

        # wait() first, and the stderr read second with a bound: an orphaned
        # child (an MCP server kimi started and did not reap) inherits this
        # pipe, so reading it to EOF can outlive the CLI by as long as that
        # child lives. This used to hang the turn here forever, and with the
        # lead parked on its delegate call the whole task read as "running"
        # until the lead's own 30-minute HTTP timeout fired.
        return_code = await self._exit_code()
        stderr_bytes = await self._drain(stderr_task)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        # A refused request — an exhausted 5-hour window above all — can leave
        # the CLI exiting 0 with an empty stream. Reported as an error, because
        # a silent success is the one outcome the failover ladder cannot act on:
        # it would hand the lead an empty answer and move on.
        quiet_failure = ""
        if return_code == 0 and not answer:
            quiet_failure = self._log_error(resolved_session_id) or (
                "Kimi produced no output and reported no result"
            )

        if return_code != 0 or quiet_failure:
            for tasks in swarms.values():
                for task in tasks:
                    task.update(status="error", output="Kimi exited before the swarm completed")
                    yield AdapterEvent(type="subagent", raw={"task": dict(task)})
            yield AdapterEvent(
                type="error",
                text="\n".join(part for part in (stderr_text, quiet_failure) if part),
                session_id=resolved_session_id,
                is_error=True,
            )
            return

        for tasks in swarms.values():
            for task in tasks:
                task.update(status="unknown", output="No AgentSwarm result was reported")
                yield AdapterEvent(type="subagent", raw={"task": dict(task)})

        # The CLI ends the stream without a summary, so the result event the
        # graph records the turn from is assembled here. Cost is None: the
        # subscription reports no per-turn price, and inventing one would put
        # a fabricated number in the thread's total.
        yield AdapterEvent(
            type="result",
            text="".join(answer),
            session_id=resolved_session_id,
            cost_usd=None,
            is_error=False,
            usage=self._read_usage(resolved_session_id),
        )

    async def _readlines(self) -> AsyncIterator[bytes]:
        """Stdout, line by line, ending when the CLI does.

        `async for line in process.stdout` ends on EOF, and EOF needs *every*
        holder of the write end to close it. Kimi starts MCP servers and shells
        that inherit this pipe, so a child it fails to reap keeps the stream
        open after the CLI itself is gone: the read never returns and the worker
        never finishes. `Process.wait()` is no help — asyncio resolves it only
        once every pipe has disconnected, so it hangs on exactly the same
        orphan. `Process.returncode` is set as soon as the child is reaped, so
        that is what this watches, with a short grace period after the exit so
        output already on the pipe is not thrown away."""
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        while True:
            read = asyncio.ensure_future(stdout.readline())
            line = await self._next_line(read)
            if not line:  # real EOF, or the CLI is gone and nothing followed
                return
            yield line

    async def _next_line(self, read: asyncio.Future) -> bytes:
        """The pending read's line, or b"" once waiting for it is pointless."""
        exited_at: Optional[float] = None
        while True:
            done, _ = await asyncio.wait({read}, timeout=self.POLL_SECONDS)
            if done:
                return read.result()
            if self._process is None or self._process.returncode is None:
                continue
            now = time.monotonic()
            if exited_at is None:
                exited_at = now
            elif now - exited_at > self.EXIT_GRACE_SECONDS:
                read.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await read
                return b""

    async def _exit_code(self) -> int:
        """The CLI's exit status, without waiting on its pipes.

        Same reason as _readlines: `wait()` needs every pipe disconnected, and
        a leaked child holding one would make reading the status outlast the
        turn. A process that somehow has not been reaped counts as success — the
        stream already ended, and inventing a failure would fail a turn that
        produced a real answer."""
        assert self._process is not None
        deadline = time.monotonic() + self.EXIT_GRACE_SECONDS
        while self._process.returncode is None and time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_SECONDS)
        return self._process.returncode or 0

    async def _drain(self, stderr_task: Optional[asyncio.Task]) -> bytes:
        """Stderr, bounded. See the comment at the call site: an orphan holding
        the pipe open must cost this turn a couple of seconds, not its life."""
        if stderr_task is None:
            return b""
        try:
            return await asyncio.wait_for(
                asyncio.shield(stderr_task), timeout=self.EXIT_GRACE_SECONDS
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()
            return b""

    def _log_error(self, session_id: Optional[str]) -> str:
        if not session_id:
            return ""
        try:
            return read_last_error(session_id, kimi_home=self.env.get("KIMI_CODE_HOME")) or ""
        except Exception:  # noqa: BLE001 - diagnosing a failure must not fail
            return ""

    def _read_usage(self, session_id: Optional[str]) -> Optional[dict]:
        if not session_id:
            return None
        try:
            return read_session_tokens(session_id, kimi_home=self.env.get("KIMI_CODE_HOME"))
        except Exception:  # noqa: BLE001 - usage accounting must never fail a turn
            return None

    @staticmethod
    def _parse_event(
        event: dict, session_id: Optional[str]
    ) -> tuple[Optional[AdapterEvent], Optional[str]]:
        """One CLI event to (AdapterEvent | None, session id).

        The session id arrives near the *end* of the stream (in the resume
        hint), not at the start as it does for the other two CLIs, so it is
        threaded through rather than fixed up front."""
        role = event.get("role")
        if role == "meta":
            if event.get("type") == "session.resume_hint" and event.get("session_id"):
                return None, event["session_id"]
            return None, session_id
        if role == "assistant":
            text = event.get("content")
            if isinstance(text, str) and text:
                return (
                    AdapterEvent(type="text", text=text, session_id=session_id, raw=event),
                    session_id,
                )
            # tool_calls: reported to the UI by the gate, not from here.
            return None, session_id
        if role == "tool":
            return None, session_id
        return None, session_id

    async def terminate(self) -> None:
        if self._process is None or self._process.returncode is not None:
            # The CLI is already gone, but the MCP servers and shells it started
            # may not be: they are in its process group, and an orphan that
            # keeps running is what leaves `uvx` servers alive for days.
            self._signal_group(signal.SIGTERM)
            return
        # The whole group, not just the CLI: signalling only the parent leaves
        # its children holding this turn's pipes open.
        if not self._signal_group(signal.SIGTERM):
            self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except asyncio.TimeoutError:
            if not self._signal_group(signal.SIGKILL):
                self._process.kill()
            await self._process.wait()

    def _signal_group(self, sig: int) -> bool:
        """Signals the CLI's process group, reporting whether it could.

        False on a platform without process groups, or when the group is
        already gone — both mean "fall back to signalling the process", not
        "something went wrong"."""
        if self._process is None or not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(os.getpgid(self._process.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def default_executable() -> str:
    """`kimi` if it is on PATH; otherwise the path the official installer
    uses. The install script adds `~/.kimi-code/bin` to the *shell's* rc file,
    which a service started outside a login shell never reads."""
    return os.environ.get("KIMI_EXECUTABLE") or "kimi"
