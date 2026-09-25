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
from datetime import datetime, timezone
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
# A calendar month, for pruning and for the "how long is this window" figure.
# The *reset* is computed from the calendar rather than from this number: a
# monthly cap rolls over on the 1st, not 31 days after the last turn.
ONE_MONTH = 31 * 24 * 60 * 60


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def month_reset(now: float) -> float:
    """The start of next month, UTC — when a monthly cap rolls over."""
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def month_start(now: float) -> float:
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    return datetime(moment.year, moment.month, 1, tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class WindowSpec:
    """One limit an agent runs against.

    Not every CLI is billed the same way, so the windows are per agent rather
    than the same two for everyone. TAPIE is a key against an endpoint with a
    dollar cap -- $50 rolling over five hours, $1500 a month -- and Kimi's
    second window is the calendar month, not seven days. Reading either of
    those as "7일, in tokens" made the bar show a number that meant nothing."""

    key: str
    label: str
    seconds: int
    # "tokens" or "usd": what the budget counts, and therefore what a
    # utilization is the ratio of.
    unit: str = "tokens"


FIVE_HOUR = WindowSpec("five_hour", "5시간", FIVE_HOURS)
SEVEN_DAY = WindowSpec("seven_day", "7일", SEVEN_DAYS)
MONTHLY_TOKENS = WindowSpec("monthly", "이번 달", ONE_MONTH)
FIVE_HOUR_USD = WindowSpec("five_hour", "5시간", FIVE_HOURS, unit="usd")
MONTHLY_USD = WindowSpec("monthly", "이번 달", ONE_MONTH, unit="usd")

# Which windows each CLI actually has.
AGENT_WINDOWS: dict[str, tuple[WindowSpec, ...]] = {
    "claude": (FIVE_HOUR, SEVEN_DAY),
    "codex": (FIVE_HOUR, SEVEN_DAY),
    # Kimi's plan is monthly; it reports no utilization anywhere, so this bar
    # is entirely ANA's own count against the budget below.
    "kimi": (FIVE_HOUR, MONTHLY_TOKENS),
    # TAPIE bills in dollars, and the CLI reports the cost of every turn.
    "tapie": (FIVE_HOUR_USD, MONTHLY_USD),
}
DEFAULT_WINDOWS = (FIVE_HOUR, SEVEN_DAY)


def windows_for(agent: str) -> tuple[WindowSpec, ...]:
    return AGENT_WINDOWS.get(agent, DEFAULT_WINDOWS)


@dataclass
class _Sample:
    at: float
    tokens: int
    # What that work cost, when the CLI said. Only the dollar-capped agents
    # have a use for it, but every sample carries it rather than keeping two
    # histories that can disagree about when something happened.
    usd: float = 0.0


@dataclass
class WindowUsage:
    label: str
    # Stable name the routing ladder matches on ("five_hour", "seven_day",
    # "monthly"), separate from the human label the status bar shows.
    key: str
    seconds: int
    tokens: int = 0
    budget: float = 0
    # "tokens" or "usd": what `spent` and `budget` are counted in.
    unit: str = "tokens"
    # Dollars spent in this window, for the agents billed that way.
    usd: float = 0.0
    # Provider-reported utilization (0..1) when the CLI gave one.
    reported: Optional[float] = None
    manual: Optional[float] = None
    # When this window next resets, as the provider stated it (or as the user
    # did, for a manual figure). None means nobody said.
    resets_at: Optional[float] = None

    @property
    def spent(self) -> float:
        """What this window counts: dollars for a dollar cap, tokens else."""
        return self.usd if self.unit == "usd" else self.tokens

    @property
    def utilization(self) -> Optional[float]:
        if self.reported is not None:
            return self.reported
        if self.manual is not None:
            return self.manual
        if self.budget <= 0:
            return None
        return min(self.spent / self.budget, 1.0)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "key": self.key,
            "seconds": self.seconds,
            "tokens": self.tokens,
            "budget": self.budget,
            "unit": self.unit,
            "usd": round(self.usd, 4),
            "spent": self.spent,
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
        # The dollar caps, for the agents billed in dollars. TAPIE's plan is
        # $50 rolling over five hours and $1500 a month; both are overridable
        # because a plan is a thing that changes without the code changing.
        self._usd_budgets = {
            ("tapie", "five_hour"): _env_float("ANA_TAPIE_FIVE_HOUR_USD_BUDGET", 50.0),
            ("tapie", "monthly"): _env_float("ANA_TAPIE_MONTHLY_USD_BUDGET", 1500.0),
        }
        # Kimi's month, in tokens: nothing reports it, so this is the bar.
        self._monthly_token_budgets = {
            agent: _env_int(f"ANA_{agent.upper()}_MONTHLY_TOKEN_BUDGET", 0)
            for agent in AGENTS
        }
        self.monthly_budget = _env_int("ANA_MONTHLY_TOKEN_BUDGET", 80_000_000)
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
        # The windows an agent has are its own: TAPIE and Kimi have a month
        # where Claude and Codex have a week.
        if agent not in AGENTS or window not in {spec.key for spec in windows_for(agent)}:
            raise ValueError("unknown agent or window")
        if utilization is not None and not 0 <= utilization <= 1:
            raise ValueError("utilization must be between 0 and 1")
        now = time.time()
        if resets_at is not None:
            if resets_at <= now:
                raise ValueError("reset time must be in the future")
            seconds = next(
                spec.seconds for spec in windows_for(agent) if spec.key == window
            )
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
        seconds = {"five_hour": FIVE_HOURS, "monthly": ONE_MONTH}.get(window, SEVEN_DAYS)
        return now - entry["at"] >= seconds

    def record_tokens(
        self,
        agent: str,
        tokens: int,
        *,
        usd: float = 0.0,
        now: Optional[float] = None,
    ) -> None:
        """What a turn spent. `usd` is what the CLI said it cost, which is the
        only thing a dollar-capped plan can be measured against."""
        if tokens <= 0 and usd <= 0:
            return
        now = time.time() if now is None else now
        samples = self._samples.setdefault(agent, [])
        samples.append(_Sample(at=now, tokens=max(tokens, 0), usd=max(usd, 0.0)))
        self._prune(agent, now)

    def record_cost(self, agent: str, usd: float, *, now: Optional[float] = None) -> None:
        """Cost with no token count attached, for a CLI that reports one and
        not the other."""
        self.record_tokens(agent, 0, usd=usd, now=now)

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
        # The longest window any agent has, so a monthly cap keeps its history.
        cutoff = min(now - ONE_MONTH, month_start(now))
        self._samples[agent] = [s for s in self._samples.get(agent, []) if s.at >= cutoff]

    def _window_start(self, spec: "WindowSpec", now: float) -> float:
        """When the window being measured began. A monthly cap starts on the
        1st; the rolling ones start a fixed span ago."""
        return month_start(now) if spec.key == "monthly" else now - spec.seconds

    def _tokens_since(self, agent: str, since: float, now: float) -> int:
        return sum(s.tokens for s in self._samples.get(agent, []) if s.at >= since)

    def _usd_since(self, agent: str, since: float, now: float) -> float:
        return sum(s.usd for s in self._samples.get(agent, []) if s.at >= since)

    def snapshot(self, *, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        # Every CLI ANA can route to, whether or not it has been used yet: a
        # status bar that shows only the agents with data makes "nothing here"
        # and "nothing spent" look the same, and the row a user checks before
        # switching agents is exactly the one that would be missing.
        agents = sorted(set(AGENTS) | set(self._samples) | set(self._reported) | set(self._manual))
        result = {}
        for agent in agents:
            windows = []
            for spec in windows_for(agent):
                reported = self._reported_window(agent, spec.key, now)
                since = self._window_start(spec, now)
                windows.append(
                    WindowUsage(
                        label=spec.label,
                        key=spec.key,
                        seconds=spec.seconds,
                        unit=spec.unit,
                        tokens=self._tokens_since(agent, since, now),
                        usd=self._usd_since(agent, since, now),
                        budget=self._budget(agent, spec),
                        reported=reported["utilization"] if reported else None,
                        manual=self._manual_utilization(agent, spec, now),
                        resets_at=self._resets_at(agent, spec, reported, now),
                    ).as_dict()
                )
            result[agent] = {"windows": windows}
        return result

    def _budget(self, agent: str, spec: "WindowSpec") -> float:
        """What this window is measured against, in its own unit."""
        if spec.unit == "usd":
            return self._usd_budgets.get((agent, spec.key), 0.0)
        five, weekly = self._agent_budgets.get(agent, (0, 0))
        if spec.key == "five_hour":
            return five or self.five_hour_budget
        if spec.key == "monthly":
            return self._monthly_token_budgets.get(agent, 0) or self.monthly_budget
        return weekly or self.weekly_budget

    def _resets_at(
        self,
        agent: str,
        spec: "WindowSpec",
        reported: Optional[dict],
        now: Optional[float] = None,
    ) -> Optional[float]:
        """What the bar shows as the next reset: the provider's instant when
        there is one, otherwise the one the user typed with their estimate."""
        now = time.time() if now is None else now
        window = spec.key
        if reported and isinstance(reported.get("resets_at"), (int, float)):
            return float(reported["resets_at"])
        # A monthly cap does not need anybody to report it: it rolls over on
        # the 1st whether or not a turn has run.
        if window == "monthly":
            return month_reset(now)
        # The window rolled over and nobody has run that CLI since, so its
        # utilization is unknown again — but *when it next rolls over* is not:
        # windows are a fixed length, so the schedule continues from the last
        # instant the provider stated. Showing it keeps the bar useful in the
        # gap between a reset and the next turn.
        stale = self._reported.get(agent, {}).get(window) or {}
        stale_reset = stale.get("resets_at")
        if isinstance(stale_reset, (int, float)):
            length = spec.seconds
            upcoming = float(stale_reset)
            while upcoming <= now:
                upcoming += length
            return upcoming
        entry = self._manual.get(agent, {}).get(window) or {}
        resets_at = entry.get("resets_at")
        return float(resets_at) if isinstance(resets_at, (int, float)) else None

    def _manual_utilization(self, agent: str, spec: "WindowSpec", now: float) -> Optional[float]:
        entry = self._manual.get(agent, {}).get(spec.key)
        if not entry:
            return None
        if self._expired(spec.key, entry, now):
            return None
        # The manually entered figure already includes everything spent before
        # entry. Only samples recorded afterward are added to the baseline.
        budget = self._budget(agent, spec)
        since = max(entry["at"], self._window_start(spec, now))
        later = (
            self._usd_since(agent, since, now)
            if spec.unit == "usd"
            else self._tokens_since(agent, since, now)
        )
        return min(entry["utilization"] + (later / budget if budget > 0 else 0), 1.0)
