"""The catalog an agent sees.

Skills reach the agent the way Claude Code's own do: as a short list in the
system prompt, with the full instructions of one skill pulled in only when it
is used. The list is deliberately terse — it is paid for on every turn, so it
carries names and one-line descriptions, not bodies.

Called from the adapter factories, appended after the role instructions from
packages/orchestrator/settings.py so the two stay independent: a change to
either does not touch the other's file.
"""
from typing import Optional

from packages.mcp.registry import LEAD, WORKER
from packages.skills.loader import load_library

HEADER = """\
[사용 가능한 스킬]
아래는 이 작업에 쓸 수 있는 ANA 스킬 목록이다. 작업이 여기 해당하면 그 스킬의
절차를 따라라. 해당하는 스킬이 없으면 평소대로 진행하면 된다. 목록에 없는
스킬이나 MCP 서버를 쓰려고 하지 마라(게이트에서 거부된다).
"""

LEAD_HEADER = """\
[하위 에이전트가 쓸 수 있는 스킬]
아래는 네가 띄우는 하위 에이전트가 수행할 수 있는 ANA 스킬 목록이다. 작업을
나눌 때 해당하는 스킬이 있으면 그 이름을 지시에 적어라. 너 자신이 직접
실행하지는 마라.
"""


def catalog_text(role: str, available_servers: Optional[set[str]] = None) -> str:
    """The catalog block for one role, or "" when nothing applies.

    The lead gets the *worker* catalog under its own heading: the CLI's native
    sub-agents run inside the lead's process and inherit its system prompt, so
    a catalog withheld from the lead is a catalog no sub-agent ever sees —
    the same reason the lead's process carries the worker MCP servers
    (packages/mcp/wiring.py).

    Empty rather than a header with no entries: an agent told it has a skill
    list and then shown none tends to explain that it has none, which costs
    tokens and helps nobody."""
    library = load_library()
    is_lead = role == LEAD
    skills = library.for_role(WORKER if is_lead else role, available_servers)
    if not skills:
        return ""
    header = LEAD_HEADER if is_lead else HEADER
    return header + "\n".join(skill.summary() for skill in skills)


def with_catalog(
    system_prompt: Optional[str], role: str, available_servers: Optional[set[str]] = None
) -> Optional[str]:
    """Appends the catalog to whatever system prompt the caller already built."""
    catalog = catalog_text(role, available_servers)
    if not catalog:
        return system_prompt
    return f"{system_prompt}\n\n{catalog}" if system_prompt else catalog


def render_skill(name: str, inputs: Optional[dict] = None) -> str:
    """The full instructions of one skill, inputs validated and substituted.

    Raises KeyError for an unknown skill and pydantic.ValidationError for
    inputs that do not fit its schema."""
    skill = load_library().get(name)
    if skill is None:
        raise KeyError(name)
    return skill.render(inputs)
