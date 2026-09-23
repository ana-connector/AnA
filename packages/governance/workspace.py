"""Per-session workspaces and path confinement.

Until now every conversation shared one fixed directory, so two threads could
overwrite each other's work and a stray `rm` in one affected all of them. Each
thread now gets its own directory under a root, and the gate refuses tool
calls that reach outside the directory of the thread making them.

Path confinement is enforced at the gate rather than by the CLIs: both run
with their own sandboxes off inside the container (codex cannot sandbox there
at all), so the gate is where "outside your workspace" has to be decided.
"""
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Optional

# Absolute paths, and ~-relative ones, as they appear inside a shell command.
_PATH_RE = re.compile(r"(?<![\w=])(~?/[^\s'\";|&><)]+)")

# Read-only system paths a normal command touches constantly. Flagging these
# would make the confinement check fire on `ls /usr/bin` and train the user to
# approve everything, which is worse than not checking.
_IGNORED_PREFIXES = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/opt",
    "/proc",
    "/sys",
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/etc/ssl",
)

# Public name for the list above: the sub-agent scope check needs the same
# exemptions, and reaching into a private name across modules is how two
# copies of a list start to drift.
IGNORED_PREFIXES = _IGNORED_PREFIXES

# Field names used by file-oriented tools in both CLIs.
_PATH_FIELDS = ("file_path", "path", "notebook_path", "target_file", "directory")


def session_workspace(root: os.PathLike | str, thread_id: str, create: bool = True) -> Path:
    """The directory belonging to one conversation.

    The thread id is sanitized rather than trusted: it arrives as a query
    parameter, and a value like "../../etc" would otherwise pick the
    directory this function exists to confine."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id)[:64] or "default"
    path = Path(root) / safe
    # Belt and braces: the substitution above already removes separators, so
    # the result is a single segment — but the check is what actually
    # guarantees containment, and it costs nothing.
    if not is_within(str(path), root):
        path = Path(root) / hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def extract_paths(tool_input: dict) -> list[str]:
    """Filesystem paths a tool call refers to, best-effort.

    Best-effort is the honest description: a shell command can construct a
    path at runtime, so this catches the literal ones. It is a second line
    behind the rule set, not a proof of containment."""
    paths: list[str] = []
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            paths.append(value)
    command = tool_input.get("command")
    if isinstance(command, str):
        paths.extend(_PATH_RE.findall(command))
    return paths


def _normalize(path: str) -> str:
    expanded = os.path.expanduser(path)
    return os.path.normpath(expanded)


def is_within(path: str, root: os.PathLike | str) -> bool:
    """Whether `path` stays inside `root` once `..` and symlink-free
    normalization are applied."""
    root_resolved = os.path.normpath(str(root))
    candidate = _normalize(path)
    if not os.path.isabs(candidate):
        candidate = os.path.normpath(os.path.join(root_resolved, candidate))
    return candidate == root_resolved or candidate.startswith(root_resolved + os.sep)


def escaping_paths(
    tool_input: dict, workspace: os.PathLike | str, extra_allowed: Iterable[str] = ()
) -> list[str]:
    """Paths in this call that leave the session workspace.

    Read-only system locations are ignored (see _IGNORED_PREFIXES): a command
    that reads /usr/bin is not escaping anything, and flagging it would train
    the user to approve without reading."""
    allowed_prefixes = tuple(_IGNORED_PREFIXES) + tuple(extra_allowed)
    escaping = []
    for raw in extract_paths(tool_input):
        candidate = _normalize(raw)
        if not os.path.isabs(candidate):
            continue  # relative paths resolve inside the working directory
        if candidate.startswith(allowed_prefixes):
            continue
        if not is_within(candidate, workspace):
            escaping.append(candidate)
    return escaping


def workspace_root(default: Optional[str] = None) -> str:
    """Root that holds every session's directory. Falls back to the old fixed
    workspace so an existing deployment keeps working."""
    return (
        os.environ.get("ANA_WORKSPACE_ROOT")
        or default
        or os.environ.get("ANA_WORKSPACE_DIR")
        or "/home/hello/workspace"
    )
