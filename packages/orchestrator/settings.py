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

from packages.orchestrator.routing import DEFAULT_RULES_PATH, ModelLadder

SYSTEM_PROMPT_FILENAME = "system_prompt.txt"
ROUTING_FILENAME = "routing.yaml"


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


def _write_temp(text: str) -> Path:
    path = state_dir() / ".routing-check.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def system_prompt_path() -> Path:
    return state_dir() / SYSTEM_PROMPT_FILENAME


def read_system_prompt() -> str:
    """Extra instructions appended to every agent's own system prompt. Empty by
    default: ANA should not put words in the agent's mouth unless asked."""
    path = system_prompt_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_system_prompt(text: str) -> None:
    system_prompt_path().write_text(text or "", encoding="utf-8")


# What the lead has to know about its own position, regardless of what the user
# configured. It is not a preference: the gate limits the lead to delegation
# and clock tools (see apps/server/app/gate.py).
LEAD_INSTRUCTIONS = """\
너는 오케스트레이터 ANA의 대장 에이전트다.

- 파일 읽기/쓰기/수정, 명령 실행, 검색 도구는 사용할 수 없다.
  이런 작업은 하위 에이전트에게 위임해라. 하위 에이전트 위임/관리 도구와
  현재 시각 확인 도구는 사용할 수 있다.
- 네 일은 (1) 요청을 이해하고, (2) 필요한 작업을 하위 에이전트가 수행할 수 있는
  형태로 나누고, (3) 그 결과를 종합해 사용자에게 답하는 것이다.
- 파일 내용이나 실행 결과를 추측해서 말하지 마라. 모르면 그 확인 자체를 작업으로
  나눠 위임해라.
- 종합할 때는 실패한 작업을 숨기지 말고 무엇이 왜 안 됐는지 밝혀라.
"""

WORKER_INSTRUCTIONS = """\
너는 ANA의 하위 에이전트다. 주어진 한 가지 작업만 끝까지 수행하고, 결과를 짧고
사실대로 보고해라. 작업 범위를 넘어서는 변경은 하지 마라.
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
