"""Permission scopes for sub-agents.

Roadmap stage 3 asks for a mechanism by which "대장 Agent가 sub-agent에 줄
권한(파일 접근 범위, 실행 가능 명령어 등)을 설정할 수 있다", and stage 5's STRIDE
row for Elevation of Privilege says a sub-agent must not leave the scope its
lead gave it. This is that mechanism, and like every other permission decision
in ANA it is enforced at the gate rather than by asking the CLI nicely.

A scope is two things:

- **mode** — `write` (the default: whatever the rule set allows) or `read`,
  which refuses the tools that change something. A survey task does not need
  to be able to edit files, and a lead that knows this can say so.
- **paths** — workspace-relative prefixes the sub-agent may touch. Empty means
  the whole session workspace, which the workspace-escape rule already bounds.

The scope travels to the sub-agent's CLI as environment variables, comes back
in the hook payload, and is checked here. It narrows what the rule set allows
and never widens it: a scope cannot grant a sub-agent something the rules deny
its lead.
"""
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from packages.governance.workspace import IGNORED_PREFIXES, extract_paths, is_within

READ = "read"
WRITE = "write"
MODES = (READ, WRITE)

MODE_ENV = "ANA_GATE_SCOPE_MODE"
PATHS_ENV = "ANA_GATE_SCOPE_PATHS"

# Tools that change something, by the name the hook reports. Both CLIs' own
# names plus the MCP tools ANA grants — a read-only sub-agent that can still
# run `mcp__git__git_commit` is not read-only.
MUTATING_TOOLS = {
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "killshell",
    "applypatch",
    "apply_patch",
}

# Shell tools are judged by their command, not refused outright: the first
# read-only worker ANA ran could not answer "how many files are here", because
# listing a directory is a shell command. A survey task needs `ls`, `grep` and
# `git log`; it does not need `rm`.
SHELL_TOOLS = {"bash", "shell", "run_shell_command", "local_shell"}

# Commands whose whole job is to look. Anything not on this list is treated as
# a write under a read-only scope — an allowlist, because the set of ways to
# change a file is not enumerable.
READ_ONLY_COMMANDS = {
    "awk", "basename", "cat", "cksum", "column", "comm", "cut", "date", "df",
    "diff", "dirname", "du", "echo", "env", "file", "find", "grep", "head",
    "hostname", "id", "jq", "less", "ls", "md5sum", "nl", "od", "pwd", "readlink",
    "realpath", "rg", "sha1sum", "sha256sum", "sort", "stat", "tail", "tr",
    "tree", "uniq", "wc", "which", "whoami", "xxd", "yq",
}
# `git` is read-only only in some moods.
READ_ONLY_GIT_SUBCOMMANDS = {
    "blame", "branch", "config", "describe", "diff", "grep", "log", "ls-files",
    "ls-tree", "rev-parse", "shortlog", "show", "show-ref", "status", "tag",
}
# Splits a command line into the segments a shell would run separately.
_SEGMENT_RE = re.compile(r"(?:\|\||&&|\||;|\n)")
# Any redirection writes, whatever the command in front of it was.
_REDIRECT_RE = re.compile(r"(?<![0-9<>])>{1,2}(?!&)")
# sed/perl/python editing a file in place.
_IN_PLACE_RE = re.compile(r"(^|\s)(-i\b|--in-place)")
MUTATING_MCP_TOOLS = {
    "git_add",
    "git_commit",
    "git_reset",
    "git_checkout",
    "git_create_branch",
    "execute_sql",
}


