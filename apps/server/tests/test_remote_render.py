"""Turning a gate request into a Discord message, and a click back into an answer.

Everything here is pure: no socket, no bot token, no Discord. The mapping is
where this feature can quietly go wrong -- an option list longer than Discord
allows, an answer whose shape does not match what the browser produces -- so it
is tested on its own.
"""
import pytest

from packages.remote import render


def _approval(**overrides) -> dict:
    request = {
        "request_id": "11111111-2222-3333-4444-555555555555",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"},
        "rule_id": "destructive-shell",
        "category": "shell",
        "description": "되돌릴 수 없는 삭제 명령이다",
        "agent": "claude",
        "cwd": "/home/hello/workspace/demo",
        "workspace": "/home/hello/workspace/demo",
    }
    request.update(overrides)
    return request


def _questions(**overrides) -> dict:
    request = {
        "request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "agent": "codex",
        "role": "lead",
        "questions": [
            {
                "question": "어떤 DB를 쓸까?",
                "options": [
                    {"value": "postgres", "label": "Postgres", "description": "기본값"},
                    {"value": "sqlite", "label": "SQLite", "description": "가볍다"},
                ],
                "required": True,
                "multi": False,
                "note": "나중에 바꾸기 어렵다",
            },
        ],
    }
    request.update(overrides)
    return request


# --- custom ids --------------------------------------------------------------


def test_custom_id_round_trips():
    packed = render.custom_id("approve", "11111111-2222-3333-4444-555555555555", "allow")

    assert len(packed) <= render.MAX_CUSTOM_ID
    assert render.parse_custom_id(packed) == (
        "approve", "11111111-2222-3333-4444-555555555555", "allow",
    )


def test_custom_id_of_another_app_is_not_ours():
    assert render.parse_custom_id("someone-elses-button") is None
    assert render.parse_custom_id("") is None


# --- approvals ---------------------------------------------------------------


def test_approval_message_carries_the_reason_the_rule_gave():
    message = render.approval_message(_approval(), workspace_name="demo")
    embed = message["embeds"][0]

    assert "되돌릴 수 없는 삭제 명령이다" in embed["description"]
    # The command itself, because "may I run this" is unanswerable without it.
    assert "rm -rf build" in embed["description"]
    assert any(field["value"] == "demo" for field in embed["fields"])
    assert any(field["value"] == "claude" for field in embed["fields"])


def test_approval_message_has_one_allow_and_one_deny_button():
    message = render.approval_message(_approval(), workspace_name="demo")
    buttons = message["components"][0]["components"]

    assert [button["label"] for button in buttons] == ["허용", "거부"]
    assert [render.parse_custom_id(b["custom_id"])[2] for b in buttons] == ["allow", "deny"]
    # Denial is the safe button, so it is the one styled as dangerous.
    assert buttons[0]["style"] == render.STYLE_SUCCESS
    assert buttons[1]["style"] == render.STYLE_DANGER


def test_a_huge_tool_input_is_truncated_rather_than_rejected():
    message = render.approval_message(
        _approval(tool_input={"command": "echo " + "x" * 9000}), workspace_name="demo"
    )

    assert len(message["embeds"][0]["description"]) <= render.MAX_EMBED_DESCRIPTION


# --- questionnaires ----------------------------------------------------------


def test_each_question_becomes_its_own_select():
    request = _questions(questions=[
        {"question": "첫 번째?", "options": ["a", "b"], "required": True},
        {"question": "두 번째?", "options": ["c", "d"], "required": True},
    ])

    message = render.question_message(request, workspace_name="demo", state={})
    rows = message["components"]

    selects = [row["components"][0] for row in rows if row["components"][0]["type"] == render.SELECT]
    assert len(selects) == 2
    assert [option["label"] for option in selects[0]["options"]] == ["a", "b"]
    # Values are positional: an ask_user option value may be any length, and
    # Discord caps a select value at 100 characters.
    assert [option["value"] for option in selects[0]["options"]] == ["0", "1"]


def test_a_multi_select_question_allows_several_picks():
    request = _questions(questions=[
        {"question": "무엇을 켤까?", "options": ["a", "b", "c"], "multi": True, "required": True},
    ])

    select = render.question_message(request, workspace_name="demo", state={})["components"][0][
        "components"
    ][0]

    assert select["max_values"] == 3
    assert select["min_values"] == 1


def test_an_optional_question_may_be_left_empty():
    request = _questions(questions=[
        {"question": "덧붙일 말?", "options": ["a", "b"], "required": False},
    ])

    select = render.question_message(request, workspace_name="demo", state={})["components"][0][
        "components"
    ][0]

    assert select["min_values"] == 0


def test_a_free_text_question_gets_a_button_that_opens_the_modal():
    request = _questions(questions=[
        {"question": "배포 URL?", "input": {"label": "URL"}, "required": True},
    ])

    message = render.question_message(request, workspace_name="demo", state={})
    labels = [
        component["label"]
        for row in message["components"]
        for component in row["components"]
        if component["type"] == render.BUTTON
    ]

    assert "직접 입력" in labels
    assert "보내기" in labels


