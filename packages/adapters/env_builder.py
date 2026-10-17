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
            # TAPIE: the same CLI, authenticated with a key against its own
            # endpoint instead of the Pro subscription. ANTHROPIC_API_KEY_SAVED
            # is still read, because that is where the key lived before this
            # mode had a name of its own.
            api_key = env.get("TAPIE_API_KEY") or env.get("ANTHROPIC_API_KEY_SAVED")
            if not api_key:
                raise RuntimeError(
                    "TAPIE_API_KEY is not set; cannot build the TAPIE environment"
                )
            env["ANTHROPIC_API_KEY"] = api_key
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
            env["ANTHROPIC_BASE_URL"] = (
                env.get("TAPIE_BASE_URL") or "https://panel.tapie.kr/api/claude-code"
            )
        # The auto-approved agent subprocess (run with
        # --dangerously-skip-permissions) has no legitimate need for direct
        # database access; leaving DATABASE_URL in its environment would
        # hand it working credentials to ANA's own memory database (turns/
        # checkpoint tables), which it could drop or corrupt if it chose to
        # or was tricked into it.
        env.pop("DATABASE_URL", None)
        return _without_inherited_gate(_with_mcp_tool_timeout(env))


# The CLI's own deadline for one MCP tool call. ANA's `delegate.run_task`
# holds its answer until the worker is finished, which is the whole point —
# a lead that re-asks every few seconds spends a tool call, and a model
# round trip, on learning nothing. The client has to be willing to wait that
# long, and Claude Code's default is far shorter.
#
# Only set when the environment has nothing to say: an operator who has
# configured a deadline meant it.
MCP_TOOL_TIMEOUT_MS = "3900000"  # 65 minutes, past ANA_DELEGATE_WAIT_SECONDS


def _with_mcp_tool_timeout(env: dict[str, str]) -> dict[str, str]:
    env.setdefault("MCP_TOOL_TIMEOUT", MCP_TOOL_TIMEOUT_MS)
    return env


def _without_inherited_gate(env: dict[str, str]) -> dict[str, str]:
    """Drops a gate token the server happened to be holding itself.

    These are added back by `with_gate` for the conversation the subprocess is
    actually being started for. What they must never be is inherited: ANA runs
    as an agent inside its own workspace, so its environment can carry a live
    token for somebody else's conversation — and with the gate switched off
    nothing adds one, which left exactly that stale token in the CLI's
    environment. Same reasoning as the DATABASE_URL strip above."""
    env.pop("ANA_GATE_TOKEN", None)
    env.pop("ANA_GATE_URL", None)
    return env


def build_codex_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a `codex` CLI subprocess. Codex authenticates from
    ~/.codex (mounted into the container), so unlike the Claude adapter there
    are no keys to inject -- but the same DATABASE_URL strip applies: the
    auto-approved agent subprocess must not be handed working credentials to
    ANA's own memory database (see EnvBuilder.build)."""
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("DATABASE_URL", None)
    return _without_inherited_gate(_with_mcp_tool_timeout(env))


def build_kimi_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a `kimi` CLI subprocess.

    Kimi authenticates from its data directory (KIMI_CODE_HOME), not from
    environment variables — its own docs are explicit that KIMI_API_KEY and
    friends are *not* read from the shell — so there is nothing to inject
    here. The DATABASE_URL strip applies for the same reason as the other
    two: an agent subprocess has no business holding credentials to ANA's own
    memory database."""
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("DATABASE_URL", None)
    return _without_inherited_gate(_with_mcp_tool_timeout(env))


def build_cursor_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a Cursor Agent (`agent`) CLI subprocess.

    Auth is the host login session under `~/.config/cursor/auth.json`, reached
    via the prepared HOME from `prepare_cursor_home`. An optional
    `CURSOR_API_KEY` in the environment is left alone when present (CI), but
    the default path is login — no key is injected here.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("DATABASE_URL", None)
    return _without_inherited_gate(_with_mcp_tool_timeout(env))


def default_cursor_model_from_settings() -> str | None:
    """Reads CURSOR_MODEL. None means: let the agent CLI use its default."""
    return os.environ.get("CURSOR_MODEL") or None


def build_opencode_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for an `opencode` CLI subprocess.

    Auth is whatever the host login wrote into OpenCode's data directory
    (`auth.json`), reached through the prepared HOME from
    `prepare_opencode_home`; provider keys that are already in the environment
    (the config file can read them) are left alone. The DATABASE_URL strip
    applies for the same reason as the other four.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("DATABASE_URL", None)
    return _without_inherited_gate(_with_mcp_tool_timeout(env))


def default_opencode_model_from_settings() -> str | None:
    """Reads OPENCODE_MODEL (`provider/model`). None means: no --model flag,
    so the CLI uses the model its own config names."""
    return os.environ.get("OPENCODE_MODEL") or None


def default_opencode_effort_from_settings() -> str | None:
    """Reads OPENCODE_VARIANT (OPENCODE_EFFORT is accepted too, because that is
    what the other CLIs call it). None means: no --variant flag."""
    return (
        os.environ.get("OPENCODE_VARIANT") or os.environ.get("OPENCODE_EFFORT") or None
    )


def default_kimi_model_from_settings() -> str | None:
    """Reads KIMI_MODEL. None means: no --model flag, so the CLI uses
    `default_model` from its config."""
    return os.environ.get("KIMI_MODEL") or None


def default_kimi_effort_from_settings() -> str | None:
    """Reads KIMI_EFFORT. Kimi has no effort flag — the value is written into
    the prepared home's config.toml instead (see prepare_kimi_home)."""
    return os.environ.get("KIMI_EFFORT") or None


def with_gate(env: dict[str, str], gate_url: str, token: str) -> dict[str, str]:
    """Adds the gate coordinates the PreToolUse hook needs. The hook runs
    inside the CLI subprocess, so this environment is the only channel that
    tells it which conversation it is screening for."""
    env = dict(env)
    env["ANA_GATE_URL"] = gate_url
    env["ANA_GATE_TOKEN"] = token
    return env


def default_codex_model_from_settings() -> str | None:
    """Reads CODEX_MODEL from the environment. None means: let the codex CLI
    use its own default (no --model flag passed)."""
    return os.environ.get("CODEX_MODEL") or None


def default_codex_effort_from_settings() -> str | None:
    """Reads CODEX_EFFORT from the environment. None means: pass no
    model_reasoning_effort override and let the codex CLI use its own."""
    return os.environ.get("CODEX_EFFORT") or None


def default_codex_sandbox_from_settings() -> str | None:
    """Reads CODEX_SANDBOX from the environment. None means: pass
    --dangerously-bypass-approvals-and-sandbox, matching the Claude
    adapter's --dangerously-skip-permissions."""
    sandbox = os.environ.get("CODEX_SANDBOX") or None
    if sandbox and sandbox not in ("read-only", "workspace-write", "danger-full-access"):
        raise ValueError(f"unknown codex sandbox mode: {sandbox!r}")
    return sandbox


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
