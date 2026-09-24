"""Per-session CLI configuration that wires the PreToolUse hook into both
CLIs.

Each CLI reads hooks from a different place — `claude` from a settings file
passed with `--settings`, `codex` from `hooks.json` inside `$CODEX_HOME`,
`kimi` from a `[[hooks]]` array in `config.toml` inside `$KIMI_CODE_HOME` —
but all three accept the same `hookSpecificOutput` response contract, so one
hook script backs all of them (see docs/plans/2026-09-22-governance-gate.md
and docs/plans/2026-09-23-kimi-adapter.md).
"""
import json
import os
import re
from pathlib import Path

from packages.governance import atomic

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
    return atomic.write_text(
        Path(directory) / "claude_settings.json",
        json.dumps(_hook_entry(python_executable), indent=2),
    )


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

    atomic.write_text(
        home / "hooks.json", json.dumps(_hook_entry(python_executable), indent=2)
    )
    return home


# Kimi keeps hooks in its config file rather than a separate one, so ANA's
# copy of that file carries them. Blockable events only: everything else in
# its event list is observational, and a hook that runs on every one of them
# is latency for nothing.
KIMI_HOOKS_TEMPLATE = """
[[hooks]]
event = "PreToolUse"
command = "{command}"
timeout = 600

[[hooks]]
event = "SubagentStart"
command = "{command}"
timeout = 30

[[hooks]]
event = "SubagentStop"
command = "{command}"
timeout = 30
"""

# Files in the user's own KIMI_CODE_HOME that ANA links rather than copies:
# credentials the CLI refreshes in place, and the identifiers/trust records
# that make a run look like the same installation rather than a new one.
KIMI_LINKED_ENTRIES = (
    "credentials",
    "device_id",
    "region",
    "workspace-trust",
    "workspaces.json",
)


def prepare_kimi_home(
    directory: os.PathLike | str,
    source_home: os.PathLike | str,
    python_executable: str = "python3",
    effort: str | None = None,
) -> Path:
    """Builds a KIMI_CODE_HOME for ANA that carries the gate hook.

    `[[hooks]]` lives in `config.toml`, which also holds the provider
    definitions — so the file is *copied* from the user's home (with the hooks
    appended) rather than symlinked, while `credentials/` is linked so an
    OAuth refresh is shared instead of diverging into two half-valid copies.

    `effort` is written into `[thinking]`, because Kimi has no per-run effort
    flag. That is also why the caller gives each effort level its own home:
    two concurrent sub-agents on different efforts would otherwise rewrite one
    file under each other."""
    home = Path(directory)
    home.mkdir(parents=True, exist_ok=True)

    source = Path(source_home)
    for name in KIMI_LINKED_ENTRIES:
        link = home / name
        target = source / name
        if link.is_symlink() or link.exists():
            continue
        if target.exists():
            link.symlink_to(target)

    config = source / "config.toml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    text = _without_ana_hooks(text)
    if effort:
        text = _with_thinking_effort(text, effort)
    text += KIMI_HOOKS_TEMPLATE.format(command=f"{python_executable} {HOOK_SCRIPT}")
    atomic.write_text(home / "config.toml", text)
    return home


_HOOKS_BLOCK_RE = re.compile(r"\n*\[\[hooks\]\][\s\S]*?(?=\n\[|\Z)")
_THINKING_EFFORT_RE = re.compile(r"(?m)^(\[thinking\][\s\S]*?^effort\s*=\s*)\"[^\"]*\"")


def _without_ana_hooks(text: str) -> str:
    """Drops any `[[hooks]]` already in the copied config.

    The source is the user's own file; if ANA has written its home once and
    the user then copied it back, or the user wrote hooks of their own, the
    entries would otherwise accumulate on every session — and ANA's own hook
    would run several times per tool call."""
    return _HOOKS_BLOCK_RE.sub("", text).rstrip() + "\n"


def _with_thinking_effort(text: str, effort: str) -> str:
    if _THINKING_EFFORT_RE.search(text):
        return _THINKING_EFFORT_RE.sub(rf'\g<1>"{effort}"', text, count=1)
    return text.rstrip() + f'\n\n[thinking]\nenabled = true\neffort = "{effort}"\n'