def test_no_free_text_question_means_no_input_button():
    message = render.question_message(_questions(), workspace_name="demo", state={})
    labels = [
        component["label"]
        for row in message["components"]
        for component in row["components"]
        if component["type"] == render.BUTTON
    ]

    assert "직접 입력" not in labels


def test_four_questions_still_fit_discords_five_action_rows():
    request = _questions(questions=[
        {"question": f"질문 {i}", "options": ["a", "b"], "required": True} for i in range(4)
    ])

    message = render.question_message(request, workspace_name="demo", state={})

    assert len(message["components"]) <= render.MAX_ACTION_ROWS


def test_too_many_options_are_capped_at_what_discord_accepts():
    request = _questions(questions=[
        {"question": "하나 골라", "options": [f"option-{i}" for i in range(40)], "required": True},
    ])

    select = render.question_message(request, workspace_name="demo", state={})["components"][0][
        "components"
    ][0]

    assert len(select["options"]) == render.MAX_SELECT_OPTIONS


def test_what_is_already_picked_comes_back_marked_as_default():
    request = _questions(questions=[
        {"question": "첫 번째?", "options": ["a", "b"], "required": True},
    ])

    select = render.question_message(request, workspace_name="demo", state={0: ["1"]})[
        "components"
    ][0]["components"][0]

    assert [option["default"] for option in select["options"]] == [False, True]


# --- modals ------------------------------------------------------------------


def test_the_modal_holds_every_free_text_question_at_once():
    request = _questions(questions=[
        {"question": "이름?", "input": {"label": "이름", "placeholder": "ana"}, "required": True},
        {"question": "고르기", "options": ["a", "b"], "required": True},
        {"question": "메모?", "input": {"label": "메모"}, "required": False},
    ])

    modal = render.modal_payload(request)
    rows = modal["components"]

    assert len(rows) == 2
    assert [render.parse_custom_id(row["components"][0]["custom_id"])[2] for row in rows] == ["0", "2"]
    assert rows[0]["components"][0]["placeholder"] == "ana"
    assert rows[0]["components"][0]["required"] is True
    assert rows[1]["components"][0]["required"] is False


# --- answers -----------------------------------------------------------------


def test_answers_match_the_shape_the_browser_sends():
    request = _questions(questions=[
        {"question": "하나", "options": ["a", "b"], "required": True},
        {"question": "여럿", "options": ["c", "d", "e"], "multi": True, "required": True},
        {"question": "직접", "input": {"label": "값"}, "required": True},
    ])
    state = {0: ["1"], 1: ["0", "2"], render.text_key(2): ["손으로 쓴 답"]}

    assert render.collect_answers(request["questions"], state) == [
        "b", ["c", "e"], "손으로 쓴 답",
    ]


def test_an_unanswered_optional_question_is_empty_not_missing():
    request = _questions(questions=[
        {"question": "하나", "options": ["a", "b"], "required": True},
        {"question": "선택", "options": ["c", "d"], "required": False},
    ])

    assert render.collect_answers(request["questions"], {0: ["0"]}) == ["a", ""]


def test_free_text_wins_over_a_selection_for_the_same_question():
    """The web card puts the input inside the same choice group: typing an
    answer replaces whatever was picked."""
    request = _questions(questions=[
        {"question": "하나", "options": ["a", "b"], "input": {"label": "기타"}, "required": True},
    ])

    answers = render.collect_answers(request["questions"], {0: ["0"], render.text_key(0): ["기타 답"]})

    assert answers == ["기타 답"]


def test_missing_required_answers_are_named_before_submitting():
    request = _questions(questions=[
        {"question": "필수", "options": ["a", "b"], "required": True},
        {"question": "선택", "options": ["c"], "required": False},
        {"question": "필수2", "options": ["d"], "required": True},
    ])

    assert render.missing_required(request["questions"], {2: ["0"]}) == [0]


def test_nothing_is_missing_once_everything_required_is_answered():
    request = _questions()

    assert render.missing_required(request["questions"], {0: ["0"]}) == []


# --- closing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected",
    [("allow", "허용"), ("deny", "거부"), ("timeout", "시간 초과"), ("answered", "답변됨")],
)
def test_a_closed_request_says_what_happened_and_loses_its_buttons(outcome, expected):
    closed = render.closed_message(
        _approval(), workspace_name="demo", outcome=outcome, by="discord:42"
    )

    assert closed["components"] == []
    assert expected in closed["embeds"][0]["footer"]["text"]


def test_a_closed_request_names_who_answered_it():
    closed = render.closed_message(
        _approval(), workspace_name="demo", outcome="allow", by="user"
    )

    assert "웹" in closed["embeds"][0]["footer"]["text"]


def test_turn_summaries_are_truncated_to_what_discord_accepts():
    message = render.turn_message("demo", "x" * 9000)

    assert len(message["embeds"][0]["description"]) <= render.MAX_EMBED_DESCRIPTION
    assert "demo" in message["embeds"][0]["title"]
