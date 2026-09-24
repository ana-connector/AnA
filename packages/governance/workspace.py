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
import shlex
from pathlib import Path
from typing import Iterable, Optional

# Patch contents are data. Only file headers name paths the patch will touch.
_PATCH_BLOCK_RE = re.compile(r"(?ms)^\*\*\* Begin Patch\s*\n.*?^\*\*\* End Patch[^\n]*")
_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")

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

# Temporary output is routine for local probes and builds. This is an
# exception to session workspace approval, not to a worker's narrower scope.
_TEMP_PREFIXES = ("/tmp", "/var/tmp")

# Public name for the list above: the sub-agent scope check needs the same
# exemptions, and reaching into a private name across modules is how two
# copies of a list start to drift.
IGNORED_PREFIXES = _IGNORED_PREFIXES


def is_ignored_system_path(path: str) -> bool:
    """Match an exempt path itself or a child, never an unrelated sibling."""
    return any(path == prefix or path.startswith(prefix + os.sep) for prefix in IGNORED_PREFIXES)


def _without_heredoc_bodies(command: str) -> str:
    """Keep shell commands while skipping here-document data lines."""
    lines = command.splitlines(keepends=True)
    kept: list[str] = []
    delimiter: str | None = None
    for line in lines:
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        kept.append(line)
        marker = _HEREDOC_RE.search(line)
        if marker:
            delimiter = marker.group(2)
    return "".join(kept)

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
    for field in ("command", "patch"):
        value = tool_input.get(field)
        if not isinstance(value, str):
            continue
        for block in _PATCH_BLOCK_RE.findall(value):
            paths.extend(_PATCH_PATH_RE.findall(block))
        if field == "patch":
            continue
        command = _without_heredoc_bodies(_PATCH_BLOCK_RE.sub("", value))
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            # An unfinished shell quote cannot justify guessing that text in
            # the unfinished argument is a filesystem path.
            continue
        for token in tokens:
            # Accept path operands and option assignments, not arbitrary slash
            # fragments in URLs, regexes, markup, or quoted output strings.
            candidate = token.split("=", 1)[-1] if "=" in token else token
            if candidate.startswith("@/"):
                candidate = candidate[1:]
            if candidate.startswith(("/", "~/")) and not candidate.startswith("//"):
                paths.append(candidate)
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
    allowed_prefixes = _TEMP_PREFIXES + tuple(extra_allowed)
    escaping = []
    for raw in extract_paths(tool_input):
        candidate = _normalize(raw)
        if not os.path.isabs(candidate):
            continue  # relative paths resolve inside the working directory
        if is_ignored_system_path(candidate) or any(
            candidate == prefix or candidate.startswith(prefix + os.sep)
            for prefix in allowed_prefixes
        ):
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
