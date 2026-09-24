"""Writing a file another process may be about to execute.

Every CLI ANA runs is handed generated files: a settings file with the gate
hook, an `mcp.json`, a launcher script for each MCP server. They are written
per adapter build, and a turn builds several adapters at once — so two workers
starting together write the same paths while a third CLI is reading or
executing them. A plain `open(...).write(...)` truncates first, which gives
the reader an empty or half-written file.

Writing to a temporary file in the same directory and renaming it into place
makes the swap atomic: a reader sees either the old file or the new one.
"""
import os
from pathlib import Path
from typing import Optional


def write_text(path: os.PathLike | str, text: str, *, mode: Optional[int] = None) -> Path:
    """Atomically replaces `path` with `text`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so the rename cannot cross a filesystem boundary — and
    # unique per process, so two writers do not share the temporary either.
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
