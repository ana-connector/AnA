"""Context handoff between the agent CLIs.

Each CLI keeps its own native session (claude --resume / codex exec resume /
kimi --session),
so switching agents mid-thread means the incoming CLI knows nothing about the
turns the other one just ran. This module builds the brief that closes that
gap: the thread's own recent turns, attributed by agent, prepended to the
user's prompt.
"""

AGENTS = ("claude", "codex", "kimi")
DEFAULT_AGENT = "claude"

# Bound on each past turn's prompt/response before interpolation, same
# rationale as graph.py's _MAX_MATCH_FIELD_CHARS: turns.response is an
# unbounded TEXT column, and the augmented prompt is passed to a CLI as a
# single argv element, which Linux caps at MAX_ARG_STRLEN (131072 bytes).
_MAX_TURN_FIELD_CHARS = 500


def normalize_agent(agent: str | None) -> str:
    """Maps an incoming agent name to a known one, falling back to the
    default. Client-supplied, so it must never reach argv unvalidated."""
    if agent in AGENTS:
        return agent
    return DEFAULT_AGENT


def _truncate(text: str, limit: int = _MAX_TURN_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head - 3
    return text[:head] + "..." + text[-tail:]


def build_handoff_prompt(
    prompt: str,
    *,
    incoming_agent: str,
    previous_agent: str,
    turns: list[dict],
) -> str:
    """Prefixes `prompt` with a brief telling `incoming_agent` what the other
    agent was doing. Returns `prompt` unchanged when there is nothing to hand
    over (no recorded turns), since an empty brief is only noise."""
    if not turns:
        return prompt

    history = "\n".join(
        f"{i + 1}. ({t.get('agent', 'unknown')}) Q: {_truncate(t['prompt'])}\n"
        f"   A: {_truncate(t['response'])}"
        for i, t in enumerate(turns)
    )
    return (
        f"[ANA 핸드오프] 직전까지 {previous_agent} 에이전트가 이 작업을 진행했다. "
        f"너({incoming_agent})가 이어서 진행해라.\n\n"
        f"최근 대화:\n{history}\n\n"
        f"현재 요청: {prompt}"
    )