def _normalize_name(tool_name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", tool_name.lower())


@dataclass(frozen=True)
class Scope:
    """What one sub-agent may do, as its lead decided."""

    mode: str = WRITE
    # Workspace-relative prefixes. Empty = the whole session workspace.
    paths: tuple[str, ...] = ()

    @property
    def unrestricted(self) -> bool:
        return self.mode == WRITE and not self.paths

    @classmethod
    def parse(cls, raw, *, max_paths: int = 8) -> "Scope":
        """Builds a scope from the lead's plan JSON.

        Client-of-the-model data throughout: an unknown mode falls back to the
        default rather than failing the turn, and a path that tries to climb
        out of the workspace (`..`, absolute, `~`) is dropped — a scope is a
        restriction, so the safe reading of a malformed one is "no extra
        permission", never "no restriction on where it may reach"."""
        if not isinstance(raw, dict):
            return cls()
        mode = raw.get("mode")
        mode = mode if mode in MODES else WRITE
        paths: list[str] = []
        for entry in raw.get("paths") or []:
            if not isinstance(entry, str):
                continue
            cleaned = entry.strip().strip("/")
            if not cleaned or cleaned.startswith("~") or ".." in cleaned.split("/"):
                continue
            paths.append(os.path.normpath(cleaned))
        return cls(mode=mode, paths=tuple(dict.fromkeys(paths))[:max_paths])

    def to_env(self) -> dict[str, str]:
        """How the scope reaches the sub-agent's CLI. Only set when it
        restricts something, so an unscoped worker's environment is unchanged
        from what it was before scopes existed."""
        if self.unrestricted:
            return {}
        env = {MODE_ENV: self.mode}
        if self.paths:
            env[PATHS_ENV] = ":".join(self.paths)
        return env

    @classmethod
    def from_payload(cls, mode: Optional[str], paths: Optional[str]) -> "Scope":
        """The scope as the hook reported it back to the gate."""
        return cls(
            mode=mode if mode in MODES else WRITE,
            paths=tuple(part for part in (paths or "").split(":") if part),
        )

    def as_dict(self) -> dict:
        return {"mode": self.mode, "paths": list(self.paths)}


def instructions(scope: Scope) -> str:
    """What the sub-agent is told about its own scope.

    Telling it up front is not a substitute for the gate — the gate is what
    enforces this — but an agent that knows it is read-only reports what needs
    changing instead of discovering the limit one denial at a time."""
    if scope.unrestricted:
        return ""
    lines = ["[PERMISSION SCOPE] The lead limited this task as follows."]
    if scope.mode == READ:
        lines.append(
            "- Read-only. Editing, creating or deleting files, committing and"
            " installing are blocked. Reading is unlimited: ls, cat, head, grep,"
            " rg, find, wc, git log/status/diff and the file-reading tools all"
            " work. If something needs changing, report what and how instead."
        )
    if scope.paths:
        lines.append(
            f"- Paths you may work in: {', '.join(scope.paths)} (anything else is blocked)"
        )
    return "\n".join(lines)


def with_instructions(system_prompt: Optional[str], scope: Optional[Scope]) -> Optional[str]:
    text = instructions(scope or Scope())
    if not text:
        return system_prompt
    return f"{system_prompt}\n\n{text}" if system_prompt else text


def violation(
    tool_name: str,
    tool_input: dict,
    scope: Scope,
    workspace: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Why this call is outside the sub-agent's scope, or None if it is inside."""
    if scope.unrestricted:
        return None
    if scope.mode == READ:
        if _is_mutating(tool_name):
            return (
                f"a read-only sub-agent cannot use '{tool_name}'. "
                "Report what needs changing to the lead instead."
            )
        if _normalize_name(tool_name) in SHELL_TOOLS:
            command = tool_input.get("command")
            command = command if isinstance(command, str) else ""
            offending = _writing_segment(command)
            if offending is not None:
                return (
                    f"a read-only scope allows read commands only: '{offending}' may "
                    "change something. Use a reading command (ls, cat, grep, git log, "
                    "…) or report the change the lead has to make."
                )
    if scope.paths and workspace:
        outside = _outside_paths(tool_input, scope.paths, workspace, cwd)
        if outside:
            allowed = ", ".join(scope.paths)
            return (
                f"this sub-agent may only work in {allowed}; outside it: "
                f"{', '.join(outside[:3])}"
            )
    return None


def _writing_segment(command: str) -> Optional[str]:
    """The first part of a shell command that is not plainly a read, or None
    when every part of it only looks at things."""
    if not command.strip():
        return None
    if _REDIRECT_RE.search(command):
        return command.strip()[:60]
    for segment in _SEGMENT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        tokens = segment.split()
        # Leading VAR=value assignments belong to the command that follows.
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if name == "git":
            subcommand = next((t for t in tokens[1:] if not t.startswith("-")), "")
            if subcommand in READ_ONLY_GIT_SUBCOMMANDS:
                continue
            return segment[:60]
        if name in ("sed", "perl", "python", "python3") and _IN_PLACE_RE.search(segment):
            return segment[:60]
        if name == "sed" and not _IN_PLACE_RE.search(segment):
            continue
        if name in READ_ONLY_COMMANDS:
            continue
        return segment[:60]
    return None


def _is_mutating(tool_name: str) -> bool:
    normalized = _normalize_name(tool_name)
    if normalized in MUTATING_TOOLS:
        return True
    # mcp__git__git_commit -> git_commit; the hook may also strip punctuation.
    for name in MUTATING_MCP_TOOLS:
        if normalized.endswith(_normalize_name(name)) and normalized != _normalize_name(name):
            return True
    return normalized in {_normalize_name(name) for name in MUTATING_MCP_TOOLS}


def _outside_paths(
    tool_input: dict,
    paths: Iterable[str],
    workspace: str,
    cwd: Optional[str] = None,
) -> list[str]:
    """Paths this call names that fall outside the allowed sub-directories.

    Unlike the workspace-escape check this *does* resolve relative paths: a
    worker scoped to `docs/` says `docs/plan.md`, and "it was relative so it
    must be fine" is exactly the hole this closes.

    The scope is relative to wherever the worker is actually working, which
    since sub-agent isolation is its own git worktree inside the workspace
    (`.ana/worktrees/<task>/`). Resolving only against the workspace root
    denied `<workspace>/.ana/worktrees/th-t1/docs/a.md` — the worker's own
    copy of the very directory it was granted."""
    bases = [os.path.normpath(workspace)]
    if cwd:
        resolved_cwd = os.path.normpath(os.path.expanduser(cwd))
        if resolved_cwd not in bases and is_within(resolved_cwd, workspace):
            bases.append(resolved_cwd)

    allowed = [
        os.path.normpath(os.path.join(base, path)) for base in bases for path in paths
    ]
    outside: list[str] = []
    for raw in extract_paths(tool_input):
        candidate = os.path.expanduser(raw)
        if candidate.startswith(IGNORED_PREFIXES):
            continue
        if os.path.isabs(candidate):
            candidates = [os.path.normpath(candidate)]
        else:
            candidates = [
                os.path.normpath(os.path.join(base, candidate)) for base in bases
            ]
        if not any(
            is_within(option, root) for option in candidates for root in allowed
        ):
            outside.append(candidates[0])
    return outside
