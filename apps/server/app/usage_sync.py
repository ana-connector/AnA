"""Reading the providers' real usage figures on demand.

The status bar prefers the same account-quota APIs each CLI's `/usage` uses
(see `.ana/*_REA_REPORT.md`), so a sync costs an HTTP round trip rather than a
model turn. Codex still comes off its rollout log for free.

| Agent  | Source |
|--------|--------|
| Claude | `GET /api/oauth/usage` with `~/.claude/.credentials.json` |
| Cursor | Connect `GetCurrentPeriodUsage` with `~/.config/cursor/auth.json` |
| Kimi   | `GET /usages` with refreshed `~/.kimi-code/credentials` |
| Codex  | rollout `rate_limits` under CODEX_HOME |
| OpenCode | none — the CLI publishes no usage or quota API, so it has no window to sync (see packages/observability/limits.py) |

`ANA_USAGE_PROBE=on` keeps the Claude stream probe as a fallback when the
OAuth call fails (older installs / expired token without refresh).
"""
import asyncio
import contextlib
import os
from typing import Callable, Optional

from packages.observability.claude_usage import fetch_rate_limits as fetch_claude_limits
from packages.observability.codex_usage import read_rate_limits as read_codex_rate_limits
from packages.observability.cursor_usage import fetch_rate_limits as fetch_cursor_limits
from packages.observability.kimi_usage import fetch_rate_limits as fetch_kimi_limits

PROBE_PROMPT = "Reply with exactly: ok"
DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0


def probe_enabled() -> bool:
    return (os.environ.get("ANA_USAGE_PROBE", "on") or "").lower() != "off"


def probe_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("ANA_USAGE_PROBE_TIMEOUT_SECONDS",
                                             DEFAULT_PROBE_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_PROBE_TIMEOUT_SECONDS


def sync_codex(tracker) -> dict:
    try:
        windows = read_codex_rate_limits()
    except Exception as exc:  # noqa: BLE001
        return {"agent": "codex", "refreshed": False, "reason": str(exc)}
    if not windows:
        return {
            "agent": "codex",
            "refreshed": False,
            "reason": "codex 롤아웃 로그에 아직 한도 기록이 없습니다",
        }
    tracker.record_reported_windows("codex", windows)
    return {"agent": "codex", "refreshed": True, "source": "rollout log",
            "cost_usd": 0.0, "tokens": 0}


def sync_claude_http(tracker) -> dict:
    try:
        windows = fetch_claude_limits()
    except Exception as exc:  # noqa: BLE001
        return {"agent": "claude", "refreshed": False, "reason": str(exc)}
    if not windows:
        return {
            "agent": "claude",
            "refreshed": False,
            "reason": "Claude OAuth 사용량을 읽지 못했습니다",
        }
    tracker.record_reported_windows("claude", windows)
    return {"agent": "claude", "refreshed": True, "source": "oauth /usage",
            "cost_usd": 0.0, "tokens": 0}


def sync_cursor(tracker) -> dict:
    try:
        windows = fetch_cursor_limits()
    except Exception as exc:  # noqa: BLE001
        return {"agent": "cursor", "refreshed": False, "reason": str(exc)}
    if not windows:
        return {
            "agent": "cursor",
            "refreshed": False,
            "reason": "Cursor 사용량을 읽지 못했습니다",
        }
    tracker.record_reported_windows("cursor", windows)
    return {"agent": "cursor", "refreshed": True, "source": "DashboardService",
            "cost_usd": 0.0, "tokens": 0}


def sync_kimi(tracker) -> dict:
    try:
        windows = fetch_kimi_limits()
    except Exception as exc:  # noqa: BLE001
        return {"agent": "kimi", "refreshed": False, "reason": str(exc)}
    if not windows:
        return {
            "agent": "kimi",
            "refreshed": False,
            "reason": "Kimi /usages를 읽지 못했습니다",
        }
    tracker.record_reported_windows("kimi", windows)
    return {"agent": "kimi", "refreshed": True, "source": "GET /usages",
            "cost_usd": 0.0, "tokens": 0}


async def sync_claude(tracker, adapter_factory: Callable) -> dict:
    """OAuth first; optional probe turn only when that fails and probes are on."""
    http = sync_claude_http(tracker)
    if http.get("refreshed"):
        return http
    if not probe_enabled():
        return http
    try:
        adapter = adapter_factory()
    except Exception as exc:  # noqa: BLE001
        # Factory failure is why the probe did not run; keep the OAuth miss as
        # secondary context only when the exception has nothing to say.
        return {**http, "reason": str(exc) or http.get("reason")}

    reported: Optional[dict] = None
    tokens = 0
    cost_usd = 0.0
    error = ""
    try:
        async with asyncio.timeout(probe_timeout_seconds()):
            async for event in adapter.run_stream(PROBE_PROMPT):
                if event.type == "usage" and event.usage:
                    reported = event.usage
                if event.type in ("usage", "result"):
                    tokens += _tokens(event.usage)
                if event.type == "result":
                    cost_usd = float(event.cost_usd or 0.0)
                    if event.is_error and event.text:
                        error = event.text
                if event.type == "error" and event.text:
                    error = event.text
    except asyncio.TimeoutError:
        error = f"{probe_timeout_seconds():.0f}초 안에 응답이 없었습니다"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    finally:
        with contextlib.suppress(Exception):
            await adapter.terminate()

    if tokens or cost_usd:
        tracker.record_tokens("claude", tokens, usd=cost_usd)
    if reported:
        tracker.record_reported("claude", reported)
        return {"agent": "claude", "refreshed": True, "source": "probe turn",
                "cost_usd": cost_usd, "tokens": tokens}
    return {
        "agent": "claude",
        "refreshed": False,
        "reason": error or http.get("reason") or "탐색 턴이 한도 정보를 보내지 않았습니다",
        "cost_usd": cost_usd,
        "tokens": tokens,
    }


def _tokens(usage: Optional[dict]) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(
        int(usage.get(field) or 0)
        for field in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens")
    )


async def sync_usage(tracker, adapter_factory: Callable) -> list[dict]:
    """Every configured CLI. Cheap HTTP / disk first; Claude probe last."""
    results = [
        sync_codex(tracker),
        sync_cursor(tracker),
        sync_kimi(tracker),
        await sync_claude(tracker, adapter_factory),
    ]
    return results
