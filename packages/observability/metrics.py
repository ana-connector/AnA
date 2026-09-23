"""Prometheus metrics for the orchestrator.

Kept in one module so every label set is defined in a single place: labels
with unbounded values (a shell command, a thread id) would create a new time
series per call and eventually take the scrape down, so only bounded values
are used — agent names, verdicts, and rule ids, all of which come from a
fixed list or the YAML rule set.
"""
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Classic exposition, not OpenMetrics: generate_latest() emits the classic
# text format, and serving it under the OpenMetrics content type makes
# Prometheus reject the scrape with "data does not end with # EOF".

REGISTRY = CollectorRegistry()

turns_total = Counter(
    "ana_turns_total",
    "Turns completed, by the agent that answered and how the turn ended",
    ["agent", "outcome"],
    registry=REGISTRY,
)

turn_duration_seconds = Histogram(
    "ana_turn_duration_seconds",
    "Wall-clock duration of a turn, including any failover retries",
    ["agent"],
    # A CLI turn runs for seconds to minutes, so the default buckets (which
    # top out at 10s) would put almost everything in +Inf.
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)

turn_cost_usd_total = Counter(
    "ana_turn_cost_usd_total",
    "Reported cost in USD (the Claude CLI reports this; Codex does not)",
    ["agent"],
    registry=REGISTRY,
)

turn_tokens_total = Counter(
    "ana_turn_tokens_total",
    "Input+output tokens reported by the CLI",
    ["agent"],
    registry=REGISTRY,
)

gate_decisions_total = Counter(
    "ana_gate_decisions_total",
    "Governance gate decisions",
    ["verdict", "decided_by", "rule_id"],
    registry=REGISTRY,
)

gate_wait_seconds = Histogram(
    "ana_gate_wait_seconds",
    "How long an approval request waited for a human answer",
    buckets=(1, 5, 15, 30, 60, 120, 300),
    registry=REGISTRY,
)

failovers_total = Counter(
    "ana_failovers_total",
    "Circuit breaker moves from one failover rung to the next",
    ["from_rung", "to_rung"],
    registry=REGISTRY,
)

subagents_total = Counter(
    "ana_subagents_total",
    "Sub-agent tasks run by the planner fan-out",
    ["agent", "status"],
    registry=REGISTRY,
)

active_sessions = Gauge(
    "ana_active_sessions",
    "Open chat connections (each holds one gate token)",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Body and content type for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def record_turn(
    agent: str,
    *,
    outcome: str,
    duration_seconds: float,
    cost_usd: float = 0.0,
    tokens: int = 0,
) -> None:
    turns_total.labels(agent=agent, outcome=outcome).inc()
    turn_duration_seconds.labels(agent=agent).observe(duration_seconds)
    if cost_usd:
        turn_cost_usd_total.labels(agent=agent).inc(cost_usd)
    if tokens:
        turn_tokens_total.labels(agent=agent).inc(tokens)


def record_gate_decision(verdict: str, decided_by: str, rule_id: str | None) -> None:
    gate_decisions_total.labels(
        verdict=verdict, decided_by=decided_by, rule_id=rule_id or "none"
    ).inc()


def record_subagent(agent: str, status: str) -> None:
    subagents_total.labels(agent=agent, status=status).inc()


def record_failover(from_rung: str, to_rung: str) -> None:
    failovers_total.labels(from_rung=from_rung, to_rung=to_rung).inc()
