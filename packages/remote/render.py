"""Gate requests in, Discord message payloads out. Clicks in, answers out.

Pure functions, deliberately: this is the part of the Discord bridge where a
mistake is silent -- an option list one item over the limit, an answer whose
shape does not match what the web card produces -- and pure functions are the
part that can be tested without a bot token.

The answer shape is the contract that matters. `apps/web/src/components/ui/
question-card.tsx` produces one entry per question, a list when `multi` is set
and a string otherwise, so `collect_answers` produces exactly that and neither
/gate/ask nor `ask_user` has to learn that Discord exists.
"""
from typing import Any, Optional

# Discord's component types and button styles, spelled out rather than
# imported: there is no SDK here (see packages/remote/discord_client.py).
ACTION_ROW = 1
BUTTON = 2
SELECT = 3
TEXT_INPUT = 4

STYLE_PRIMARY = 1
STYLE_SECONDARY = 2
STYLE_SUCCESS = 3
STYLE_DANGER = 4

# The limits Discord enforces. Going over any of them is a 400 on send, which
# would look like "the bot is broken" rather than "that message was too long".
MAX_CUSTOM_ID = 100
MAX_ACTION_ROWS = 5
MAX_SELECT_OPTIONS = 25
MAX_SELECT_LABEL = 100
MAX_SELECT_DESCRIPTION = 100
MAX_PLACEHOLDER = 150
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD = 1024
MAX_EMBED_TITLE = 256
MAX_MODAL_ROWS = 5
MAX_LABEL = 45

COLOR_ASK = 0xE8A33D      # waiting on a person
COLOR_QUESTION = 0x5B8DEF
COLOR_DONE = 0x4B5563      # already decided: grey, so it reads as history

SEPARATOR = ":"
PREFIX = "ana"


