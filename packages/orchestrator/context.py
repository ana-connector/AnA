"""The context ANA hands every worker of a turn, and the workspace's memory of it.

Workers used to start cold. Five sub-agents given five slices of the same job
each opened the same files to find out what they were looking at, so the same
`routing.yaml` was read five times and paid for five times — and a worker that
read it badly guessed at an API the lead already knew.

The lead now names what the workers need *once*, in the plan's `context`, and
ANA reads those files itself and puts the excerpts in front of every worker's
instruction. The lead is the only agent that has seen the conversation, so it is
the only one that can say which files matter; reading them is plumbing, and
plumbing should happen once.

What survives the turn lives in the workspace, under `.ana/context.md`: notes
the lead wrote about *this* project rather than about one request. A later turn
— and a delegated worker, which has no plan and so no `context` of its own —
is handed that file the same way. It is per workspace on purpose: the notes
describe a repository, and two conversations open on the same one are looking at
the same repository.

Deliberate limits:

- **Excerpts are capped**, per file and in total. The point is to save the
  worker a read, not to paste a repository into a prompt; a file too big to
  excerpt is worth reading properly, and the block says so.
- **Paths stay inside the workspace.** They arrive from a model, and `context`
  is not a way around the gate's confinement.
- **Memory is bounded and newest-first-out is not an option**: the oldest
  entries are dropped, because the newest note is the one that describes the
  repository as it is now.
"""
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from packages.governance.workspace import is_within

# Where a workspace keeps what ANA remembers about it. Same directory the
# worktrees live in, which git's exclude file already hides from the user's
# `git status` (packages/orchestrator/worktrees.py).
CONTEXT_DIRNAME = ".ana"
MEMORY_FILENAME = "context.md"

# How much of a workspace may reach a worker prompt. A worker's own read is
# cheaper than a prompt nobody can fit: these are the point where excerpting
# stops paying for itself.
MAX_FILES = 12
MAX_FILE_CHARS = 6000
MAX_TOTAL_CHARS = 24000
MAX_NOTE_CHARS = 4000
# What the workspace keeps between turns, oldest entries dropped first.
MAX_MEMORY_CHARS = 8000

_ENTRY_RE = re.compile(r"(?m)^## ")


@dataclass(frozen=True)
class SharedContext:
    """What the lead says every worker of this turn needs to know."""

    files: tuple[str, ...] = ()
    notes: str = ""
    # Whether the notes describe the project rather than this one request, and
    # so belong in the workspace's memory. The lead asks for that explicitly:
    # ANA remembering every turn's working notes would fill the file with
    # things that were true for one request.
    remember: bool = False

    @property
    def empty(self) -> bool:
        return not self.files and not self.notes


def parse(raw) -> SharedContext:
    """A plan's `context`, as far as it can be trusted.

    Client-of-the-model data throughout: a path list that is a string, a note
    that is a dict, a file count that would paste the repository. Each is
    dropped or capped rather than failing the plan — a malformed context is a
    missing convenience, not a broken turn."""
    if not isinstance(raw, dict):
        return SharedContext()
    files: list[str] = []
    entries = raw.get("files")
    if isinstance(entries, str):  # one path, unwrapped
        entries = [entries]
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            path = entry.strip()
            if path not in files:
                files.append(path)
            if len(files) >= MAX_FILES:
                break
    notes = raw.get("notes")
    if not isinstance(notes, str):
        notes = ""
    return SharedContext(
        files=tuple(files),
        notes=notes.strip()[:MAX_NOTE_CHARS],
        remember=bool(raw.get("remember")),
    )


def _readable(workspace: str, path: str) -> Optional[str]:
    """The absolute path of a context file, or None when it is not one.

    Absolute paths and `..` are refused rather than resolved: `context` is the
    lead naming files in the workspace, and anything else is a path the gate
    would have screened if a worker had asked for it."""
    if os.path.isabs(path) or path.startswith("~"):
        return None
    candidate = os.path.normpath(os.path.join(workspace, path))
    if not is_within(candidate, workspace):
        return None
    return candidate if os.path.isfile(candidate) else None


def _excerpt(absolute: str, relative: str, budget: int) -> str:
    """One file as a labelled excerpt, truncated on a line boundary.

    The header names the range and the file's real length, so a worker that
    needs the rest knows there is a rest. Silent truncation is what makes an
    agent confident about code it has only seen the first half of."""
    try:
        text = Path(absolute).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"--- {relative} (읽지 못함: {exc}) ---"
    total = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    limit = min(MAX_FILE_CHARS, budget)
    if len(text) <= limit:
        return f"--- {relative} (lines 1-{total} of {total}) ---\n{text.rstrip()}"
    head = text[:limit]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    shown = head.count("\n") + 1
    return (
        f"--- {relative} (lines 1-{shown} of {total}, truncated — read the file"
        f" itself if you need the rest) ---\n{head.rstrip()}"
    )


