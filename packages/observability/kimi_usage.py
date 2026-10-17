"""Token counts for a Kimi turn, and account quota via GET /usages.

`kimi -p --output-format stream-json` reports no usage at all — the stream is
version, messages, resume hint, and nothing else. The CLI does record counts,
in the session's own wire log:

    {"usage": {"inputOther": 1058, "output": 69,
               "inputCacheRead": 18944, "inputCacheCreation": 0},
     "usageScope": "turn", ...}

(verified against kimi 2.0.2, 2026-09-23). Read back after the turn, the same
way Codex's real rate limits are read out of its rollout log, so a Kimi turn
still counts against the thread's token budget instead of looking free.

Account windows come from the managed OAuth API the CLI's `/usage` calls
(`.ana/KIMI_CODE_REA_REPORT.md`): `GET {base}/usages` with a Bearer token from
`~/.kimi-code/credentials/`, refreshed through `auth.kimi.ai` when expired.

Kimi's input fields are disjoint: `inputOther`, `inputCacheRead`, and
`inputCacheCreation` must all be included to count the full input. Unlike
Claude's usage payload, cached input is not already inside `inputOther`.
"""
import json
import os
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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


# --- Account quota (GET /usages) ---------------------------------------------

FIVE_HOUR = "five_hour"
MONTHLY = "monthly"
KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_API_BASE = "https://api.kimi.ai/coding/v1"
DEFAULT_OAUTH_HOST = "https://auth.kimi.ai"
USAGES_TIMEOUT = 8.0


def fetch_rate_limits(kimi_home: Optional[os.PathLike | str] = None) -> Optional[dict]:
    """Account windows from GET /usages, refreshing the OAuth token when needed.

    Returns `{"five_hour": {"utilization": 0..1, "resets_at": …}, "monthly": …}`.
    """
    home = Path(kimi_home) if kimi_home else default_kimi_home()
    token = _ensure_access_token(home)
    if not token:
        return None
    base = (
        os.environ.get("KIMI_CODE_BASE_URL")
        or os.environ.get("KIMI_BASE_URL")
        or DEFAULT_API_BASE
    ).rstrip("/")
    request = Request(
        f"{base}/usages",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "kimi-code-cli/2.0.2",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=USAGES_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return normalize_usages(body)


def normalize_usages(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    usages = payload.get("usages")
    if not isinstance(usages, dict):
        return None
    out: dict = {}
    mapping = (
        ("limit_5h", FIVE_HOUR),
        ("limit_month_total", MONTHLY),
    )
    for src, dst in mapping:
        window = usages.get(src)
        if not isinstance(window, dict):
            continue
        ratio = window.get("used_ratio")
        if not isinstance(ratio, (int, float)):
            continue
        entry: dict = {"utilization": max(0.0, min(1.0, float(ratio)))}
        resets = _parse_iso(window.get("reset_time"))
        if resets is not None:
            entry["resets_at"] = resets
        out[dst] = entry
    return out or None


def _ensure_access_token(home: Path) -> Optional[str]:
    path = _credentials_file(home)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = payload.get("access_token")
    expires_at = payload.get("expires_at")
    fresh = (
        isinstance(token, str)
        and token
        and isinstance(expires_at, (int, float))
        and float(expires_at) > time.time() + 30
    )
    if fresh:
        return token
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return token if isinstance(token, str) and token else None
    renewed = _refresh_token(home, refresh)
    if not renewed:
        return token if isinstance(token, str) and token else None
    try:
        path.write_text(json.dumps(renewed, indent=2), encoding="utf-8")
    except OSError:
        pass
    return renewed.get("access_token")


def _credentials_file(home: Path) -> Optional[Path]:
    directory = home / "credentials"
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("kimi-code-env-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _refresh_token(home: Path, refresh_token: str) -> Optional[dict]:
    oauth_host = (
        os.environ.get("KIMI_CODE_OAUTH_HOST")
        or os.environ.get("KIMI_OAUTH_HOST")
        or DEFAULT_OAUTH_HOST
    ).rstrip("/")
    body = urlencode(
        {
            "client_id": KIMI_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    device_id = _device_id(home)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "kimi-code-cli/2.0.2",
        "X-Msh-Platform": "linux",
        "X-Msh-Version": "2.0.2",
        "X-Msh-Device-Name": (platform.node() or "ana")[:64],
        "X-Msh-Device-Model": platform.machine() or "unknown",
        "X-Msh-Os-Version": platform.release() or "unknown",
        "X-Msh-Device-Id": device_id,
    }
    request = Request(f"{oauth_host}/api/oauth/token", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=USAGES_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    expires_in = data.get("expires_in")
    expires_at = (
        time.time() + float(expires_in)
        if isinstance(expires_in, (int, float))
        else time.time() + 900
    )
    return {
        "access_token": access,
        "refresh_token": data.get("refresh_token") or refresh_token,
        "expires_at": int(expires_at),
        "scope": data.get("scope") or "kimi-code",
        "token_type": data.get("token_type") or "Bearer",
        "expires_in": expires_in if isinstance(expires_in, (int, float)) else 900,
    }


def _device_id(home: Path) -> str:
    path = home / "device_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return "ana-usage-sync"


def _parse_iso(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None
