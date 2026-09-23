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
    "bash",
    "killshell",
    "applypatch",
    "apply_patch",
    "shell",
    "run_shell_command",
    "local_shell",
}
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
    lines = ["[권한 스코프] 대장이 이 작업에 다음 제한을 걸었다."]
    if scope.mode == READ:
        lines.append(
            "- 읽기 전용이다. 파일 수정, 명령 실행, 커밋은 차단된다. 변경이 필요하면"
            " 무엇을 어떻게 바꿔야 하는지 보고만 해라."
        )
    if scope.paths:
        lines.append(f"- 작업 가능한 경로: {', '.join(scope.paths)} (그 밖은 차단된다)")
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
) -> Optional[str]:
    """Why this call is outside the sub-agent's scope, or None if it is inside."""
    if scope.unrestricted:
        return None
    if scope.mode == READ and _is_mutating(tool_name):
        return (
            f"읽기 전용 스코프의 하위 에이전트는 '{tool_name}'을(를) 쓸 수 없다. "
            "변경이 필요하면 대장에게 보고해라."
        )
    if scope.paths and workspace:
        outside = _outside_paths(tool_input, scope.paths, workspace)
        if outside:
            allowed = ", ".join(scope.paths)
            return (
                f"이 하위 에이전트에게 허용된 경로는 {allowed} 뿐이다: "
                f"{', '.join(outside[:3])}"
            )
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


def _outside_paths(tool_input: dict, paths: Iterable[str], workspace: str) -> list[str]:
    """Paths this call names that fall outside the allowed sub-directories.

    Unlike the workspace-escape check this *does* resolve relative paths: a
    worker scoped to `docs/` says `docs/plan.md`, and "it was relative so it
    must be fine" is exactly the hole this closes."""
    allowed = [os.path.normpath(os.path.join(workspace, path)) for path in paths]
    outside: list[str] = []
    for raw in extract_paths(tool_input):
        candidate = os.path.expanduser(raw)
        if candidate.startswith(IGNORED_PREFIXES):
            continue
        if not os.path.isabs(candidate):
            candidate = os.path.join(workspace, candidate)
        candidate = os.path.normpath(candidate)
        if not any(is_within(candidate, root) for root in allowed):
            outside.append(candidate)
    return outside
