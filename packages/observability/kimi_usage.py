"""Token counts for a Kimi turn.

`kimi -p --output-format stream-json` reports no usage at all — the stream is
version, messages, resume hint, and nothing else. The CLI does record counts,
in the session's own wire log:

    {"usage": {"inputOther": 1058, "output": 69,
               "inputCacheRead": 18944, "inputCacheCreation": 0},
     "usageScope": "turn", ...}

(verified against kimi 2.0.2, 2026-09-23). Read back after the turn, the same
way Codex's real rate limits are read out of its rollout log, so a Kimi turn
still counts against the thread's token budget instead of looking free.

Kimi's input fields are disjoint: `inputOther`, `inputCacheRead`, and
`inputCacheCreation` must all be included to count the full input. Unlike
Claude's usage payload, cached input is not already inside `inputOther`.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

INDEX_FILENAME = "session_index.jsonl"
WIRE_FILENAME = "wire.jsonl"
# Tail of the wire log to scan. A turn's usage record is near the end; a long
# session's log is mostly message bodies nobody needs here.
TAIL_BYTES = 512 * 1024


def default_kimi_home() -> Path:
    return Path(
        os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
    )


def session_dir(session_id: str, kimi_home: Optional[os.PathLike | str] = None) -> Optional[Path]:
    """Where the CLI put this session, according to its own index.

    The directory name encodes the working directory, so it cannot be derived
    from the session id alone — the index is the only mapping."""
    home = Path(kimi_home) if kimi_home else default_kimi_home()
    index = home / INDEX_FILENAME
    if not index.is_file():
        return None
    try:
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):  # newest entry for this id wins
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("sessionId") == session_id and record.get("sessionDir"):
            return Path(record["sessionDir"])
    return None


def read_session_tokens(
    session_id: str, kimi_home: Optional[os.PathLike | str] = None
) -> Optional[dict]:
    """The last turn's token counts, in the key names the graph already reads
    (`input_tokens` / `output_tokens`), or None when there are none to read."""
    directory = session_dir(session_id, kimi_home)
    if directory is None:
        return None
    usage = _last_usage(directory)
    if usage is None:
        return None
    input_tokens = usage.get("inputOther")
    output_tokens = usage.get("output")
    cache_read = usage.get("inputCacheRead")
    cache_creation = usage.get("inputCacheCreation")
    if not any(isinstance(value, int) for value in (
        input_tokens, output_tokens, cache_read, cache_creation
    )):
        return None
    return {
        "input_tokens": sum(
            value for value in (input_tokens, cache_read, cache_creation)
            if isinstance(value, int)
        ),
        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
        # Breakdown kept for diagnostics; graph only adds input_tokens and
        # output_tokens, so these are not added a second time.
        "cache_read_input_tokens": usage.get("inputCacheRead"),
        "cache_creation_input_tokens": usage.get("inputCacheCreation"),
    }


LOG_FILENAME = "kimi-code.log"
# A refusal the CLI never puts on stderr looks like:
#   WARN llm request failed turnStep=0.1 model=kimi-code/k3 ... errorMessage="403
#   You've reached your 5-hour usage limit. ..." statusCode=403
_LOG_ERROR_RE = re.compile(r'errorMessage="((?:[^"\\]|\\.)*)"')


def read_last_error(
    session_id: str, kimi_home: Optional[os.PathLike | str] = None
) -> Optional[str]:
    """The last request failure the CLI recorded for this session, if any.

    Kimi reports a refused request (an exhausted 5-hour window, a bad key) only
    in its own log: stdout ends without a result and stderr can be empty, so a
    turn that never ran looked exactly like a turn that answered with nothing.
    ANA reads the log back for the same reason it reads the token counts there —
    it is the only place the CLI says what happened."""
    directory = session_dir(session_id, kimi_home)
    if directory is None:
        return None
    logs = sorted(
        (path for path in directory.rglob(LOG_FILENAME) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in logs:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if " WARN " not in line and " ERROR " not in line:
                continue
            match = _LOG_ERROR_RE.search(line)
            if match:
                return match.group(1).encode().decode("unicode_escape", errors="replace")
    return None


def _last_usage(directory: Path) -> Optional[dict]:
    logs = sorted(
        (path for path in directory.rglob(WIRE_FILENAME) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in logs:
        found = _last_usage_in(path)
        if found is not None:
            return found
    return None


def _last_usage_in(path: Path) -> Optional[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        if '"usage"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partial first line is expected when the tail cuts one
        usage = _find_usage(record)
        if usage:
            return usage
    return None


def _find_usage(node) -> Optional[dict]:
    """The `usage` object wherever the record nests it — the log is the CLI's
    own event envelope, and its shape is its business, not a contract."""
    if isinstance(node, dict):
        usage = node.get("usage")
        if isinstance(usage, dict) and ("output" in usage or "inputOther" in usage):
            return usage
        for value in node.values():
            found = _find_usage(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_usage(value)
            if found:
                return found
    return None
