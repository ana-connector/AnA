import asyncio
import json
from typing import AsyncIterator, Optional

from packages.adapters.base import AdapterEvent, AgentAdapter


class CodexAdapter(AgentAdapter):
    """Wraps the `codex` CLI as a one-subprocess-per-turn adapter, mirroring
    ClaudeCodeAdapter: `codex exec --json` for a fresh session and
    `codex exec resume <session_id>` to carry context across turns, instead
    of keeping an OS process alive.

    Event mapping (verified against codex exec --json on 2026-09-22, see
    docs/plans/2026-09-22-dual-cli-orchestration.md):
      thread.started  -> session id for this turn
      item.completed  -> "text", when item.type == "agent_message"
      turn.completed  -> "usage" (token counts) then "result"
      turn.failed     -> "error"
    """

    def __init__(
        self,
        workspace_dir: str,
        env: dict[str, str],
        executable: str = "codex",
        model: Optional[str] = None,
        sandbox: Optional[str] = None,
        hooks_enabled: bool = False,
        command_prefix: Optional[list[str]] = None,
        system_prompt: Optional[str] = None,
        mcp_args: Optional[list[str]] = None,
    ):
        self.workspace_dir = workspace_dir
        self.env = env
        self.executable = executable
        self.model = model
        # True when env["CODEX_HOME"] points at an ANA-prepared home carrying
        # hooks.json. Hooks in automation need --dangerously-bypass-hook-trust
        # (that flag skips the *hook trust prompt*, not the sandbox), and with
        # the gate screening every call the blanket approval bypass is dropped.
        self.hooks_enabled = hooks_enabled
        # Prepended to argv so the CLI runs somewhere else — `docker exec` into
        # the workspace's own container. Empty means "run it here".
        self.command_prefix = list(command_prefix or [])
        # The Codex CLI has no --append-system-prompt for a one-shot run, so
        # the same instructions ride in front of the prompt instead.
        self.system_prompt = system_prompt
        # `-c mcp_servers.<name>.command=...` overrides for the servers this
        # role is scoped for. They point at launcher scripts, so no secret
        # ever reaches argv (see packages/mcp/launcher.py). Codex has no
        # --strict flag; ANA's prepared CODEX_HOME defines no MCP servers of
        # its own, so these overrides are the whole list.
        self.mcp_args = list(mcp_args or [])
        # None means: pass --dangerously-bypass-approvals-and-sandbox, the
        # parity default with ClaudeCodeAdapter's --dangerously-skip-permissions.
        # A value ("read-only" | "workspace-write" | "danger-full-access")
        # passes -s <value> instead.
        self.sandbox = sandbox
        self._process: Optional[asyncio.subprocess.Process] = None
        # test-only hook, same contract as ClaudeCodeAdapter._extra_args_prefix:
        # extra argv inserted between the executable and the rest of the args
        # so tests can point at fake_codex.py without changing production argv.
        self._extra_args_prefix: list[str] = []

    def _build_args(self, prompt: str, session_id: Optional[str]) -> list[str]:
        resuming = bool(session_id)
        args = [*self.command_prefix, self.executable, *self._extra_args_prefix, "exec"]
        if resuming:
            # `codex exec resume <SESSION_ID> <PROMPT>`: the subcommand and
            # its positional session id must precede the prompt.
            args += ["resume", session_id]
        args += [
            "--json",
            "--skip-git-repo-check",  # the workspace need not be a git repo
        ]
        # `codex exec resume` accepts neither -C/--cd nor -s/--sandbox (it
        # errors with "unexpected argument '-C' found" -- verified against
        # the installed CLI). The working directory is already set via the
        # subprocess's own cwd, so -C is only an explicit restatement of it
        # on the fresh-session path, and the sandbox mode goes through the
        # -c config override, which resume does accept.
        if not resuming:
            args += ["-C", self.workspace_dir]
        if self.hooks_enabled:
            args += ["--dangerously-bypass-hook-trust"]
            if self.sandbox == "workspace-write":
                # The workspace-write sandbox blocks network access by
                # default, which would make an egress command fail *after*
                # the user approved it at the gate — confusing, and it moves
                # the real decision away from the gate. With every tool call
                # screened, the gate is the control point for egress.
                args += ["-c", "sandbox_workspace_write.network_access=true"]
        if self.sandbox:
            if resuming:
                # -c values are parsed as TOML, so the mode must be quoted.
                args += ["-c", f'sandbox_mode="{self.sandbox}"']
            else:
                args += ["-s", self.sandbox]
        else:
            # No sandbox mode configured. The flag name is alarming, but it
            # turns off the CLI's *own* sandbox and approval prompts — the
            # PreToolUse hook still runs and can still deny (verified), so
            # with the gate on the enforcement point simply moves to ANA plus
            # the container. This is also the only workable setting inside
            # Docker: codex's sandbox shells out to bubblewrap, which needs
            # unprivileged user namespaces and fails there with "bwrap: No
            # permissions to create a new namespace". Set CODEX_SANDBOX
            # explicitly on a host where bubblewrap works.
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        if self.model:
            args += ["--model", self.model]
        args += self.mcp_args
        args += [
            f"[시스템 지시]\n{self.system_prompt}\n\n{prompt}"
            if self.system_prompt
            else prompt
        ]
        return args

    async def run_stream(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        """One turn. A resume whose rollout the CLI cannot find is retried as a
        fresh session rather than surfaced as an error: the session id was
        recorded by ANA, and if the CLI has since lost the file (a changed
        CODEX_HOME, a cleaned-up session directory) the conversation should
        continue, not die on bookkeeping."""
        saw_missing_rollout = False
        async for event in self._run_once(prompt, session_id):
            if (
                session_id
                and event.type == "error"
                and _is_missing_rollout(event.text)
            ):
                saw_missing_rollout = True
                continue
            yield event
        if saw_missing_rollout:
            async for event in self._run_once(prompt, None):
                yield event

    async def _run_once(
        self, prompt: str, session_id: Optional[str] = None
    ) -> AsyncIterator[AdapterEvent]:
        args = self._build_args(prompt, session_id)

        self._process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace_dir,
            env=self.env,
            # `codex exec` appends piped stdin to the prompt as an <stdin>
            # block, and blocks waiting for EOF when stdin is an open pipe
            # ("Reading additional input from stdin..."). DEVNULL is what
            # keeps a turn from hanging forever before it starts.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Same 10 MiB rationale as ClaudeCodeAdapter: a single JSONL line
            # holding a long agent message or a full command-execution
            # payload exceeds asyncio's 64KiB default line limit.
            limit=10 * 1024 * 1024,
        )

        assert self._process.stdout is not None
        # Drain stderr concurrently with stdout. The codex CLI writes model
        # -manager warnings and progress noise to stderr (observed:
        # "ERROR codex_models_manager: failed to refresh available models"),
        # and a child blocked writing a full stderr pipe can never reach
        # stdout EOF — the same deadlock ClaudeCodeAdapter guards against.
        stderr_task = (
            asyncio.create_task(self._process.stderr.read())
            if self._process.stderr is not None
            else None
        )

        resolved_session_id = session_id
        final_text = ""
        streamed_items: dict[str, str] = {}

        async for raw_line in self._process.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError:
                # The CLI prefixes stdout with human-readable lines in some
                # modes (e.g. "Reading additional input from stdin..."),
                # which are not JSON and carry nothing we need.
                continue

            event_type = raw_event.get("type")

            if event_type == "thread.started":
                resolved_session_id = raw_event.get("thread_id") or resolved_session_id
                if self.model:
                    # The CLI's JSONL never names the model, so this is only
                    # known when ANA pinned one. Otherwise the gate learns it
                    # from the hook payload, which does carry it.
                    yield AdapterEvent(
                        type="model",
                        text=self.model,
                        session_id=resolved_session_id,
                        model=self.model,
                    )
                continue

            if event_type == "item.updated":
                item = raw_event.get("item", {})
                if item.get("type") == "agent_message" and item.get("id") and isinstance(item.get("text"), str):
                    item_id = item["id"]
                    previous = streamed_items.get(item_id, "")
                    current = item["text"]
                    if current.startswith(previous) and len(current) > len(previous):
                        delta = current[len(previous):]
                        streamed_items[item_id] = current
                        final_text += delta
                        yield AdapterEvent(type="text", text=delta, session_id=resolved_session_id, raw=raw_event)
                continue

            if event_type == "item.completed":
                item = raw_event.get("item", {})
                if item.get("type") == "reasoning":
                    text = item.get("text") or item.get("summary") or ""
                    if text:
                        yield AdapterEvent(
                            type="reasoning",
                            text=text,
                            session_id=resolved_session_id,
                            raw=raw_event,
                        )
                    continue
                if item.get("type") != "agent_message":
                    # reasoning / command_execution / file_change items are
                    # progress detail, not assistant content.
                    continue
                text = item.get("text", "")
                if not text:
                    continue
                previous = streamed_items.pop(item.get("id", ""), "")
                if previous and text.startswith(previous):
                    text = text[len(previous):]
                if not text:
                    continue
                final_text += text
                yield AdapterEvent(
                    type="text",
                    text=text,
                    session_id=resolved_session_id,
                    raw=raw_event,
                )
                continue

            if event_type == "turn.completed":
                usage = raw_event.get("usage")
                if usage:
                    yield AdapterEvent(
                        type="usage",
                        session_id=resolved_session_id,
                        usage=usage,
                        raw=raw_event,
                    )
                yield AdapterEvent(
                    type="result",
                    text=final_text,
                    session_id=resolved_session_id,
                    # The Codex JSONL reports token counts, not a dollar
                    # figure, so there is no cost to report here.
                    cost_usd=None,
                    is_error=False,
                    raw=raw_event,
                )
                continue

            if event_type in ("turn.failed", "error"):
                yield AdapterEvent(
                    type="error",
                    text=_error_text(raw_event),
                    session_id=resolved_session_id,
                    is_error=True,
                    raw=raw_event,
                )

        stderr_bytes = await stderr_task if stderr_task is not None else b""
        return_code = await self._process.wait()
        if return_code != 0:
            yield AdapterEvent(
                type="error",
                text=stderr_bytes.decode("utf-8", errors="replace"),
                session_id=resolved_session_id,
                is_error=True,
            )

    async def terminate(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()


_MISSING_ROLLOUT = "no rollout found"


def _is_missing_rollout(text: str) -> bool:
    return _MISSING_ROLLOUT in (text or "").lower()


def _error_text(raw_event: dict) -> str:
    error = raw_event.get("error")
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, ensure_ascii=False)
    if isinstance(error, str):
        return error
    return raw_event.get("message") or json.dumps(raw_event, ensure_ascii=False)
