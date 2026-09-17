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
        return env


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
