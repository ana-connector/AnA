import json
import asyncio
import sys
from pathlib import Path

from packages.adapters.codex import CodexAdapter

FAKE_CLI = str(Path(__file__).parent / "fixtures" / "fake_codex.py")


async def _collect(stream):
    return [event async for event in stream]


def _adapter(tmp_path, **kwargs) -> CodexAdapter:
    adapter = CodexAdapter(
        workspace_dir=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        executable=sys.executable,
        **kwargs,
    )
    adapter._extra_args_prefix = [FAKE_CLI]
    return adapter


def test_build_args_fresh_session_bypasses_sandbox(tmp_path):
    adapter = _adapter(tmp_path)
    args = adapter._build_args("hello", None)

    assert "exec" in args
    assert "resume" not in args
    assert "--json" in args
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert args[args.index("-C") + 1] == str(tmp_path)
    assert args[-1] == "hello"


def test_build_args_resume_places_session_id_after_subcommand(tmp_path):
    adapter = _adapter(tmp_path)
    args = adapter._build_args("hello", "thread-123")

    exec_index = args.index("exec")
    assert args[exec_index + 1] == "resume"
    assert args[exec_index + 2] == "thread-123"
    assert args[-1] == "hello"


def test_build_args_resume_omits_flags_the_subcommand_rejects(tmp_path):
    # `codex exec resume` accepts neither -C nor -s and exits non-zero with
    # "unexpected argument '-C' found" (verified against the real CLI).
    adapter = _adapter(tmp_path)
    args = adapter._build_args("hello", "thread-123")

    assert "-C" not in args
    assert "-s" not in args


def test_build_args_resume_passes_sandbox_as_config_override(tmp_path):
    adapter = _adapter(tmp_path, sandbox="read-only")
    args = adapter._build_args("hello", "thread-123")

    assert "-s" not in args
    assert args[args.index("-c") + 1] == 'sandbox_mode="read-only"'
    assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_build_args_explicit_sandbox_replaces_bypass(tmp_path):
    adapter = _adapter(tmp_path, sandbox="workspace-write")
    args = adapter._build_args("hello", None)

    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert args[args.index("-s") + 1] == "workspace-write"


def test_build_args_includes_model_when_set(tmp_path):
    adapter = _adapter(tmp_path, model="gpt-5-codex")
    args = adapter._build_args("hello", None)

    assert args[args.index("--model") + 1] == "gpt-5-codex"


async def test_run_stream_yields_text_usage_and_result(tmp_path):
    adapter = _adapter(tmp_path)
    events = await _collect(adapter.run_stream("ping"))

    types = [e.type for e in events]
    # The reasoning item is surfaced as its own event so the UI can show
    # thinking apart from the answer.
    assert types == ["reasoning", "text", "usage", "result"]

    _, text_event, usage_event, result_event = events
    assert text_event.text == "pong"
    # thread.started supplies the session id for every event of the turn
    assert text_event.session_id == "test-codex-thread-id"
    assert usage_event.raw["usage"]["output_tokens"] == 6
    assert result_event.text == "pong"
    assert result_event.session_id == "test-codex-thread-id"
    # The Codex JSONL reports tokens, not dollars.
    assert result_event.cost_usd is None
    assert result_event.is_error is False