def _cut(text: str, limit: int) -> str:
    """Truncates on a character budget, saying so when it cuts."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


# --- custom ids ---------------------------------------------------------------


def custom_id(kind: str, request_id: str, field: str = "") -> str:
    """`ana:<kind>:<request id>:<field>`.

    A UUID is 36 characters, which leaves room inside Discord's 100-character
    limit for every kind and field this module uses."""
    return _cut(SEPARATOR.join((PREFIX, kind, request_id, field)), MAX_CUSTOM_ID)


def parse_custom_id(value: str) -> Optional[tuple[str, str, str]]:
    """(kind, request_id, field), or None for anything that is not ours.

    A channel can hold other bots' buttons, and their clicks arrive on the same
    socket."""
    parts = (value or "").split(SEPARATOR)
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    return parts[1], parts[2], parts[3]


def text_key(index: int) -> str:
    """Where a modal's answer to question `index` is kept.

    Select state is keyed by the integer index and free text by this string, so
    one question can hold both and the text can win."""
    return f"text-{index}"


# --- approvals ----------------------------------------------------------------


def _command_of(tool_input: dict) -> str:
    """The one line worth reading out of a tool call's input."""
    for key in ("command", "file_path", "path", "url", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def approval_message(request: dict, *, workspace_name: str) -> dict:
    """The card the gate would have shown the browser, as a Discord message."""
    request_id = request.get("request_id") or ""
    tool_name = request.get("tool_name") or "tool"
    command = _command_of(request.get("tool_input") or {})
    description = request.get("description") or request.get("reason") or ""

    body = description
    if command:
        body = f"{body}\n\n```\n{_cut(command, 1500)}\n```" if body else f"```\n{_cut(command, 1500)}\n```"

    fields = [
        {"name": "워크스페이스", "value": _cut(workspace_name or "-", MAX_EMBED_FIELD), "inline": True},
        {"name": "에이전트", "value": _cut(request.get("agent") or "-", MAX_EMBED_FIELD), "inline": True},
    ]
    if request.get("rule_id"):
        fields.append(
            {"name": "규칙", "value": _cut(str(request["rule_id"]), MAX_EMBED_FIELD), "inline": True}
        )

    return {
        "embeds": [
            {
                "title": _cut(f"승인 필요 · {tool_name}", MAX_EMBED_TITLE),
                "description": _cut(body, MAX_EMBED_DESCRIPTION),
                "color": COLOR_ASK,
                "fields": fields,
            }
        ],
        "components": [
            {
                "type": ACTION_ROW,
                "components": [
                    {
                        "type": BUTTON,
                        "style": STYLE_SUCCESS,
                        "label": "허용",
                        "custom_id": custom_id("approve", request_id, "allow"),
                    },
                    {
                        "type": BUTTON,
                        "style": STYLE_DANGER,
                        "label": "거부",
                        "custom_id": custom_id("approve", request_id, "deny"),
                    },
                ],
            }
        ],
    }


# --- questionnaires -----------------------------------------------------------


def _option_label(option: Any) -> str:
    return option if isinstance(option, str) else (option.get("label") or option.get("value") or "")


def _option_value(option: Any) -> str:
    return option if isinstance(option, str) else (option.get("value") or option.get("label") or "")


def _option_description(option: Any) -> str:
    return "" if isinstance(option, str) else (option.get("description") or "")


def _options_of(question: dict) -> list:
    return list(question.get("options") or [])[:MAX_SELECT_OPTIONS]


def _has_input(question: dict) -> bool:
    field = question.get("input") or {}
    return bool(field.get("label"))


def _select_for(question: dict, index: int, request_id: str, picked: list[str]) -> dict:
    options = _options_of(question)
    required = question.get("required", True) is not False
    multi = bool(question.get("multi"))
    return {
        "type": SELECT,
        "custom_id": custom_id("pick", request_id, str(index)),
        "placeholder": _cut(question.get("question") or f"질문 {index + 1}", MAX_PLACEHOLDER),
        "min_values": 1 if required else 0,
        "max_values": len(options) if multi else 1,
        "options": [
            {
                "label": _cut(_option_label(option), MAX_SELECT_LABEL),
                "value": str(position),
                "description": _cut(_option_description(option), MAX_SELECT_DESCRIPTION),
                "default": str(position) in picked,
            }
            for position, option in enumerate(options)
        ],
    }


def question_message(request: dict, *, workspace_name: str, state: dict) -> dict:
    """One message for the whole questionnaire: a select per question, and a
    row of buttons to type free text or send.

    `ask_user` caps a request at four questions and Discord allows five action
    rows, so the select rows plus the button row always fit."""
    request_id = request.get("request_id") or ""
    questions = list(request.get("questions") or [])

    lines = []
    for index, question in enumerate(questions):
        lines.append(f"**{index + 1}. {question.get('question') or ''}**")
        if question.get("note"):
            lines.append(question["note"])
        answered = _answer_text(question, state, index)
        if answered:
            lines.append(f"→ {answered}")
    body = "\n".join(lines)

    rows: list[dict] = []
    for index, question in enumerate(questions):
        if not _options_of(question) or len(rows) >= MAX_ACTION_ROWS - 1:
            continue
        picked = [str(value) for value in state.get(index, [])]
        rows.append({"type": ACTION_ROW, "components": [_select_for(question, index, request_id, picked)]})

    buttons = []
    if any(_has_input(question) for question in questions):
        buttons.append(
            {
                "type": BUTTON,
                "style": STYLE_SECONDARY,
                "label": "직접 입력",
                "custom_id": custom_id("ask", request_id, "text"),
            }
        )
    buttons.append(
        {
            "type": BUTTON,
            "style": STYLE_PRIMARY,
            "label": "보내기",
            "custom_id": custom_id("ask", request_id, "submit"),
        }
    )
    rows.append({"type": ACTION_ROW, "components": buttons})

    return {
        "embeds": [
            {
                "title": _cut(f"질문 · {request.get('agent') or request.get('role') or 'agent'}", MAX_EMBED_TITLE),
                "description": _cut(body, MAX_EMBED_DESCRIPTION),
                "color": COLOR_QUESTION,
                "fields": [
                    {"name": "워크스페이스", "value": _cut(workspace_name or "-", MAX_EMBED_FIELD), "inline": True}
                ],
            }
        ],
        "components": rows,
    }


def modal_payload(request: dict) -> dict:
    """Every free-text question in one modal.

    A modal can only be opened as the direct response to a click and holds five
    inputs, which is more than `ask_user` can ask for, so one modal covers a
    whole request rather than one question at a time."""
    request_id = request.get("request_id") or ""
    questions = list(request.get("questions") or [])
    rows = []
    for index, question in enumerate(questions):
        if not _has_input(question) or len(rows) >= MAX_MODAL_ROWS:
            continue
        field = question.get("input") or {}
        rows.append(
            {
                "type": ACTION_ROW,
                "components": [
                    {
                        "type": TEXT_INPUT,
                        "style": 1,
                        "custom_id": custom_id("text", request_id, str(index)),
                        "label": _cut(field.get("label") or question.get("question") or "답", MAX_LABEL),
                        "placeholder": _cut(field.get("placeholder") or "", MAX_PLACEHOLDER),
                        "required": question.get("required", True) is not False,
                    }
                ],
            }
        )
    return {
        "custom_id": custom_id("modal", request_id, "submit"),
        "title": _cut("직접 입력", MAX_LABEL),
        "components": rows,
    }


def _answer_text(question: dict, state: dict, index: int) -> str:
    """One question's current answer, for showing in the message body."""
    typed = state.get(text_key(index)) or []
    if typed and str(typed[0]).strip():
        return str(typed[0])
    options = _options_of(question)
    picked = [str(value) for value in state.get(index, [])]
    labels = [
        _option_label(options[int(position)])
        for position in picked
        if position.isdigit() and int(position) < len(options)
    ]
    return ", ".join(labels)


def collect_answers(questions: list[dict], state: dict) -> list:
    """The answers, in the shape the browser sends them.

    One entry per question: a list when the question allows several picks, a
    string otherwise, and "" for an optional question nobody answered."""
    answers: list = []
    for index, question in enumerate(questions):
        typed = state.get(text_key(index)) or []
        options = _options_of(question)
        picked = [str(value) for value in state.get(index, [])]
        values = [
            _option_value(options[int(position)])
            for position in picked
            if position.isdigit() and int(position) < len(options)
        ]
        if typed and str(typed[0]).strip():
            # The web card puts the free-text field inside the same choice
            # group, where typing replaces the selection. Same here.
            values = [str(typed[0])]
        if question.get("multi"):
            answers.append(values)
        else:
            answers.append(values[0] if values else "")
    return answers


def missing_required(questions: list[dict], state: dict) -> list[int]:
    """Indices of required questions with nothing answered yet."""
    missing = []
    for index, question in enumerate(questions):
        if question.get("required", True) is False:
            continue
        typed = state.get(text_key(index)) or []
        if (typed and str(typed[0]).strip()) or state.get(index):
            continue
        missing.append(index)
    return missing


# --- closing ------------------------------------------------------------------

OUTCOME_LABEL = {
    "allow": "허용",
    "deny": "거부",
    "timeout": "시간 초과",
    "answered": "답변됨",
    "closed": "취소됨",
}


def _who(by: str) -> str:
    if not by or by == "user":
        return "웹 UI"
    if by.startswith("discord:"):
        return f"Discord <@{by.split(':', 1)[1]}>"
    return by


def closed_message(
    request: dict, *, workspace_name: str, outcome: str, by: str, kind: str = "approval"
) -> dict:
    """The same message with its buttons gone and a footer saying what became
    of it -- so the phone does not keep offering a decision that is made."""
    base = (
        approval_message(request, workspace_name=workspace_name)
        if kind == "approval"
        else question_message(request, workspace_name=workspace_name, state={})
    )
    embed = dict(base["embeds"][0])
    embed["color"] = COLOR_DONE
    label = OUTCOME_LABEL.get(outcome, outcome)
    embed["footer"] = {"text": _cut(f"{label} · {_who(by)}", MAX_EMBED_FIELD)}
    return {"embeds": [embed], "components": []}


def turn_message(workspace_name: str, text: str) -> dict:
    """What the agent finally said, so the person who approved something from a
    phone can see whether it worked."""
    return {
        "embeds": [
            {
                "title": _cut(f"턴 종료 · {workspace_name}", MAX_EMBED_TITLE),
                "description": _cut(text or "(빈 응답)", MAX_EMBED_DESCRIPTION),
                "color": COLOR_DONE,
            }
        ]
    }
