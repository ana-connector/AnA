"""Circuit breaker and failover chain.

Two triggers, one pipeline (roadmap stages 2 and 3): a turn that dies on a
provider's usage limit, and a thread that crosses a cost/token threshold.
Both resolve the same way — move to the next rung of the failover chain and
keep the conversation going — because the point is never to stop working, it
is to stop hammering the resource that just said no.

Chain, per the roadmap: switch the Claude CLI's auth mode first (a
subscription limit and an API-billing limit are different buckets), and only
then hand the work to another CLI — Codex, then Kimi.
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

CLAUDE = "claude"
CODEX = "codex"
KIMI = "kimi"
TAPIE = "tapie"
CURSOR = "cursor"
OPENCODE = "opencode"

# Substrings seen in a CLI's error output when the provider refuses on usage
# rather than on the request itself. Deliberately broad: a missed match keeps
# the failure as a plain error (the user sees it), while a false positive only
# costs one retry on another rung.
_USAGE_LIMIT_PATTERNS = (
    r"usage limit",
    # What the Claude CLI actually prints when a plan's window is exhausted:
    # "You've hit your session limit · resets 3:20am (UTC)". Without this the
    # breaker never fired and the turn just failed.
    r"session limit",
    r"hit your .{0,30}limit",
    r"limit reached",
    r"limit · resets",
    r"resets at",
    r"weekly limit",
    r"rate limit",
    r"rate_limit",
    r"quota",
    r"insufficient[_ ]credit",
    r"credit balance",
    # "Fable 5 requires usage credits. Run /usage …" — a model the plan cannot
    # pay for is the same situation as a spent window: try the next rung.
    r"requires .{0,20}credits",
    r"usage credits",
    r"too many requests",
    r"429",
    r"overloaded",
    r"resource[_ ]exhausted",
)
_USAGE_LIMIT_RE = re.compile("|".join(_USAGE_LIMIT_PATTERNS), re.IGNORECASE)

# Account and credential failures are recoverable by another configured CLI,
# just like an exhausted window. Keep this narrow: a gate denial or a bad
# user command must still be reported rather than retried elsewhere.
_AUTH_FAILURE_RE = re.compile(
    r"failed to authenticate|authentication failed|invalid api key|"
    r"api error:\s*40[13]\b|(?:account|claude code).{0,30}(?:suspended|paused)|"
    r"claude code가 일시정지",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rung:
    """One step of the failover chain."""

    agent: str
    # Only meaningful for the Claude CLI: "pro" (subscription) or "api".
    auth_mode: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.agent}/{self.auth_mode}" if self.auth_mode else self.agent


@dataclass(frozen=True)
class BreakerConfig:
    max_thread_cost_usd: float = 5.0
    max_thread_tokens: int = 2_000_000
    rate_limit_utilization: float = 0.95
    cooldown_seconds: float = 900.0

    @classmethod
    def from_env(cls) -> "BreakerConfig":
        return cls(
            max_thread_cost_usd=float(os.environ.get("ANA_MAX_THREAD_COST_USD", 5.0)),
            max_thread_tokens=int(os.environ.get("ANA_MAX_THREAD_TOKENS", 2_000_000)),
            rate_limit_utilization=float(
                os.environ.get("ANA_RATE_LIMIT_UTILIZATION", 0.95)
            ),
            cooldown_seconds=float(os.environ.get("ANA_FAILOVER_COOLDOWN_SECONDS", 900)),
        )


def is_usage_limit(text: str) -> bool:
    """Whether a CLI's error text reads as "you are out of budget/quota"
    rather than "your request was wrong"."""
    return bool(text) and bool(_USAGE_LIMIT_RE.search(text))


def is_auth_failure(text: str) -> bool:
    """A provider rejected this CLI's credentials or account."""
    return bool(text) and bool(_AUTH_FAILURE_RE.search(text))


def is_recoverable_failure(text: str) -> bool:
    return is_usage_limit(text) or is_auth_failure(text)


def rate_limit_exceeded(usage: Optional[dict], threshold: float) -> bool:
    """Reads the Claude CLI's rate_limit_event payload, whose windows carry a
    0..1 utilization. Any window at or above the threshold counts: being out
    of five-hour budget stops the turn just as surely as a weekly cap."""
    if not usage:
        return False
    windows = usage.get("unifiedWindows") or usage.get("windows") or {}
    if not isinstance(windows, dict):
        return False
    for window in windows.values():
        if isinstance(window, dict):
            utilization = window.get("utilization")
            if isinstance(utilization, (int, float)) and utilization >= threshold:
                return True
    return False