CONTEXT_HEADER = """\
[SHARED CONTEXT] The lead already read these for you. Work from them instead of
opening the same files again; open a file yourself only when you need what is
not here.
"""

MEMORY_HEADER = """\
[PROJECT NOTES] What ANA knows about this workspace from earlier turns.
"""


def render(
    context: SharedContext | None,
    workspace: Optional[str],
    *,
    remembered: str = "",
) -> str:
    """The block that goes in front of every worker instruction, or "".

    Empty is a normal answer: most turns name no context, and an empty header
    reads to a worker as context it failed to receive."""
    sections: list[str] = []
    if remembered.strip():
        sections.append(MEMORY_HEADER + "\n" + remembered.strip())
    if context is None or context.empty:
        return "\n\n".join(sections)
    body: list[str] = []
    if context.notes:
        body.append(context.notes)
    budget = MAX_TOTAL_CHARS
    missing: list[str] = []
    if workspace:
        for relative in context.files:
            if budget <= 0:
                missing.append(relative)
                continue
            absolute = _readable(workspace, relative)
            if absolute is None:
                missing.append(relative)
                continue
            excerpt = _excerpt(absolute, relative, budget)
            budget -= len(excerpt)
            body.append(excerpt)
    else:
        missing.extend(context.files)
    if missing:
        # Named rather than dropped: a worker told nothing about `config/x.yaml`
        # assumes it was not important, and the lead thought it was.
        body.append(
            "--- not included (read these yourself if you need them): "
            + ", ".join(missing)
            + " ---"
        )
    if not body:
        return "\n\n".join(sections)
    sections.append(CONTEXT_HEADER + "\n" + "\n\n".join(body))
    return "\n\n".join(sections)


def attach(prompt: str, block: str) -> str:
    """The worker's instruction with the context in front of it."""
    return f"{block}\n\n[INSTRUCTION]\n{prompt}" if block.strip() else prompt


def memory_path(workspace: str) -> Path:
    return Path(workspace) / CONTEXT_DIRNAME / MEMORY_FILENAME


def recall(workspace: Optional[str]) -> str:
    """What earlier turns wrote about this workspace, or ""."""
    if not workspace:
        return ""
    try:
        return memory_path(workspace).read_text(encoding="utf-8").strip()
    except OSError:
        return ""  # no notes yet is the normal state, not an error


def _trim(text: str) -> str:
    """Drops whole oldest entries until the file fits.

    Whole entries, never a prefix: half a note is worse than no note, because
    it reads as a complete statement about the project."""
    if len(text) <= MAX_MEMORY_CHARS:
        return text
    entries = [part for part in _ENTRY_RE.split(text) if part.strip()]
    kept: list[str] = []
    size = 0
    for entry in reversed(entries):
        block = "## " + entry.strip() + "\n"
        if kept and size + len(block) > MAX_MEMORY_CHARS:
            break
        kept.append(block)
        size += len(block)
    return "\n".join(reversed(kept)).strip() + "\n"


def _hide(directory: Path) -> None:
    """Keeps `.ana/` out of the user's `git status`.

    Self-contained on purpose: a `.gitignore` ignoring `*` inside the directory
    covers the directory and itself, so ANA never edits a tracked file in what
    may be someone else's repository. worktrees.py writes the same exclusion to
    `.git/info/exclude`, but only once a worktree exists — and the notes can be
    written on a turn that creates none."""
    marker = directory / ".gitignore"
    if marker.exists():
        return
    try:
        marker.write_text("*\n", encoding="utf-8")
    except OSError:
        pass  # cosmetic; a workspace that cannot take the file still works


def remember(workspace: Optional[str], context: SharedContext, *, title: str = "") -> bool:
    """Writes the lead's notes into the workspace's memory. False when nothing
    was written.

    Only when the lead asked (`remember: true`) and only the notes: the file
    excerpts are the files, and a copy of them here would be stale by the next
    turn."""
    if not workspace or context is None or not context.remember or not context.notes:
        return False
    entry_title = (title or "메모").strip()[:80]
    entry = f"## {date.today().isoformat()} · {entry_title}\n{context.notes.strip()}\n"
    if context.files:
        entry += f"\n관련 파일: {', '.join(context.files)}\n"
    path = memory_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _hide(path.parent)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if context.notes.strip() in current:
            return False  # the same note twice is noise, not a second fact
        path.write_text(_trim(current.rstrip() + "\n\n" + entry if current.strip() else entry),
                        encoding="utf-8")
    except OSError:
        return False  # a read-only workspace is not a reason to fail the turn
    return True
