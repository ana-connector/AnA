"""Model routing.

Which model answers a turn is a budget question, not a content question: the
lead's job is the same whoever runs it, so the rule is "use the cheapest window
that still has room". The ladder lives in YAML (config/routing.yaml) so it can
be edited from the settings page without a restart.

A rung is a (CLI, model) pair plus the utilization at which ANA stops using it.
Utilization comes from the provider when it reports one and from ANA's own
token accounting otherwise — either way it is the same number the status bar
shows, so what the user sees is what the router used.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from packages.orchestrator.handoff import AGENTS, DEFAULT_AGENT

AUTO = "auto"
LEAD = "lead"
WORKER = "worker"

# Each CLI has its own vocabulary for how hard to think, and neither accepts
# the other's: `claude --effort` takes these five, while Codex takes a
# `model_reasoning_effort` config value. Validated at parse time so a typo in
# the settings editor is rejected there rather than killing a turn later.
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    # Verified against the provider's own rejection, not the docs: asking for
    # "minimal" comes back as
    #   Unsupported value: 'minimal' is not supported with the 'gpt-6-sol'
    #   model. Supported values are: 'none', 'low', 'medium', 'high',
    #   'xhigh', and 'max'.
    # — a 400 that failed every Codex planning call while ANA reported the
    # CLI's unrelated stderr warning instead.
    "codex": ("none", "low", "medium", "high", "xhigh", "max"),
    # Kimi has no effort flag at all; ANA writes the level into the prepared
    # home's config.toml, which is why each level gets its own home.
    "kimi": ("low", "high", "max"),
}

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"


@dataclass(frozen=True)
class Rung:
    agent: str
    model: Optional[str] = None
    # How hard the model should think on this rung. None means "whatever the
    # CLI defaults to", which is also what the environment's CLAUDE_EFFORT
    # gives when the ladder says nothing.
    effort: Optional[str] = None
    # Window name and the utilization at which this rung is considered spent.
    # None means "no limit": the last rung, where everything lands.
    window: Optional[str] = None
    limit: Optional[float] = None

    @property
    def label(self) -> str:
        return f"{self.agent}/{self.model}" if self.model else self.agent


@dataclass(frozen=True)
class RoutingDecision:
    agent: str
    model: Optional[str] = None
    effort: Optional[str] = None
    reason: str = ""
    # True when the client named the agent and the ladder was not consulted.
    explicit: bool = False

    @property
    def label(self) -> str:
        return f"{self.agent}/{self.model}" if self.model else self.agent


def _window_utilization(usage: Optional[dict], agent: str, window: str) -> float:
    """How much of `agent`'s `window` is spent, as 0..1. Unknown counts as 0:
    a rung nobody has measured yet is a rung with room."""
    if not usage:
        return 0.0
    windows = (usage.get(agent) or {}).get("windows") or []
    for entry in windows:
        if entry.get("key") == window or entry.get("label") == window:
            value = entry.get("utilization")
            return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0


class ModelLadder:
    def __init__(self, lead: list[Rung], workers: dict[str, list[Rung]]):
        self.lead = lead or [Rung(agent=DEFAULT_AGENT)]
        self.workers = workers or {}

    @classmethod
    def from_yaml(cls, path: os.PathLike | str) -> "ModelLadder":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("lead"), list) or not raw["lead"]:
            raise ValueError("routing rules require a non-empty lead ladder")
        return cls(
            lead=_parse_rungs(raw.get("lead"), "lead"),
            workers={
                name: _parse_rungs(rungs, f"workers.{name}")
                for name, rungs in (raw.get("workers") or {}).items()
            },
        )

    def rungs_for(self, role: str, preference: Optional[str] = None) -> list[Rung]:
        if role == LEAD:
            return self.lead
        by_name = self.workers.get(preference or "default") or self.workers.get("default")
        return by_name or self.lead

    def choose(
        self,
        role: str,
        usage: Optional[dict] = None,
        preference: Optional[str] = None,
    ) -> RoutingDecision:
        rungs = self.rungs_for(role, preference)
        for rung in rungs:
            if rung.window is None or rung.limit is None:
                return RoutingDecision(
                    agent=rung.agent,
                    model=rung.model,
                    effort=rung.effort,
                    reason="마지막 단계",
                )
            spent = _window_utilization(usage, rung.agent, rung.window)
            if spent < rung.limit:
                return RoutingDecision(
                    agent=rung.agent,
                    model=rung.model,
                    effort=rung.effort,
                    reason=f"{rung.window} {spent:.0%} < {rung.limit:.0%}",
                )
        last = rungs[-1]
        return RoutingDecision(
            agent=last.agent, model=last.model, effort=last.effort, reason="모든 단계 소진"
        )


def _parse_rungs(raw, where: str) -> list[Rung]:
    rungs: list[Rung] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        agent = entry.get("agent")
        if agent not in AGENTS:
            raise ValueError(f"{where}: unknown agent {agent!r}")
        until = entry.get("until") or {}
        window = until.get("window")
        limit = until.get("utilization")
        effort = entry.get("effort") or None
        if effort is not None and effort not in EFFORTS.get(agent, ()):
            raise ValueError(
                f"{where}: {agent} has no effort level {effort!r} "
                f"(expected one of {', '.join(EFFORTS.get(agent, ()))})"
            )
        rungs.append(
            Rung(
                agent=agent,
                model=(entry.get("model") or None),
                effort=effort,
                window=window if window else None,
                limit=float(limit) if isinstance(limit, (int, float)) else None,
            )
        )
    return rungs


@lru_cache(maxsize=4)
def _cached_ladder(path: str, mtime: float) -> ModelLadder:
    return ModelLadder.from_yaml(path)


def load_routing_table(path: os.PathLike | str | None = None) -> ModelLadder:
    """Re-reads the file whenever it changes: the point of YAML is editing it
    on a running system."""
    if path is None:
        # The settings editor seeds and writes this file. Reading the bundled
        # default here ignored every edit unless ANA_ROUTING_RULES was set.
        from packages.orchestrator.settings import routing_path

        path = routing_path()
    resolved = str(path)
    return _cached_ladder(resolved, os.stat(resolved).st_mtime_ns)


# What the planning call asks for. Cheap, but not the floor of the list:
# "none" turns reasoning off entirely, and the judgement being made — does
# this request need sub-agents, and which — is exactly the kind a model gets
# wrong without any.
PLANNING_EFFORT = {"claude": "low", "codex": "low", "kimi": "low"}


def planning_effort(agent: str) -> Optional[str]:
    """Effort for the planning call.

    Planning is a few lines of JSON deciding whether a request needs
    sub-agents, so it does not pay for deep reasoning on every turn.
    ANA_PLANNER_EFFORT overrides; a value the named CLI does not have is
    ignored rather than sent, because the provider answers an unknown level
    with a 400 that fails the turn."""
    levels = EFFORTS.get(agent, ())
    configured = os.environ.get("ANA_PLANNER_EFFORT")
    if configured and configured in levels:
        return configured
    preferred = PLANNING_EFFORT.get(agent)
    if preferred in levels:
        return preferred
    return levels[0] if levels else None


def model_for(agent: str, *, role: str = LEAD, preference: Optional[str] = None) -> Optional[str]:
    """The model this ladder pairs with `agent`.

    Used when the breaker moves a turn to another CLI: the model chosen for the
    rung that just failed means nothing to the next one ("fable" is not a Codex
    model), so each rung brings its own."""
    return settings_for(agent, role=role, preference=preference)[0]


def settings_for(
    agent: str, *, role: str = LEAD, preference: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """`(model, effort)` for `agent` on this ladder — the pair travels together
    for the same reason the model does: an effort level is only meaningful
    next to the model it was chosen for."""
    for rung in load_routing_table().rungs_for(role, preference):
        if rung.agent == agent:
            return rung.model, rung.effort
    return None, None


def decide(
    requested_agent: Optional[str],
    *,
    role: str = LEAD,
    usage: Optional[dict] = None,
    preference: Optional[str] = None,
) -> RoutingDecision:
    """Resolves the (CLI, model) for one turn.

    A concrete agent from the client is honored — the UI keeps a manual
    override — but the model still comes from that agent's first rung, so an
    override picks the CLI without pinning a model nobody chose."""
    ladder = load_routing_table()
    if requested_agent in AGENTS:
        for rung in ladder.rungs_for(role, preference):
            if rung.agent == requested_agent:
                return RoutingDecision(
                    agent=rung.agent,
                    model=rung.model,
                    effort=rung.effort,
                    reason="사용자 지정",
                    explicit=True,
                )
        return RoutingDecision(agent=requested_agent, reason="사용자 지정", explicit=True)
    return ladder.choose(role, usage=usage, preference=preference)
