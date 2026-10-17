"""Per-session CLI configuration that wires the PreToolUse hook into the CLIs.

Each CLI reads hooks from a different place — `claude` from a settings file
passed with `--settings`, `codex` from `hooks.json` inside `$CODEX_HOME`,
`kimi` from a `[[hooks]]` array in `config.toml` inside `$KIMI_CODE_HOME` —
but those three accept the same `hookSpecificOutput` response contract, so one
hook script backs them (see docs/plans/2026-09-22-governance-gate.md
and docs/plans/2026-09-23-kimi-adapter.md).

Cursor Agent has no PreToolUse hook; `prepare_cursor_home` only isolates HOME
so MCP config and the host login (`auth.json`) can be shared safely. OpenCode
has none either, and `prepare_opencode_home` does the same job for it — with
the twist that OpenCode's credentials live in its data directory rather than
its config directory.
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


def write_claude_settings(
    directory: os.PathLike | str,
    python_executable: str = "python3",
    commit_attribution: str | None = None,
) -> Path:
    """Writes the `--settings` file for one session and returns its path.

    `commit_attribution` replaces the Claude CLI's own commit trailer
    ("Co-Authored-By: Claude …"), which it adds whatever the system prompt
    says; "" removes it. None leaves the CLI's default alone."""
    settings = _hook_entry(python_executable)
    if commit_attribution is not None:
        settings["attribution"] = {"commit": commit_attribution}
    return atomic.write_text(
        Path(directory) / "claude_settings.json",
        json.dumps(settings, indent=2),
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


def prepare_cursor_home(
    directory: os.PathLike | str,
    source_config_dir: os.PathLike | str,
) -> Path:
    """Builds a HOME for the Cursor Agent CLI subprocess.

    Auth lives in `~/.config/cursor/auth.json` (the host login session). That
    file is symlinked so an OAuth refresh stays shared rather than splitting
    into two half-valid copies. MCP servers are written later into
    `$HOME/.cursor/mcp.json` by `cursor_mcp`; chats land under the prepared
    home so they do not collide with the user's interactive CLI sessions.
    """
    home = Path(directory)
    config = home / ".config" / "cursor"
    cursor_dir = home / ".cursor"
    config.mkdir(parents=True, exist_ok=True)
    cursor_dir.mkdir(parents=True, exist_ok=True)

    source = Path(source_config_dir)
    auth_src = source / "auth.json"
    auth_dst = config / "auth.json"
    if auth_src.is_file() and not auth_dst.exists() and not auth_dst.is_symlink():
        auth_dst.symlink_to(auth_src)
    # Optional profile cache; missing is fine — the CLI rewrites it.
    for name in ("cli-config.json",):
        src = source / name
        dst = config / name
        if src.is_file() and not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src)
    return home


def prepare_opencode_home(
    directory: os.PathLike | str,
    source_data_dir: os.PathLike | str,
) -> Path:
    """Builds a HOME for the OpenCode CLI subprocess.

    OpenCode keeps credentials in its *data* directory
    (`~/.local/share/opencode/auth.json`), not its config directory, and keeps
    sessions in `opencode.db` next to them. Pointing HOME (and XDG_DATA_HOME)
    at a prepared directory therefore gives ANA its own session database —
    which is the point, so a turn here does not show up in the user's
    interactive `opencode` session list — while `auth.json` is symlinked so a
    token refresh stays shared with the host login.

    The user's *config* is not copied: the caller points OPENCODE_CONFIG_DIR at
    the real one (providers, keys, plugins and their node_modules all live
    there) and OPENCODE_CONFIG at the small overlay `ana.json` that
    `write_opencode_mcp_config` writes under this home, which is how ANA's MCP
    servers are added without editing a file the user owns.
    """
    home = Path(directory)
    data = home / ".local" / "share" / "opencode"
    config = home / ".config" / "opencode"
    data.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)

    source = Path(source_data_dir)
    auth_src = source / "auth.json"
    auth_dst = data / "auth.json"
    if auth_src.is_file() and not auth_dst.exists() and not auth_dst.is_symlink():
        auth_dst.symlink_to(auth_src)
    return home
