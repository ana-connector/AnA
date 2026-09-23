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
            "source": "reported" if self.reported is not None else ("manual" if self.manual is not None else "estimated"),
        }


class UsageTracker:
    """Per-agent token history plus whatever the provider reported.

    In memory on purpose: it answers "how close am I right now", and a restart
    losing a few hours of samples is a smaller problem than a schema for it."""

    def __init__(self, five_hour_budget: Optional[int] = None, weekly_budget: Optional[int] = None):
        self.five_hour_budget = five_hour_budget or int(
            os.environ.get("ANA_FIVE_HOUR_TOKEN_BUDGET", 2_000_000)
        )
        self.weekly_budget = weekly_budget or int(
            os.environ.get("ANA_WEEKLY_TOKEN_BUDGET", 20_000_000)
        )
        self._samples: dict[str, list[_Sample]] = {}
        self._reported: dict[str, dict[str, float]] = {}
        self._manual: dict[str, dict[str, dict[str, float]]] = {}
        self._manual_path = Path(os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana"))) / "usage-estimates.json"
        try:
            self._manual = json.loads(self._manual_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

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
        if agent not in ("claude", "codex") or window not in ("five_hour", "seven_day"):
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
        store = self._reported.setdefault(agent, {})
        for name, window in windows.items():
            if isinstance(window, dict) and isinstance(
                window.get("utilization"), (int, float)
            ):
                store[name] = float(window["utilization"])

    def _prune(self, agent: str, now: float) -> None:
        cutoff = now - SEVEN_DAYS
        self._samples[agent] = [s for s in self._samples.get(agent, []) if s.at >= cutoff]

    def _tokens_since(self, agent: str, seconds: int, now: float) -> int:
        cutoff = now - seconds
        return sum(s.tokens for s in self._samples.get(agent, []) if s.at >= cutoff)

    def snapshot(self, *, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        agents = sorted(set(self._samples) | set(self._reported) | set(self._manual))
        result = {}
        for agent in agents:
            reported = self._reported.get(agent, {})
            five = WindowUsage(
                label="5시간",
                key="five_hour",
                seconds=FIVE_HOURS,
                tokens=self._tokens_since(agent, FIVE_HOURS, now),
                budget=self.five_hour_budget,
                reported=reported.get("five_hour"),
                manual=self._manual_utilization(agent, "five_hour", now),
            )
            week = WindowUsage(
                label="7일",
                key="seven_day",
                seconds=SEVEN_DAYS,
                tokens=self._tokens_since(agent, SEVEN_DAYS, now),
                budget=self.weekly_budget,
                reported=reported.get("seven_day") or reported.get("weekly"),
                manual=self._manual_utilization(agent, "seven_day", now),
            )
            result[agent] = {"windows": [five.as_dict(), week.as_dict()]}
        return result

    def _manual_utilization(self, agent: str, window: str, now: float) -> Optional[float]:
        entry = self._manual.get(agent, {}).get(window)
        if not entry:
            return None
        seconds = FIVE_HOURS if window == "five_hour" else SEVEN_DAYS
        if self._expired(window, entry, now):
            return None
        # The manually entered figure already includes everything spent before
        # entry. Only samples recorded afterward are added to the baseline.
        budget = self.five_hour_budget if window == "five_hour" else self.weekly_budget
        later = sum(s.tokens for s in self._samples.get(agent, []) if s.at > entry["at"] and s.at >= now - seconds)
        return min(entry["utilization"] + (later / budget if budget > 0 else 0), 1.0)
