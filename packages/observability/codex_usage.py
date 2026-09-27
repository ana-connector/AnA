"""Codex's own rate-limit numbers.

`codex exec --json` reports tokens but not limits, so ANA used to estimate the
Codex side of the status bar from the tokens it had seen against a configured
budget. It does not have to: the CLI writes the provider's real figures into
its rollout log for every turn —

    "rate_limits": {"limit_id": "codex",
                    "primary":   {"used_percent": 95.0, "window_minutes": 300,
                                  "resets_at": 1790153969},
                    "secondary": {"used_percent": 29.0, "window_minutes": 10080,
                                  "resets_at": 1790242160}}

(verified against codex-cli 0.156.1, 2026-09-23). This reads the newest such
record back, so both CLIs' bars show a provider number rather than a guess.

Windows are matched by their length rather than by the names "primary" and
"secondary": what matters is which of ANA's two windows a figure belongs to,
and the CLI states that in minutes.
"""
import json
import os
from pathlib import Path
from typing import Optional

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"

# How much of a rollout file's tail to read. A rate_limits record is ~200
# bytes and the last turn's is near the end; reading the whole file would mean
# reading every message of a long conversation to find it.
TAIL_BYTES = 256 * 1024
# How many recently-touched rollout files to look at. More than one because
# the newest file is not always the one that just ran (a resumed session
# writes to its original file).
FILES_CHECKED = 5


def default_codex_home() -> Path:
    """Where ANA's own CODEX_HOME lives — the same directory
    packages/governance/cli_config.prepare_codex_home() builds, shared with
    workspace containers through the state volume."""
    state_dir = os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana"))
    return Path(os.environ.get("CODEX_HOME") or Path(state_dir) / "codex_home")


# Last parse, keyed on the newest rollout file and its mtime: the status bar
# asks on every refresh, and re-reading a quarter megabyte of JSONL to learn
# that nothing has changed is waste.
_CACHE: dict[str, tuple[float, Optional[dict]]] = {}


def read_rate_limits(codex_home: Optional[os.PathLike | str] = None) -> Optional[dict]:
    """The newest rate-limit snapshot Codex recorded, or None if there is none.

    Returns `{"five_hour": {"utilization": 0.95, "resets_at": 1790153969}, ...}`
    — the shape UsageTracker stores for a provider-reported window."""
    home = Path(codex_home) if codex_home is not None else default_codex_home()
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    files = sorted(
        (path for path in sessions.rglob("rollout-*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:FILES_CHECKED]
    if not files:
        return None

    newest = files[0].stat().st_mtime
    cached = _CACHE.get(str(sessions))
    if cached is not None and cached[0] == newest:
        return cached[1]

    best: Optional[dict] = None
    best_at = -1.0
    for path in files:
        payload = _last_rate_limits(path)
        if payload is None:
            continue
        # Several files may each carry a record; the most recently written
        # file wins, which is why `files` is sorted by mtime.
        at = path.stat().st_mtime
        if at > best_at:
            best, best_at = payload, at
    windows = _windows(best) if best else None
    _CACHE[str(sessions)] = (newest, windows)
    return windows


def _last_rate_limits(path: Path) -> Optional[dict]:
    """Scans one rollout file's tail backwards for the last rate_limits."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    # A partial first line is expected when the tail starts mid-file; it is
    # dropped by the JSON parse below like any other unparseable line.
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_rate_limits(record)
        # A rollout may end with a different limit bucket (for example
        # "premium") whose primary and secondary windows are null. Keep
        # looking for the last usable Codex snapshot in that same file.
        if found and _windows(found):
            return found
    return None


def _find_rate_limits(node) -> Optional[dict]:
    """The `rate_limits` object, wherever the rollout record nests it.

    Searched rather than addressed by path: the record is the CLI's own event
    envelope, and its shape is its business, not a contract with ANA."""
    if isinstance(node, dict):
        limits = node.get("rate_limits")
        if isinstance(limits, dict):
            return limits
        for value in node.values():
            found = _find_rate_limits(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_rate_limits(value)
            if found:
                return found
    return None


def _windows(limits: dict) -> Optional[dict]:
    windows: dict[str, dict] = {}
    for entry in limits.values():
        if not isinstance(entry, dict):
            continue
        used = entry.get("used_percent")
        minutes = entry.get("window_minutes")
        if not isinstance(used, (int, float)) or not isinstance(minutes, (int, float)):
            continue
        key = _window_key(minutes)
        if key is None:
            continue
        window = {"utilization": min(max(used / 100.0, 0.0), 1.0)}
        resets_at = entry.get("resets_at")
        if isinstance(resets_at, (int, float)):
            window["resets_at"] = float(resets_at)
        # A shorter window wins a tie: two entries that both map to five_hour
        # would otherwise depend on dict order.
        if key not in windows or minutes < windows[key].get("_minutes", minutes + 1):
            window["_minutes"] = float(minutes)
            windows[key] = window
    for window in windows.values():
        window.pop("_minutes", None)
    return windows or None


def _window_key(minutes: float) -> Optional[str]:
    """Which of ANA's windows a Codex window corresponds to. Codex reports 300
    minutes (5 hours) and 10080 (7 days); anything under half a day is treated
    as the short window and anything longer as the weekly one, so a plan with
    slightly different windows still lands somewhere sensible."""
    if minutes <= 0:
        return None
    return FIVE_HOUR if minutes < 12 * 60 else SEVEN_DAY