async def test_run_stream_emits_updates_without_repeating_completed_item(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.env = {"PATH": "/usr/bin:/bin", "FAKE_CODEX_PARTIAL": "1"}

    events = await _collect(adapter.run_stream("ping", session_id="previous-thread"))

    assert [event.text for event in events if event.type == "text"] == ["po", "ng"]
    assert next(event for event in events if event.type == "result").text == "pong"


async def test_run_stream_resumes_with_session_id(tmp_path):
    argv_log = tmp_path / "argv.jsonl"
    adapter = _adapter(tmp_path)
    adapter.env = {"PATH": "/usr/bin:/bin", "FAKE_CODEX_ARGV_LOG": str(argv_log)}

    await _collect(adapter.run_stream("ping", session_id="prior-thread"))

    logged = json.loads(argv_log.read_text().splitlines()[0])
    assert logged[logged.index("exec") + 1 : logged.index("exec") + 3] == [
        "resume",
        "prior-thread",
    ]


async def test_terminate_stops_native_child_process(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    adapter = _adapter(tmp_path)
    adapter._extra_args_prefix = [str(wrapper), str(child_pid_file)]
    running = asyncio.create_task(_collect(adapter.run_stream("ping")))
    try:
        for _ in range(200):
            if child_pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        await adapter.terminate()
        await asyncio.wait_for(running, timeout=3)
        # A reaped process may linger briefly as a zombie, but it cannot hold
        # the Codex writer lock or continue work. Reading /proc races with the
        # kernel removing the entry, so a lookup that fails means gone, and
        # the check is given a moment rather than sampled once.
        def gone() -> bool:
            try:
                return "State:\tZ" in open(f"/proc/{child_pid}/status").read()
            except (FileNotFoundError, ProcessLookupError):
                return True

        for _ in range(100):
            if gone():
                break
            await asyncio.sleep(0.02)
        assert gone()
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_run_stream_yields_error_on_non_zero_exit(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.env = {"PATH": "/usr/bin:/bin", "FAKE_CODEX_FAIL": "1"}

    events = await _collect(adapter.run_stream("ping"))

    assert [e.type for e in events] == ["error"]
    assert events[0].is_error is True
    assert "codex exploded" in events[0].text


async def test_terminate_is_safe_before_any_turn(tmp_path):
    adapter = _adapter(tmp_path)
    await adapter.terminate()  # must not raise: no subprocess started yet


async def test_reasoning_items_are_surfaced_separately(tmp_path, monkeypatch):
    """The fake CLI emits a reasoning item before its answer. It is its own
    event type, not assistant text: the UI shows thinking apart from the
    reply."""
    adapter = _adapter(tmp_path)
    events = await _collect(adapter.run_stream("ping"))

    assert [e.type for e in events] == ["reasoning", "text", "usage", "result"]
    assert events[0].text == "hmm"


async def test_a_missing_rollout_retries_as_a_fresh_session(tmp_path):
    """The CLI refuses a resume whose rollout file it no longer has ("no
    rollout found for thread id …"). ANA recorded that session id, so losing
    the file must not end the conversation."""
    argv_log = tmp_path / "argv.jsonl"
    adapter = _adapter(tmp_path)
    adapter.env = {
        "PATH": "/usr/bin:/bin",
        "FAKE_CODEX_ARGV_LOG": str(argv_log),
        "FAKE_CODEX_MISSING_ROLLOUT": "1",
    }

    events = await _collect(adapter.run_stream("ping", session_id="gone-thread"))

    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    # First the resume, then a fresh run — and the caller sees a normal turn.
    assert "resume" in calls[0] and "resume" not in calls[1]
    assert [e.type for e in events] == ["reasoning", "text", "usage", "result"]
    assert not any(e.type == "error" for e in events)


def test_routine_stderr_chatter_is_not_an_error():
    """`codex exec` prints "Reading additional input from stdin..." whenever
    its stdin is not a terminal, plus temp-dir and rollout warnings. Reporting
    one as the turn's failure is how a provider 400 reached the user as a
    complaint about stdin."""
    from packages.adapters.codex import _meaningful_stderr

    noisy = (
        "Reading additional input from stdin...\n"
        "WARNING: failed to clean up stale arg0 temp dirs: Permission denied\n"
        "ERROR codex_rollout::list: state db returned stale rollout path for thread x\n"
    )
    assert _meaningful_stderr(noisy) == ""
    assert _meaningful_stderr(noisy + "real failure here") == "real failure here"


async def test_a_failure_the_json_stream_explained_is_not_overwritten(tmp_path):
    adapter = _adapter(tmp_path)
    # The child's environment is built by the adapter, not inherited.
    adapter.env = {
        "PATH": "/usr/bin:/bin",
        "FAKE_CODEX_TURN_FAILED": "unsupported reasoning effort",
        "FAKE_CODEX_EXIT": "1",
        "FAKE_CODEX_STDERR": "Reading additional input from stdin...",
    }

    events = [event async for event in adapter.run_stream("hi")]

    errors = [event.text for event in events if event.type == "error"]
    assert errors == ["unsupported reasoning effort"]
