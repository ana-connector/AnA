"""Per-session CLI configuration that wires the PreToolUse hook into both
CLIs.

Each CLI reads hooks from a different place — `claude` from a settings file
passed with `--settings`, `codex` from `hooks.json` inside `$CODEX_HOME` —
but both accept the same hook entry shape and the same response contract, so
one hook script backs both (see docs/plans/2026-09-22-governance-gate.md).
"""
import json
import os
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parent / "hook.py"


def _hook_entry(python_executable: str) -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_executable} {HOOK_SCRIPT}",
                        }
                    ],
                }
            ],
            "SubagentStart": [{"hooks": [{"type": "command", "command": f"{python_executable} {HOOK_SCRIPT}"}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": f"{python_executable} {HOOK_SCRIPT}"}]}],
        }
    }


def write_claude_settings(directory: os.PathLike | str, python_executable: str = "python3") -> Path:
    """Writes the `--settings` file for one session and returns its path."""
    path = Path(directory) / "claude_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_hook_entry(python_executable), indent=2), encoding="utf-8")
    return path


def prepare_codex_home(
    directory: os.PathLike | str,
    source_home: os.PathLike | str,
    python_executable: str = "python3",
) -> Path:
    """Builds a CODEX_HOME for ANA that carries the hook.

    `hooks.json` must live inside CODEX_HOME, and writing it into the user's
    real ~/.codex would modify their own configuration — which is also a
    host-mounted directory in the container. So ANA keeps its own CODEX_HOME
    and symlinks the credentials in; sessions written there belong to ANA,
    which is what `codex exec resume` needs anyway."""
    home = Path(directory)
    home.mkdir(parents=True, exist_ok=True)

    source = Path(source_home)
    for name in ("auth.json", "config.toml"):
        link = home / name
        target = source / name
        if link.is_symlink() or link.exists():
            continue
        if target.exists():
            link.symlink_to(target)

    (home / "hooks.json").write_text(
        json.dumps(_hook_entry(python_executable), indent=2), encoding="utf-8"
    )
    return home
