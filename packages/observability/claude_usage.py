"""Claude subscription windows via the same OAuth usage API the CLI's `/usage` uses.

Verified against Claude Code 2.1.292 REA (`.ana/CLAUDE_CODE_REA_REPORT.md`) and a
live `GET https://api.anthropic.com/api/oauth/usage` call: utilization arrives
as a 0..100 percent, `resets_at` as an ISO-8601 string.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
DEFAULT_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT_SECONDS = 5.0


def default_credentials_path() -> Path:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return home / ".credentials.json"


def read_access_token(credentials_path: Optional[os.PathLike | str] = None) -> Optional[str]:
    path = Path(credentials_path) if credentials_path else default_credentials_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def fetch_rate_limits(
    *,
    credentials_path: Optional[os.PathLike | str] = None,
    url: Optional[str] = None,
) -> Optional[dict]:
    """Provider windows as UsageTracker wants them, or None when unavailable.

    Returns `{"five_hour": {"utilization": 0.17, "resets_at": …}, "seven_day": …}`.
    """
    token = read_access_token(credentials_path)
    if not token:
        return None
    endpoint = url or os.environ.get("CLAUDE_USAGE_URL") or DEFAULT_URL
    request = Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return normalize_oauth_usage(body)


def normalize_oauth_usage(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    out: dict = {}
    for key in (FIVE_HOUR, SEVEN_DAY):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        raw = window.get("utilization")
        if not isinstance(raw, (int, float)):
            continue
        # OAuth `/usage` reports 0..100; Claude's stream rate_limit_event uses 0..1.
        utilization = float(raw)
        if utilization > 1.0:
            utilization = utilization / 100.0
        entry: dict = {"utilization": max(0.0, min(1.0, utilization))}
        resets = _parse_resets(window.get("resets_at"))
        if resets is not None:
            entry["resets_at"] = resets
        out[key] = entry
    return out or None


def _parse_resets(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        # Milliseconds if clearly not epoch-seconds.
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None
