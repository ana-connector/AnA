"""Editable settings: the routing rules and the system prompt.

Both are things the user should be able to change without touching the repo or
restarting the server, so they live as files ANA owns under its state directory
and are read fresh on use.

The routing rules are YAML because they already were — the file the UI edits is
the same one `packages/orchestrator/routing.py` loads, so what the editor shows
is what the router runs.
"""
import os
from pathlib import Path
from typing import Optional

import yaml

from packages.adapters.env_builder import default_mode_from_settings
from packages.orchestrator.routing import DEFAULT_RULES_PATH, ModelLadder

SYSTEM_PROMPT_FILENAME = "system_prompt.txt"
ROUTING_FILENAME = "routing.yaml"
AUTH_MODE_FILENAME = "claude_auth_mode.txt"
AUTH_MODES = ("pro", "api")


def state_dir() -> Path:
    path = Path(os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def routing_path() -> Path:
    """Where the *editable* routing rules live.

    ANA_ROUTING_RULES wins when set (that is what routing.py reads); otherwise
    the copy under the state directory is used, seeded from the repo's default
    on first read so the editor never starts empty."""
    configured = os.environ.get("ANA_ROUTING_RULES")
    if configured:
        return Path(configured)
    path = state_dir() / ROUTING_FILENAME
    if not path.exists() and DEFAULT_RULES_PATH.exists():
        path.write_text(DEFAULT_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    elif path.exists() and DEFAULT_RULES_PATH.exists():
        # Older ANA versions stored keyword/sticky rules at the same path.
        # The ladder would silently read them as an empty lead list and route
        # every turn to Claude. Keep a copy before moving to the new schema.
        try:
            current = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            current = None
        if isinstance(current, dict) and "lead" not in current and (
            "default_agent" in current or "rules" in current
        ):
            backup = path.with_name("routing.legacy.yaml")
            if not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            path.write_text(DEFAULT_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def read_routing() -> str:
    path = routing_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return DEFAULT_RULES_PATH.read_text(encoding="utf-8")


def write_routing(text: str) -> None:
    """Validated before it is stored: a rule file that fails to parse would
    otherwise take routing down on the next message, and the editor is exactly
    where a typo happens."""
    parsed = yaml.safe_load(text)
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError("routing rules must be a YAML mapping")
    # Round-trips through the real loader, so "it parsed" means "the router can
    # use it", not just "it was YAML".
    ModelLadder.from_yaml(_write_temp(text))
    routing_path().write_text(text, encoding="utf-8")


# What the settings UI writes above the rules it generates. The hand-written
# file this replaces carried a long explanation; a generated one cannot keep
# comments, so it says where it came from and what a rung means instead.
ROUTING_HEADER = """\
# Which model answers, by how much of its usage window is already spent.
#
# Written by the settings UI. Each rung names a CLI and a model; `until` is the
# utilization at which ANA leaves that rung for the next one. The last rung has
# no `until` -- it is where everything lands once the cheaper ones are spent.
# Editing takes effect on the next turn.
"""


def read_routing_structured() -> dict:
    """The ladder as data rather than text, for an editor that is not a text
    box. Unknown keys in the file are dropped, which is the same thing the
    router itself does with them."""
    raw = yaml.safe_load(read_routing()) or {}
    if not isinstance(raw, dict):
        raise ValueError("routing rules must be a YAML mapping")
    return {
        "lead": _rung_dicts(raw.get("lead")),
        "workers": {
            str(name): _rung_dicts(rungs)
            for name, rungs in (raw.get("workers") or {}).items()
        },
    }


def write_routing_structured(data: dict) -> None:
    """Serializes the ladder and stores it through the same validation the
    text editor uses, so a form cannot write a file the router rejects."""
    document: dict = {"lead": _rung_yaml(data.get("lead"))}
    workers = {
        str(name): _rung_yaml(rungs)
        for name, rungs in (data.get("workers") or {}).items()
    }
    if workers:
        document["workers"] = workers
    body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    write_routing(ROUTING_HEADER + "\n" + body)


def _rung_dicts(raw) -> list[dict]:
    rungs = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not entry.get("agent"):
            continue
        until = entry.get("until") if isinstance(entry.get("until"), dict) else {}
        rungs.append(
            {
                "agent": str(entry["agent"]),
                "model": entry.get("model") or "",
                "effort": entry.get("effort") or "",
                "window": until.get("window") or "",
                # None rather than 0: "no limit" and "leave immediately" are
                # different rungs, and 0 is a real threshold.
                "utilization": until.get("utilization"),
            }
        )
    return rungs


def _rung_yaml(rungs) -> list[dict]:
    out = []
    for rung in rungs or []:
        if not isinstance(rung, dict) or not rung.get("agent"):
            continue
        entry: dict = {"agent": str(rung["agent"])}
        if rung.get("model"):
            entry["model"] = str(rung["model"])
        if rung.get("effort"):
            entry["effort"] = str(rung["effort"])
        window = rung.get("window")
        utilization = rung.get("utilization")
        if window and isinstance(utilization, (int, float)):
            entry["until"] = {"window": str(window), "utilization": float(utilization)}
        out.append(entry)
    if not out:
        raise ValueError("a ladder needs at least one rung")
    return out


def _write_temp(text: str) -> Path:
    path = state_dir() / ".routing-check.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def auth_mode_path() -> Path:
    return state_dir() / AUTH_MODE_FILENAME


def read_auth_mode() -> str:
    """Which `~/.bashrc` billing mode the Claude CLI runs under —
    `use-claude-pro` (subscription) or `use-claude-api` (key + base URL).

    The roadmap put this in the UI as part of stage 3's secrets work ("웹 UI에서
    세션별로 override ... 정식 시크릿 관리 UI에 합쳐서 그때 함께 구현한다");
    until this existed, changing mode meant editing `.env` and restarting,
    which is precisely what that note said would stop being true. The file
    wins over the environment so a UI change outlives a container restart."""
    path = auth_mode_path()
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored in AUTH_MODES:
            return stored
    return default_mode_from_settings()


def write_auth_mode(mode: str) -> None:
    if mode not in AUTH_MODES:
        raise ValueError(f"unknown auth mode: {mode!r}")
    auth_mode_path().write_text(mode, encoding="utf-8")


def system_prompt_path() -> Path:
    return state_dir() / SYSTEM_PROMPT_FILENAME


def read_system_prompt() -> str:
    """Extra instructions appended to every agent's own system prompt. Empty by
    default: ANA should not put words in the agent's mouth unless asked."""
    path = system_prompt_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_system_prompt(text: str) -> None:
    system_prompt_path().write_text(text or "", encoding="utf-8")


LEAD_INSTRUCTIONS = """\
You are the lead agent of the ANA orchestrator.

- Complete ordinary requests yourself with the available tools. You can read,
  edit, run tests and commit when the user asks. The gate applies its normal
  rules and any explicit scope set for a planned turn.
- Use your CLI's sub-agent tools when independent work benefits from parallel
  execution. Keep simple work in this turn and coordinate delegated results.
- Finish authorized work before replying. Do not return a task proposal or
  claim that tools are unavailable when they are available in this turn.
- Output plan JSON only when ANA explicitly asks for a plan. Ordinary requests
  receive a normal answer after the work is done.
- **Write everything the user reads in Korean**: the final answer, task titles,
  and any question you ask. Everything a model reads — task instructions — in
  English, which is what the plan format asks for.
- Handle straightforward work yourself. ANA's optional planner is available
  for tasks that need isolated worktrees and explicit permission scopes.
- Never guess at file contents or command output. Check with tools.
- Use the `ask_user` tool when you need the user themselves (only you can).
  Ask **only when the answer changes what gets built**: a fork in the
  requirements, a choice that is hard to undo, a value only they know. Do not
  ask what you could find out with tools — check it. If
  there is nothing to ask, state your assumption and carry on; do not open every
  turn with a clarifying question. Write the questions in Korean.
- When you synthesize, do not hide a failed task: say what failed and why.
"""

WORKER_INSTRUCTIONS = """\
You are an ANA sub-agent. Carry out the single task you were given, all the way,
and report the result briefly and truthfully. Do not change anything outside the
scope of that task. Report in English — the lead writes the user's answer.
"""


def system_prompt_for(agent: str, role: str = "lead") -> Optional[str]:
    """The prompt as each CLI takes it.

    Claude has a flag for exactly this (`--append-system-prompt`). Codex has no
    equivalent for a one-shot run, so the caller prepends it to the prompt
    instead — same words, different delivery.

    The role instructions come first and the user's configured prompt after:
    what the lead may do is a fact about this system, not a preference to be
    overridden by accident."""
    parts = [LEAD_INSTRUCTIONS if role == "lead" else WORKER_INSTRUCTIONS]
    configured = read_system_prompt().strip()
    if configured:
        parts.append(configured)
    return "\n\n".join(parts)


def apply_workspace_prompt(adapter, prompt: str) -> None:
    """Refresh the workspace instructions on an adapter reused across turns."""
    if not hasattr(adapter, "system_prompt"):
        return
    if not hasattr(adapter, "_ana_base_system_prompt"):
        adapter._ana_base_system_prompt = adapter.system_prompt
    base = adapter._ana_base_system_prompt or ""
    extra = prompt.strip()
    adapter.system_prompt = f"{base}\n\n[WORKSPACE INSTRUCTIONS]\n{extra}" if extra else base
