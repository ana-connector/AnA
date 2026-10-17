"""Cursor plan usage via DashboardService/GetCurrentPeriodUsage.

Verified against Cursor Agent 2026.10.16 REA
(`.ana/CURSOR_AGENT_USAGE_REA_REPORT.md`) and a live Connect unary call:
`planUsage.totalPercentUsed` is 0..100, `billingCycleEnd` is epoch millis
(string or number). Cursor has no 5h/7d windows — ANA maps the plan period to
`monthly`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MONTHLY = "monthly"
DEFAULT_BASE = "https://api2.cursor.sh"
TIMEOUT_SECONDS = 10.0


def default_auth_path() -> Path:
    configured = os.environ.get("CURSOR_CONFIG_DIR")
    root = Path(configured) if configured else Path.home() / ".config" / "cursor"
    return root / "auth.json"


def read_access_token(auth_path: Optional[os.PathLike | str] = None) -> Optional[str]:
    path = Path(auth_path) if auth_path else default_auth_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("accessToken")
    return token if isinstance(token, str) and token else None


def fetch_rate_limits(
    *,
    auth_path: Optional[os.PathLike | str] = None,
    base_url: Optional[str] = None,
) -> Optional[dict]:
    """`{"monthly": {"utilization": 0.03, "resets_at": …}}` or None."""
    token = read_access_token(auth_path)
    if not token:
        return None
    base = (base_url or os.environ.get("CURSOR_API_BASE_URL") or DEFAULT_BASE).rstrip("/")
    endpoint = f"{base}/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
    request = Request(
        endpoint,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-cursor-client-type": "cli",
            "x-ghost-mode": "true",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return normalize_period_usage(body)


def normalize_period_usage(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    plan = payload.get("planUsage") or payload.get("plan_usage")
    if not isinstance(plan, dict):
        return None
    raw = plan.get("totalPercentUsed")
    if raw is None:
        raw = plan.get("total_percent_used")
    if not isinstance(raw, (int, float)):
        included = plan.get("includedSpend") or plan.get("included_spend")
        limit = plan.get("limit")
        if isinstance(included, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            raw = (included / limit) * 100.0
        else:
            return None
    utilization = float(raw)
    if utilization > 1.0:
        utilization = utilization / 100.0
    entry: dict = {"utilization": max(0.0, min(1.0, utilization))}
    end = payload.get("billingCycleEnd") or payload.get("billing_cycle_end")
    resets = _millis_to_epoch(end)
    if resets is not None:
        entry["resets_at"] = resets
    return {MONTHLY: entry}


def _millis_to_epoch(value) -> Optional[float]:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e12 else float(value)
    return None
