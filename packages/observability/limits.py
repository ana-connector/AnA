"""Usage-limit tracking for the status bar.

Two sources, deliberately kept apart because they are not the same claim:

- **Reported**: the Claude CLI emits `rate_limit_event` with each window's real
  utilization. That is the provider's own number and is used as-is.
- **Estimated**: nothing equivalent exists for Codex, so ANA counts the tokens
  it has seen in the last 5 hours and 7 days against a configured budget. It is
  an estimate of *our* usage, not a reading of the provider's limit, and the UI
  labels it that way rather than implying it knows the real cap.
"""
import os
import time
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from packages.orchestrator.handoff import AGENTS

def _env_int(name: str, default: int) -> int:
    """An unparseable budget is a typo in .env, not a reason to refuse to
    start: the default stands and the bar keeps working."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


FIVE_HOURS = 5 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60


@dataclass
class _Sample:
    at: float
    tokens: int


@dataclass
class WindowUsage:
    label: str
    # Stable name the routing ladder matches on ("five_hour", "seven_day"),
    # separate from the human label the status bar shows.
    key: str
    seconds: int
    tokens: int = 0
    budget: int = 0
    # Provider-reported utilization (0..1) when the CLI gave one.
    reported: Optional[float] = None
    manual: Optional[float] = None
    # When this window next resets, as the provider stated it (or as the user
    # did, for a manual figure). None means nobody said.
    resets_at: Optional[float] = None

    @property
    def utilization(self) -> Optional[float]:
        if self.reported is not None:
            return self.reported
        if self.manual is not None:
            return self.manual
        if self.budget <= 0:
            return None
        return min(self.tokens / self.budget, 1.0)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "key": self.key,
            "seconds": self.seconds,
            "tokens": self.tokens,
            "budget": self.budget,
            "utilization": self.utilization,
            "resets_at": self.resets_at,
            "source": "reported" if self.reported is not None else ("manual" if self.manual is not None else "estimated"),
        }


class UsageTracker:
    """Per-agent token history plus whatever the provider reported.

    In memory on purpose: it answers "how close am I right now", and a restart
    losing a few hours of samples is a smaller problem than a schema for it."""

    def __init__(self, five_hour_budget: Optional[int] = None, weekly_budget: Optional[int] = None):
        self.five_hour_budget = five_hour_budget or _env_int(
            "ANA_FIVE_HOUR_TOKEN_BUDGET", 2_000_000
        )
        self.weekly_budget = weekly_budget or _env_int(
            "ANA_WEEKLY_TOKEN_BUDGET", 20_000_000
        )
        # Per-agent overrides, for the CLIs whose plan ANA cannot read. Claude
        # and Codex report real utilization, so their budget only matters
        # before the first turn; Kimi reports none at all, so *its* bar is
        # entirely this number and a global default is a guess about the wrong
        # plan. ANA_KIMI_FIVE_HOUR_TOKEN_BUDGET / ANA_KIMI_WEEKLY_TOKEN_BUDGET
        # (and the same for CLAUDE_/CODEX_) set it per agent.
        self._agent_budgets = {
            agent: (
                _env_int(f"ANA_{agent.upper()}_FIVE_HOUR_TOKEN_BUDGET", 0),
                _env_int(f"ANA_{agent.upper()}_WEEKLY_TOKEN_BUDGET", 0),
            )
            for agent in AGENTS
        }
        self._samples: dict[str, list[_Sample]] = {}
        # agent -> window -> {"utilization": float, "resets_at": float | None}.
        # Both CLIs state a reset instant with their figures, and a reported
        # number outlives its window the same way a manual one does: once the
        # window has reset, "95% spent" describes a window that is gone.
        self._reported: dict[str, dict[str, dict]] = {}
        self._manual: dict[str, dict[str, dict[str, float]]] = {}
        state_dir = Path(os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana")))
        self._manual_path = state_dir / "usage-estimates.json"
        try:
            self._manual = json.loads(self._manual_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        # Provider-reported figures are persisted too. They arrive only while a
        # turn is running — the Claude CLI states them on its stream and
        # nowhere else — so a restart used to leave the bar reading
        # "estimated 0%" for that CLI until someone happened to run a turn on
        # it. The figures carry their own reset instant, so a stored one
        # expires by itself instead of going stale.
        self._reported_path = state_dir / "usage-reported.json"
        try:
            stored = json.loads(self._reported_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        now = time.time()
        for agent, windows in stored.items() if isinstance(stored, dict) else []:
            if not isinstance(windows, dict):
                continue
            live = {
                name: entry
                for name, entry in windows.items()
                if isinstance(entry, dict)
                and isinstance(entry.get("utilization"), (int, float))
                and not (
                    isinstance(entry.get("resets_at"), (int, float))
                    and now >= entry["resets_at"]
                )
            }
            if live:
                self._reported[agent] = live

    def set_estimate(
        self,
        agent: str,
        window: str,
        utilization: Optional[float],
        resets_at: Optional[float] = None,
    ) -> None:
        """Records what the user says is already spent, and optionally when
        that window next resets.

        Without a reset time the figure is assumed to age out one window
        length after it was entered, which is only right if the window
        happened to start at that moment. The provider's windows reset at a
        fixed instant — a weekly one on a particular day — so when the user
        knows it, that instant is what the estimate expires at."""
        if agent not in AGENTS or window not in ("five_hour", "seven_day"):
            raise ValueError("unknown agent or window")
        if utilization is not None and not 0 <= utilization <= 1:
            raise ValueError("utilization must be between 0 and 1")
        now = time.time()
        if resets_at is not None:
            if resets_at <= now:
                raise ValueError("reset time must be in the future")
            seconds = FIVE_HOURS if window == "five_hour" else SEVEN_DAYS
            if resets_at > now + seconds:
                # A reset further away than the window is long cannot be this
                # window's reset, and would pin a stale figure for days.
                raise ValueError("reset time is further away than the window is long")
        windows = self._manual.setdefault(agent, {})
        if utilization is None:
            windows.pop(window, None)
            if not windows:
                self._manual.pop(agent, None)
        else:
            windows[window] = {
                "utilization": utilization,
                "at": now,
                "resets_at": resets_at,
            }
        self._manual_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._manual_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._manual), encoding="utf-8")
        temporary.replace(self._manual_path)

    def estimates(self) -> dict:
        """What the editor shows: the figure as entered plus the reset time it
        was entered against, per agent and window. Entries that have already
        expired are left out — the window has reset, so the number is gone."""
        now = time.time()
        return {
            agent: {
                window: {
                    "utilization": entry["utilization"],
                    "resets_at": entry.get("resets_at"),
                }
                for window, entry in windows.items()
                if not self._expired(window, entry, now)
            }
            for agent, windows in self._manual.items()
            if any(not self._expired(window, entry, now) for window, entry in windows.items())
        }

    @staticmethod
    def _expired(window: str, entry: dict, now: float) -> bool:
        resets_at = entry.get("resets_at")
        if resets_at is not None:
            return now >= resets_at
        seconds = FIVE_HOURS if window == "five_hour" else SEVEN_DAYS
        return now - entry["at"] >= seconds

    def record_tokens(self, agent: str, tokens: int, *, now: Optional[float] = None) -> None:
        if tokens <= 0:
            return
        now = time.time() if now is None else now
        samples = self._samples.setdefault(agent, [])
        samples.append(_Sample(at=now, tokens=tokens))
        self._prune(agent, now)

    def record_reported(self, agent: str, payload: Optional[dict]) -> None:
        """Takes the Claude CLI's rate_limit_event windows as-is."""
        if not payload:
            return
        windows = payload.get("unifiedWindows") or payload.get("windows") or {}
        if not isinstance(windows, dict):
            return
        parsed = {}
        for name, window in windows.items():
            if not isinstance(window, dict) or not isinstance(
                window.get("utilization"), (int, float)
            ):
                continue
            entry = {"utilization": float(window["utilization"])}
            resets_at = window.get("resetsAt") or window.get("resets_at")
            if isinstance(resets_at, (int, float)):
                entry["resets_at"] = float(resets_at)
            parsed[name] = entry
        self.record_reported_windows(agent, parsed)

    def record_reported_windows(self, agent: str, windows: Optional[dict]) -> None:
        """The same thing from a source that is already normalized — Codex's
        rollout log, which states the provider's numbers the CLI itself does
        not put on stdout (see packages/observability/codex_usage.py)."""
        if not windows:
            return
        store = self._reported.setdefault(agent, {})
        changed = False
        for name, window in windows.items():
            if isinstance(window, dict) and isinstance(
                window.get("utilization"), (int, float)
            ):
                store[name] = {
                    "utilization": float(window["utilization"]),
                    "resets_at": window.get("resets_at"),
                }
                changed = True
        if changed:
            self._persist_reported()

    def _persist_reported(self) -> None:
        """Survives a restart; never fails a turn. Written the same way as the
        manual estimates — temp file then replace — so a crash mid-write
        cannot leave a half-parsed file that the next start would discard."""
        try:
            self._reported_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._reported_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._reported), encoding="utf-8")
            temporary.replace(self._reported_path)
        except OSError:
            pass

    def _reported_window(self, agent: str, window: str, now: float) -> Optional[dict]:
        """The live reported entry for one window, or None once it has reset.

        Window names differ per provider: Claude says "seven_day", and ANA's
        Codex reader normalizes to the same two names, but older payloads used
        "weekly"."""
        store = self._reported.get(agent, {})
        entry = store.get(window)
        if entry is None and window == "seven_day":
            entry = store.get("weekly")
        if not isinstance(entry, dict):
            return None
        resets_at = entry.get("resets_at")
        if isinstance(resets_at, (int, float)) and now >= resets_at:
            return None
        return entry

    def _prune(self, agent: str, now: float) -> None:
        cutoff = now - SEVEN_DAYS
        self._samples[agent] = [s for s in self._samples.get(agent, []) if s.at >= cutoff]

    def _tokens_since(self, agent: str, seconds: int, now: float) -> int:
        cutoff = now - seconds
        return sum(s.tokens for s in self._samples.get(agent, []) if s.at >= cutoff)

    def snapshot(self, *, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        # Every CLI ANA can route to, whether or not it has been used yet: a
        # status bar that shows only the agents with data makes "nothing here"
        # and "nothing spent" look the same, and the row a user checks before
        # switching agents is exactly the one that would be missing.
        agents = sorted(set(AGENTS) | set(self._samples) | set(self._reported) | set(self._manual))
        result = {}
        for agent in agents:
            five_reported = self._reported_window(agent, "five_hour", now)
            week_reported = self._reported_window(agent, "seven_day", now)
            five = WindowUsage(
                label="5시간",
                key="five_hour",
                seconds=FIVE_HOURS,
                tokens=self._tokens_since(agent, FIVE_HOURS, now),
                budget=self._budget(agent, "five_hour"),
                reported=five_reported["utilization"] if five_reported else None,
                manual=self._manual_utilization(agent, "five_hour", now),
                resets_at=self._resets_at(agent, "five_hour", five_reported),
            )
            week = WindowUsage(
                label="7일",
                key="seven_day",
                seconds=SEVEN_DAYS,
                tokens=self._tokens_since(agent, SEVEN_DAYS, now),
                budget=self._budget(agent, "seven_day"),
                reported=week_reported["utilization"] if week_reported else None,
                manual=self._manual_utilization(agent, "seven_day", now),
                resets_at=self._resets_at(agent, "seven_day", week_reported),
            )
            result[agent] = {"windows": [five.as_dict(), week.as_dict()]}
        return result

    def _budget(self, agent: str, window: str) -> int:
        """This agent's token budget for a window: its own when configured,
        the global one otherwise."""
        five, weekly = self._agent_budgets.get(agent, (0, 0))
        if window == "five_hour":
            return five or self.five_hour_budget
        return weekly or self.weekly_budget

    def _resets_at(self, agent: str, window: str, reported: Optional[dict]) -> Optional[float]:
        """What the bar shows as the next reset: the provider's instant when
        there is one, otherwise the one the user typed with their estimate."""
        if reported and isinstance(reported.get("resets_at"), (int, float)):
            return float(reported["resets_at"])
        # The window rolled over and nobody has run that CLI since, so its
        # utilization is unknown again — but *when it next rolls over* is not:
        # windows are a fixed length, so the schedule continues from the last
        # instant the provider stated. Showing it keeps the bar useful in the
        # gap between a reset and the next turn.
        stale = self._reported.get(agent, {}).get(window) or {}
        stale_reset = stale.get("resets_at")
        if isinstance(stale_reset, (int, float)):
            length = FIVE_HOURS if window == "five_hour" else SEVEN_DAYS
            now = time.time()
            upcoming = float(stale_reset)
            while upcoming <= now:
                upcoming += length
            return upcoming
        entry = self._manual.get(agent, {}).get(window) or {}
        resets_at = entry.get("resets_at")
        return float(resets_at) if isinstance(resets_at, (int, float)) else None

    def _manual_utilization(self, agent: str, window: str, now: float) -> Optional[float]:
        entry = self._manual.get(agent, {}).get(window)
        if not entry:
            return None
        seconds = FIVE_HOURS if window == "five_hour" else SEVEN_DAYS
        if self._expired(window, entry, now):
            return None
        # The manually entered figure already includes everything spent before
        # entry. Only samples recorded afterward are added to the baseline.
        budget = self._budget(agent, window)
        later = sum(s.tokens for s in self._samples.get(agent, []) if s.at > entry["at"] and s.at >= now - seconds)
        return min(entry["utilization"] + (later / budget if budget > 0 else 0), 1.0)
