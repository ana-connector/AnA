"""Which models each CLI will accept, for the places that offer a choice.

The ladder editor used a text box, which meant knowing the exact spelling of a
model name and finding out about a typo one turn later, when the CLI rejected
it. So the names come from here instead.

Some of the CLIs can be asked. Kimi lists its models
(`kimi provider list --json`), Cursor lists its own (`agent models`), and
OpenCode lists dozens of them across several providers (`opencode models`), so
those entries are discovered and cached. Claude and Codex have no such command
-- `--model` takes a name and finds out at request time -- so their names are
written down, with the aliases first because that is what a person thinks in
("opus", not "claude-opus-5").

Whatever the current ladder already uses is merged in, so a name this file has
not heard of survives a round trip through the editor instead of being silently
replaced by the first option in a dropdown.
"""
import json
import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How long a discovered list is trusted. Long enough that the editor does not
# spawn a CLI per page load, short enough that adding a model shows up without
# a restart.
DISCOVERY_TTL_SECONDS = 300.0

# The Claude Code CLI takes an alias for the latest of a family or a full model
# id (`claude --help`: "Provide an alias for the latest model (e.g. 'fable',
# 'opus', or 'sonnet') or a model's full name (e.g. 'claude-fable-5')").
CLAUDE_MODELS = (
    "fable",
    "opus",
    "sonnet",
    "haiku",
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
)

# Codex names its models in its own config (`model = "gpt-6-sol"`), and has no
# command that lists them.
CODEX_MODELS = ("gpt-6-astra", "gpt-6-sol")

# Used when the CLI cannot be asked: the models this project has actually run.
KIMI_FALLBACK = ("kimi-code/k3", "kimi-code/k3-256k", "kimi-code/kimi-for-coding")

# Cursor Agent (`agent models`). "auto" is the account default; the rest are
# common ids observed on a logged-in session — discovery replaces this when
# the CLI answers.
CURSOR_FALLBACK = (
    "auto",
    "composer-2.5",
    "gpt-5.2",
    "claude-sonnet-5-thinking-high",
    "claude-opus-5-thinking-high",
)

# OpenCode (`opencode models`) is one CLI in front of many providers, and the
# list is dozens long and account-dependent, so discovery is the real source
# and this is only what a machine without the CLI should still be offered: the
# free rung the default ladder lands on, plus one model per provider seen on a
# logged-in session. Names are `provider/model`, and the model half may itself
# contain slashes (`omniroute/tapie/claude-opus-5`).
OPENCODE_FALLBACK = (
    "opencode/space-bunny-free",
    "opencode/claude-sonnet-5-5",
    "opencode/gpt-6-astra",
    "opencode/gemini-3.1-pro",
    "opencode-go/kimi-k3",
)

_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def _kimi_executable() -> str:
    from packages.adapters.kimi import default_executable

    return default_executable()


def discover_kimi_models() -> tuple[str, ...]:
    """Kimi's own list, or the fallback when the CLI is not reachable."""
    cached = _cache.get("kimi")
    if cached and time.monotonic() - cached[0] < DISCOVERY_TTL_SECONDS:
        return cached[1]
    names: tuple[str, ...] = KIMI_FALLBACK
    try:
        result = subprocess.run(
            [_kimi_executable(), "provider", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("ANA_MODEL_DISCOVERY_TIMEOUT", 10)),
        )
        if result.returncode == 0:
            models = (json.loads(result.stdout) or {}).get("models") or {}
            found = tuple(sorted(str(name) for name in models))
            if found:
                names = found
    except Exception:  # noqa: BLE001 - a CLI that cannot be asked is not an error
        logger.info("could not list kimi models; using the built-in list", exc_info=True)
    _cache["kimi"] = (time.monotonic(), names)
    return names


def _cursor_executable() -> str:
    from packages.adapters.cursor import default_executable

    return default_executable()


def discover_cursor_models() -> tuple[str, ...]:
    """Cursor Agent's `agent models` list, or the fallback when unreachable."""
    cached = _cache.get("cursor")
    if cached and time.monotonic() - cached[0] < DISCOVERY_TTL_SECONDS:
        return cached[1]
    names: tuple[str, ...] = CURSOR_FALLBACK
    try:
        result = subprocess.run(
            [_cursor_executable(), "models"],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("ANA_MODEL_DISCOVERY_TIMEOUT", 10)),
        )
        if result.returncode == 0:
            found: list[str] = []
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if not line or line.lower().startswith("available"):
                    continue
                # "composer-2.5 - Composer 2.5" → id before the dash.
                model_id = line.split(" - ", 1)[0].strip()
                if model_id and model_id not in found:
                    found.append(model_id)
            if found:
                names = tuple(found)
    except Exception:  # noqa: BLE001 - a CLI that cannot be asked is not an error
        logger.info("could not list cursor models; using the built-in list", exc_info=True)
    _cache["cursor"] = (time.monotonic(), names)
    return names


def _opencode_executable() -> str:
    from packages.adapters.opencode import default_executable

    return default_executable()


def discover_opencode_models() -> tuple[str, ...]:
    """OpenCode's own list (`opencode models`), or the fallback.

    One `provider/model` per line, which is exactly the spelling `--model`
    takes, so the lines are used as they are. The order the CLI prints is kept:
    it groups by provider, which is what makes a list this long readable in a
    picker."""
    cached = _cache.get("opencode")
    if cached and time.monotonic() - cached[0] < DISCOVERY_TTL_SECONDS:
        return cached[1]
    names: tuple[str, ...] = OPENCODE_FALLBACK
    try:
        result = subprocess.run(
            [_opencode_executable(), "models"],
            capture_output=True,
            text=True,
            # Longer than the other two: this one may refresh its model
            # catalog from models.dev before it answers.
            timeout=float(os.environ.get("ANA_MODEL_DISCOVERY_TIMEOUT", 10)) * 2,
        )
        if result.returncode == 0:
            found: list[str] = []
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                # A model line is `provider/model`; anything without a slash is
                # a header or a warning, not a name --model would accept.
                if not line or "/" not in line or line.startswith(("-", "!", "~")):
                    continue
                if line not in found:
                    found.append(line)
            if found:
                names = tuple(found)
    except Exception:  # noqa: BLE001 - a CLI that cannot be asked is not an error
        logger.info("could not list opencode models; using the built-in list", exc_info=True)
    _cache["opencode"] = (time.monotonic(), names)
    return names


def model_catalog(used: Optional[dict[str, list[str]]] = None) -> dict[str, list[str]]:
    """Model names per agent, in the order a picker should show them.

    `used` is what the current rules already name, per agent: those go first,
    because a name in use is the one most likely to be picked again and must
    not disappear from the list just because this file has not heard of it."""
    catalog = {
        "claude": list(CLAUDE_MODELS),
        # The same CLI, so the same models; the account is what differs.
        "tapie": list(CLAUDE_MODELS),
        "codex": list(CODEX_MODELS),
        "kimi": list(discover_kimi_models()),
        "cursor": list(discover_cursor_models()),
        "opencode": list(discover_opencode_models()),
    }
    for agent, names in (used or {}).items():
        known = catalog.setdefault(agent, [])
        for name in names:
            if name and name not in known:
                known.insert(0, name)
    return catalog
