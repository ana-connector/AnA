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
        # The auto-approved agent subprocess (run with
        # --dangerously-skip-permissions) has no legitimate need for direct
        # database access; leaving DATABASE_URL in its environment would
        # hand it working credentials to ANA's own memory database (turns/
        # checkpoint tables), which it could drop or corrupt if it chose to
        # or was tricked into it.
        env.pop("DATABASE_URL", None)
        return env


def build_codex_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a `codex` CLI subprocess. Codex authenticates from
    ~/.codex (mounted into the container), so unlike the Claude adapter there
    are no keys to inject -- but the same DATABASE_URL strip applies: the
    auto-approved agent subprocess must not be handed working credentials to
    ANA's own memory database (see EnvBuilder.build)."""
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("DATABASE_URL", None)
    return env


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
