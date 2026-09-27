"""Rungs that refused for lack of capacity are rested, not re-asked per task."""
from dataclasses import dataclass
from typing import Optional

from packages.orchestrator.cooldown import Cooldowns, label_for, usable


@dataclass
class Rung:
    agent: str
    model: Optional[str] = None


def test_a_rung_that_hit_its_limit_is_skipped_until_the_cooldown_ends():
    now = [1000.0]
    cooldowns = Cooldowns(clock=lambda: now[0])
    ladder = [Rung("kimi", "kimi-code/k3"), Rung("claude", "opus")]

    cooldowns.trip(label_for("kimi", "kimi-code/k3"), reason="5-hour usage limit", seconds=60)

    assert [r.agent for r in usable(ladder, cooldowns=cooldowns)] == ["claude"]
    assert cooldowns.reason("kimi/kimi-code/k3") == "5-hour usage limit"

    now[0] += 61  # the window resets; the way to find out is to try again
    assert [r.agent for r in usable(ladder, cooldowns=cooldowns)] == ["kimi", "claude"]


def test_every_rung_resting_still_offers_the_whole_ladder():
    """A cooldown is an optimization. It must never turn into "ANA has no
    workers left" — the refusal the user then sees would be ANA's, not the
    provider's."""
    now = [0.0]
    cooldowns = Cooldowns(clock=lambda: now[0])
    ladder = [Rung("kimi", "k3"), Rung("codex", "gpt")]
    for rung in ladder:
        cooldowns.trip(label_for(rung.agent, rung.model), seconds=600)

    assert [r.agent for r in usable(ladder, cooldowns=cooldowns)] == ["kimi", "codex"]
