"""Context handoff between the agent CLIs.

Each CLI keeps its own native session (claude --resume / codex exec resume /
kimi --session / agent --resume / opencode run --session),
so switching agents mid-thread means the incoming CLI knows nothing about the
turns the other one just ran. This module builds the brief that closes that
gap: the thread's own recent turns, attributed by agent, prepended to the
user's prompt.
"""

# "tapie" is the Claude Code CLI again, authenticated with a key against
# TAPIE's endpoint rather than the Pro subscription. It is its own agent
# rather than a mode on `claude` because the two bill against different
# accounts, which is the only thing routing cares about.
# "cursor" is the Cursor Agent CLI (`agent`), authenticated from the host
# login session in ~/.config/cursor/auth.json.
# "opencode" is the OpenCode CLI, which is one CLI in front of many providers:
# its model names are `provider/model` and there are dozens of them, so the
# ladder's model field matters more here than anywhere else.
AGENTS = ("claude", "codex", "kimi", "tapie", "cursor", "opencode")
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
    # English, like every other prompt ANA writes for a model to read: the brief
    # is machine-to-machine and the same words cost fewer tokens this way. The
    # turns quoted inside it stay in whatever language they happened in.
    return (
        f"[ANA HANDOFF] The {previous_agent} agent was working on this until now. "
        f"You ({incoming_agent}) continue from here.\n\n"
        f"Recent turns:\n{history}\n\n"
        f"Current request: {prompt}"
    )
