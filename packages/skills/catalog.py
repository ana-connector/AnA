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

# English, like every other block ANA writes into a system prompt: it is paid
# for on every turn and read only by a model.
HEADER = """\
[AVAILABLE SKILLS]
ANA skills you can use for this task. If the task is one of these, follow that
skill's procedure. If none applies, work as you normally would. Do not try to
use a skill or an MCP server that is not listed (the gate denies it).
"""

LEAD_HEADER = """\
[SKILLS YOUR SUB-AGENTS CAN USE]
ANA skills the sub-agents you start are able to carry out. When you split the
work, name the matching skill in the task. Do not carry one out yourself.
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
