"""Checking that a sub-agent's work actually holds.

A worker reporting "done" is a claim, not a result. Until now ANA took the
claim: the lead synthesized whatever the workers said, so "I fixed the test"
and "the test passes" were the same sentence to the system.

A task may now carry a `verify` command. ANA runs it itself after the worker
finishes — in the worker's own worktree, as a plain subprocess, not through an
agent — and the outcome travels with the task: a failed check re-dispatches the
task once with the failure output appended, and a task that still fails is
reported as failed however confidently the worker wrote its summary.

The command is screened by the same YAML rule set that screens the agents
(config/governance.yaml). The plan comes from a model, so `verify` is
model-supplied text heading for a shell — "run whatever the plan says" would
be a hole straight through the gate. Only a command the rules *allow* runs; an
`ask` verdict is not a prompt anyone can answer here, so it is refused too.
"""
import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from typing import Optional

from packages.governance.rules import ALLOW, load_ruleset

DEFAULT_TIMEOUT_SECONDS = 300.0
# Enough of the tail to see a failing assertion; a full test log would swamp
# the prompt it is pasted into.
MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class VerificationResult:
    ran: bool
    passed: bool
    command: str = ""
    output: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "passed": self.passed,
            "command": self.command,
            "output": self.output,
            "reason": self.reason,
        }

    @property
    def failed(self) -> bool:
        return self.ran and not self.passed


def timeout_seconds() -> float:
    try:
        return float(os.environ.get("ANA_VERIFY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def max_retries() -> int:
    """How many times a failed check may send the task back to a worker.
    One by default: a second failure usually needs a different plan, not
    another attempt."""
    try:
        return max(0, int(os.environ.get("ANA_MAX_TASK_RETRIES", 1)))
    except ValueError:
        return 1


async def verify(
    command: str, cwd: str, *, env: Optional[dict] = None
) -> VerificationResult:
    """Runs one verification command and says whether it passed."""
    command = (command or "").strip()
    if not command:
        return VerificationResult(ran=False, passed=False, reason="검증 명령 없음")

    verdict = load_ruleset().evaluate("Bash", {"command": command})
    if verdict.action != ALLOW:
        # Screened by the same rules as any agent command: the plan is
        # model-written text, and this one goes straight to a shell.
        return VerificationResult(
            ran=False,
            passed=False,
            command=command,
            reason=f"검증 명령이 거버넌스 규칙에 막힘 ({verdict.rule_id or verdict.action})",
        )

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})},
            # Its own process group, so the timeout below can kill the whole
            # tree. A test runner that spawns children of its own would
            # otherwise survive the kill and hold the pipe open.
            start_new_session=True,
        )
    except OSError as exc:
        return VerificationResult(
            ran=False, passed=False, command=command, reason=f"실행 실패: {exc}"
        )

    # Read on a separate task and wait on the *process*, not on communicate():
    # cancelling communicate() does not return until its pipe readers finish,
    # which for a wedged command means not until the command exits — so the
    # timeout waited exactly as long as the thing it was supposed to cut off.
    reader = asyncio.create_task(process.stdout.read())
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds())
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(process)
        await process.wait()

    try:
        output = await asyncio.wait_for(reader, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        reader.cancel()
        output = b""

    if timed_out:
        return VerificationResult(
            ran=True,
            passed=False,
            command=command,
            output=_tail(output.decode("utf-8", errors="replace").strip()),
            reason=f"{timeout_seconds():.0f}초 안에 끝나지 않음",
        )

    text = output.decode("utf-8", errors="replace").strip()
    return VerificationResult(
        ran=True,
        passed=process.returncode == 0,
        command=command,
        output=_tail(text),
        reason="" if process.returncode == 0 else f"종료 코드 {process.returncode}",
    )


def _kill_tree(process) -> None:
    """Kills the command and anything it started."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def retry_prompt(prompt: str, result: VerificationResult) -> str:
    """The task, sent back with the evidence that it is not done.

    The failure output comes *after* the original instruction and is labeled
    as a check ANA ran, so the worker treats it as a fact about the tree
    rather than as a new opinion to argue with."""
    return (
        f"{prompt}\n\n"
        f"[CHECK FAILED] After your last attempt ANA ran this command itself, and "
        f"it failed:\n"
        f"$ {result.command}\n"
        f"{result.reason}\n"
        f"{result.output or '(no output)'}\n\n"
        "Fix this failure. The task is done when that same command passes. "
        "If you cannot fix it, say what is blocking it and why."
    )


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "…\n" + text[-MAX_OUTPUT_CHARS:]