def build_chain(preferred_agent: str, preferred_mode: Optional[str]) -> list[Rung]:
    """The order to try, starting from what the user asked for.

    TAPIE comes before Codex: it is the same CLI on a different account, so
    the cheapest way out of a limit is usually the same CLI billed elsewhere.
    Another CLI is last because switching costs the conversation its native
    session and needs a handoff brief.

    `preferred_mode` is vestigial — the API-key path is the `tapie` agent now
    — and is accepted so existing callers keep working."""
    if preferred_agent == CODEX:
        # Nothing to re-auth on the Codex side, so the rungs below it are the
        # other CLIs.
        return [
            Rung(CODEX), Rung(CLAUDE), Rung(TAPIE), Rung(KIMI), Rung(CURSOR),
            Rung(OPENCODE),
        ]
    if preferred_agent == KIMI:
        # Same shape: one account, one bucket, so the way out of a Kimi limit
        # is another CLI entirely.
        return [
            Rung(KIMI), Rung(CLAUDE), Rung(TAPIE), Rung(CODEX), Rung(CURSOR),
            Rung(OPENCODE),
        ]
    if preferred_agent == CURSOR:
        return [
            Rung(CURSOR), Rung(CLAUDE), Rung(TAPIE), Rung(CODEX), Rung(KIMI),
            Rung(OPENCODE),
        ]
    if preferred_agent == TAPIE:
        # TAPIE is the same CLI on a key; the subscription is the nearest
        # place to land, for the same reason it is TAPIE's neighbour below.
        return [
            Rung(TAPIE), Rung(CLAUDE), Rung(CODEX), Rung(KIMI), Rung(CURSOR),
            Rung(OPENCODE),
        ]
    if preferred_agent == OPENCODE:
        # OpenCode is many providers behind one CLI, so a limit here is a
        # limit on one of them: the other CLIs are the way out, and OpenCode
        # on another model is a rung the ladder can name itself.
        return [
            Rung(OPENCODE), Rung(CLAUDE), Rung(TAPIE), Rung(CODEX), Rung(KIMI),
            Rung(CURSOR),
        ]
    return [
        Rung(CLAUDE), Rung(TAPIE), Rung(CODEX), Rung(KIMI), Rung(CURSOR),
        Rung(OPENCODE),
    ]


def enabled_chain(chain: list[Rung]) -> list[Rung]:
    """The chain without the CLIs the settings page has switched off.

    Applied here rather than inside each `build_chain` branch so the orders
    above stay readable as what they are: the preference, not the filter. A
    chain filtered down to nothing keeps its first rung — failing over to a
    CLI the user switched off is wrong, but so is having nowhere to go."""
    try:
        from packages.orchestrator.routing import allowed_agents

        allowed = allowed_agents()
    except Exception:  # noqa: BLE001
        return chain
    kept = [rung for rung in chain if rung.agent in allowed]
    return kept or chain[:1]


def next_rung(chain: list[Rung], tried: list[str]) -> Optional[Rung]:
    """The first rung not tried yet, or None when the chain is exhausted."""
    for rung in chain:
        if rung.label not in tried:
            return rung
    return None


@dataclass(frozen=True)
class BreakerState:
    """What a thread remembers about a trip, so recovery is automatic.

    `tripped_at` plus the configured cooldown is the whole mechanism: while
    the cooldown is unexpired the thread keeps using the rung it fell back
    to, and once it expires the next turn starts from the preferred rung
    again. No probing, no background timer — the roadmap's "사용량이 회복되면
    자동으로 원래대로" without a scheduler."""

    rung_label: Optional[str] = None
    tripped_at: float = 0.0
    reason: str = ""

    def active(self, cooldown_seconds: float, now: Optional[float] = None) -> bool:
        if not self.rung_label:
            return False
        now = time.time() if now is None else now
        return (now - self.tripped_at) < cooldown_seconds


def thread_budget_exceeded(
    config: BreakerConfig, total_cost_usd: float, total_tokens: int
) -> Optional[str]:
    """Why the thread's budget breaker should trip, or None."""
    if total_cost_usd >= config.max_thread_cost_usd:
        return (
            f"thread cost ${total_cost_usd:.2f} reached the "
            f"${config.max_thread_cost_usd:.2f} limit"
        )
    if total_tokens >= config.max_thread_tokens:
        return f"thread used {total_tokens} tokens, at the {config.max_thread_tokens} limit"
    return None
