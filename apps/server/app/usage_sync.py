"""Reading the providers' real usage figures on demand.

The status bar shows what the CLIs happened to report on their last turn. That
is fine while ANA is working and stale the rest of the time: come back an hour
later and the bar still describes the window as it was, which is exactly when
someone wants to know whether there is room to start something.

The two CLIs hand their figures over very differently:

- **Codex** writes its rate limits into its own rollout log, so they can be
  re-read from disk at any time, for nothing
  (packages/observability/codex_usage.py).
- **Claude** only emits them as a `rate_limit_event` while a turn is streaming
  (packages/adapters/claude_code.py). There is no file to read. So a real
  "sync now" has to *run* something — one short prompt, with no tools and no
  gate token, purely to collect the event that comes with it.

That probe costs a few hundred tokens, which is why it is a button the user
presses rather than a poll, why what it spends is recorded like any other turn,
and why `ANA_USAGE_PROBE=off` exists for a deployment that would rather have a
stale number than pay for a fresh one.
"""
import asyncio
import contextlib
import os
from typing import Callable, Optional

from packages.observability.codex_usage import read_rate_limits as read_codex_rate_limits

# Short, tool-free, and cheap to answer. The answer is thrown away — what the
# call is for is the rate_limit_event that arrives alongside it.
PROBE_PROMPT = "Reply with exactly: ok"
DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0


def probe_enabled() -> bool:
    """ANA_USAGE_PROBE=off keeps the button from ever spending anything: Codex
    still syncs from its log, Claude reports that it cannot."""
    return (os.environ.get("ANA_USAGE_PROBE", "on") or "").lower() != "off"


def probe_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("ANA_USAGE_PROBE_TIMEOUT_SECONDS",
                                             DEFAULT_PROBE_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_PROBE_TIMEOUT_SECONDS


def sync_codex(tracker) -> dict:
    """Codex's real windows, straight off its rollout log. Costs nothing."""
    try:
        windows = read_codex_rate_limits()
    except Exception as exc:  # noqa: BLE001 - a log ANA cannot read is a result
        return {"agent": "codex", "refreshed": False, "reason": str(exc)}
    if not windows:
        return {
            "agent": "codex",
            "refreshed": False,
            # Not an error: a Codex that has never run has no log to read.
            "reason": "codex 롤아웃 로그에 아직 한도 기록이 없습니다",
        }
    tracker.record_reported_windows("codex", windows)
    return {"agent": "codex", "refreshed": True, "source": "rollout log",
            "cost_usd": 0.0, "tokens": 0}


async def sync_claude(tracker, adapter_factory: Callable) -> dict:
    """Claude's real windows, by running one short prompt to collect them.

    `adapter_factory` builds the CLI adapter. It is passed in rather than
    imported so this stays testable without a CLI, and it is called with no gate
    token: a probe makes no tool calls, so putting it through the gate would
    only add an audit row about a prompt that touches nothing."""
    if not probe_enabled():
        return {
            "agent": "claude",
            "refreshed": False,
            "reason": "ANA_USAGE_PROBE=off 이므로 탐색 턴을 돌리지 않았습니다",
        }
    try:
        adapter = adapter_factory()
    except Exception as exc:  # noqa: BLE001 - a CLI that cannot be built is a
        # configuration problem the caller should see, not a 500.
        return {"agent": "claude", "refreshed": False, "reason": str(exc)}

    reported: Optional[dict] = None
    tokens = 0
    cost_usd = 0.0
    error = ""
    try:
        async with asyncio.timeout(probe_timeout_seconds()):
            async for event in adapter.run_stream(PROBE_PROMPT):
                if event.type == "usage" and event.usage:
                    # The rate_limit_event's payload: window utilization, which
                    # is the whole point of the probe.
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
    except Exception as exc:  # noqa: BLE001 - reported, not raised: a failed
        # sync must not take the usage endpoint down with it.
        error = str(exc)
    finally:
        with contextlib.suppress(Exception):
            await adapter.terminate()

    # Counted even when the probe failed: a call that reached the provider and
    # came back with a limit message still spent the request.
    if tokens or cost_usd:
        tracker.record_tokens("claude", tokens, usd=cost_usd)
    if reported:
        tracker.record_reported("claude", reported)
        return {"agent": "claude", "refreshed": True, "source": "probe turn",
                "cost_usd": cost_usd, "tokens": tokens}
    return {
        "agent": "claude",
        "refreshed": False,
        # A probe that ran and reported nothing is worth saying out loud: the
        # figure on screen is the old one, and the user is entitled to know
        # they paid for a request that did not refresh it.
        "reason": error or "탐색 턴이 한도 정보를 보내지 않았습니다",
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
    """Both CLIs, cheapest first.

    Codex is synced before the Claude probe so that a probe which hangs or
    fails still leaves the free half of the work done."""
    return [sync_codex(tracker), await sync_claude(tracker, adapter_factory)]
