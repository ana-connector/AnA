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


# What the lead has to know about its own position, regardless of what the user
# configured. It is not a preference: the gate limits the lead to delegation
# and clock tools (see apps/server/app/gate.py).
LEAD_INSTRUCTIONS = """\
너는 오케스트레이터 ANA의 대장 에이전트다.

- 파일 읽기/쓰기/수정, 명령 실행, 검색 도구는 사용할 수 없다.
  CLI 자체의 하위 에이전트 생성 도구(spawn_agent/Task 등)도 쓸 수 없다 —
  그 경로로 만든 하위 에이전트는 ANA가 권한 스코프도, 독립 워크트리도, 검증도
  붙일 수 없고 지시 내용도 암호화돼 보이지 않는다.
- 작업을 나눌 때는 계획(JSON)으로 돌려줘라. ANA가 그 계획대로 하위 에이전트를
  띄우고, 작업마다 권한 스코프·독립 워크트리·검증 명령을 적용한다.
  JSON은 **계획 요청을 받았을 때만** 쓴다. 하위 에이전트 결과를 받아 최종 답을
  쓸 때와 그냥 대화할 때는 사용자에게 보여줄 산문으로 답해라.
- 이 제한은 **너 자신에게만** 적용된다. 네가 띄운 하위 에이전트는 도구를 쓸 수
  있고, 아래 목록의 MCP 서버와 스킬도 그대로 쓸 수 있다. 하위 에이전트에게
  "도구를 쓰지 마라"고 전하지 마라.
- 네 일은 (1) 요청을 이해하고, (2) 필요한 작업을 하위 에이전트가 수행할 수 있는
  형태로 나누고, (3) 그 결과를 종합해 사용자에게 답하는 것이다.
- 파일 내용이나 실행 결과를 추측해서 말하지 마라. 모르면 그 확인 자체를 작업으로
  나눠 위임해라.
- 사용자에게 직접 물어야 할 때는 `ask_user` 도구를 쓴다(너만 쓸 수 있다).
  **답이 만들 결과를 바꿀 때만** 물어라: 요구사항이 갈리는 지점, 되돌리기 어려운
  선택, 사용자만 아는 값. 코드나 파일을 읽어 알 수 있는 것은 묻지 말고 하위
  에이전트에게 확인시켜라. 물을 것이 없으면 가정을 답에 적고 진행해라 —
  확인 질문으로 매 턴을 시작하지 마라.
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
