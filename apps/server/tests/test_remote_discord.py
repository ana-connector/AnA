"""The Discord bridge: who may answer, what a click does, and what it must not do.

The gateway socket is not exercised here -- the API calls it would make are
recorded instead. What is tested is everything that decides whether a button
press becomes an approval.
"""
import asyncio

import pytest

from packages.governance.session import GateRegistry
from packages.remote import render
from packages.remote.discord_bridge import DiscordBridge, DiscordConfig

CHANNEL = "555000111"
ALLOWED = "42"
STRANGER = "99"


class FakeAPI:
    """Records what would have gone to Discord."""

    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.edited: list[tuple[str, str, dict]] = []
        self.callbacks: list[dict] = []
        self.next_message_id = 1000

    async def create_message(self, channel_id: str, payload: dict) -> str:
        self.created.append((channel_id, payload))
        self.next_message_id += 1
        return str(self.next_message_id)

    async def edit_message(self, channel_id: str, message_id: str, payload: dict) -> None:
        self.edited.append((channel_id, message_id, payload))

    async def respond(self, interaction_id: str, token: str, payload: dict) -> None:
        self.callbacks.append(payload)


def _bridge(registry, *, turn_summaries: bool = True):
    api = FakeAPI()
    config = DiscordConfig(
        token="x",
        channel_id=CHANNEL,
        allowed_user_ids=frozenset({ALLOWED}),
        turn_summaries=turn_summaries,
    )
    return DiscordBridge(config=config, api=api, registry=registry), api


def _click(custom_id: str, *, user: str = ALLOWED, channel: str = CHANNEL, values=None) -> dict:
    data = {"custom_id": custom_id, "component_type": 3 if values is not None else 2}
    if values is not None:
        data["values"] = values
    return {
        "id": "int-1",
        "token": "tok",
        "type": 3,
        "channel_id": channel,
        "member": {"user": {"id": user}},
        "data": data,
        "message": {"id": "1001"},
    }


async def _pending_approval(registry, bridge):
    """A real approval waiting on a real future, posted through the bridge."""
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    session.workspace_name = "demo"
    task = asyncio.create_task(
        registry.request_approval(session, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    )
    for _ in range(50):
        if bridge.posted_requests():
            break
        await asyncio.sleep(0.01)
    return session, task, sent[0]["request_id"]


async def _pending_question(registry, bridge, questions):
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    session.workspace_name = "demo"
    task = asyncio.create_task(registry.request_answer(session, {"questions": questions}))
    for _ in range(50):
        if bridge.posted_requests():
            break
        await asyncio.sleep(0.01)
    return session, task, sent[0]["request_id"]


# --- posting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approval_is_posted_to_the_configured_channel():
    registry = GateRegistry(approval_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    _, task, request_id = await _pending_approval(registry, bridge)

    channel, payload = api.created[0]
    assert channel == CHANNEL
    assert payload["components"][0]["components"][0]["label"] == "허용"

    registry.resolve_remote_approval(request_id, "deny", f"discord:{ALLOWED}")
    await task


# --- who may answer -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_allowlisted_click_answers_the_gate():
    registry = GateRegistry(approval_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    _, task, request_id = await _pending_approval(registry, bridge)
    await bridge.handle_interaction(_click(render.custom_id("approve", request_id, "allow")))

    answer = await task
    assert answer.value == "allow"
    assert answer.by == f"discord:{ALLOWED}"


@pytest.mark.asyncio
async def test_a_stranger_cannot_approve_anything():
    registry = GateRegistry(approval_timeout=0.3)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    _, task, request_id = await _pending_approval(registry, bridge)
    await bridge.handle_interaction(
        _click(render.custom_id("approve", request_id, "allow"), user=STRANGER)
    )

    # Refused to their face, and the future is untouched -- it times out as if
    # nobody had clicked at all.
    assert api.callbacks[0]["data"]["flags"] == DiscordBridge.EPHEMERAL
    assert (await task).value == "timeout"


@pytest.mark.asyncio
async def test_a_click_from_another_channel_is_ignored():
    registry = GateRegistry(approval_timeout=0.3)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    _, task, request_id = await _pending_approval(registry, bridge)
    await bridge.handle_interaction(
        _click(render.custom_id("approve", request_id, "allow"), channel="different")
    )

    assert api.callbacks == []
    assert (await task).value == "timeout"


@pytest.mark.asyncio
async def test_another_bots_button_is_not_ours():
    registry = GateRegistry(approval_timeout=5)
    bridge, api = _bridge(registry)

    await bridge.handle_interaction(_click("some-other-app:button"))

    assert api.callbacks == []


@pytest.mark.asyncio
async def test_a_decision_that_is_already_made_says_so_instead_of_deciding_twice():
    registry = GateRegistry(approval_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    _, task, request_id = await _pending_approval(registry, bridge)
    await bridge.handle_interaction(_click(render.custom_id("approve", request_id, "allow")))
    await task
    api.callbacks.clear()

    await bridge.handle_interaction(_click(render.custom_id("approve", request_id, "deny")))

    assert api.callbacks[0]["data"]["flags"] == DiscordBridge.EPHEMERAL


# --- questionnaires -----------------------------------------------------------


@pytest.mark.asyncio
async def test_picking_an_option_is_remembered_and_shown_back():
    registry = GateRegistry(question_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    questions = [{"question": "어느 쪽?", "options": ["왼쪽", "오른쪽"], "required": True}]
    _, task, request_id = await _pending_question(registry, bridge, questions)

    await bridge.handle_interaction(
        _click(render.custom_id("pick", request_id, "0"), values=["1"])
    )

    # The message is rewritten so the phone shows what is picked so far.
    update = api.callbacks[0]
    assert update["type"] == DiscordBridge.UPDATE_MESSAGE
    select = update["data"]["components"][0]["components"][0]
    assert [option["default"] for option in select["options"]] == [False, True]

    await bridge.handle_interaction(_click(render.custom_id("ask", request_id, "submit")))
    assert (await task).value == ["오른쪽"]


@pytest.mark.asyncio
async def test_submitting_with_a_required_question_unanswered_is_refused():
    registry = GateRegistry(question_timeout=0.3)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    questions = [
        {"question": "필수", "options": ["a", "b"], "required": True},
        {"question": "선택", "options": ["c"], "required": False},
    ]
    _, task, request_id = await _pending_question(registry, bridge, questions)

    await bridge.handle_interaction(_click(render.custom_id("ask", request_id, "submit")))

    assert api.callbacks[0]["data"]["flags"] == DiscordBridge.EPHEMERAL
    assert "필수" in api.callbacks[0]["data"]["content"]
    assert (await task).value is None


@pytest.mark.asyncio
async def test_the_text_button_opens_a_modal_and_its_answer_is_kept():
    registry = GateRegistry(question_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    questions = [{"question": "배포 URL?", "input": {"label": "URL"}, "required": True}]
    _, task, request_id = await _pending_question(registry, bridge, questions)

    await bridge.handle_interaction(_click(render.custom_id("ask", request_id, "text")))
    assert api.callbacks[0]["type"] == DiscordBridge.MODAL

    await bridge.handle_interaction(
        {
            "id": "int-2",
            "token": "tok",
            "type": DiscordBridge.MODAL_SUBMIT,
            "channel_id": CHANNEL,
            "member": {"user": {"id": ALLOWED}},
            "data": {
                "custom_id": render.custom_id("modal", request_id, "submit"),
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": render.custom_id("text", request_id, "0"),
                                "value": "https://example.test",
                            }
                        ],
                    }
                ],
            },
            "message": {"id": "1001"},
        }
    )
    await bridge.handle_interaction(_click(render.custom_id("ask", request_id, "submit")))

    assert (await task).value == ["https://example.test"]


# --- closing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answered_request_loses_its_buttons_on_discord_too():
    registry = GateRegistry(approval_timeout=5)
    bridge, api = _bridge(registry)
    registry.notifier = bridge

    session, task, request_id = await _pending_approval(registry, bridge)
    registry.resolve_approval(session, request_id, "allow", by="user")
    await task

    _, _, payload = api.edited[0]
    assert payload["components"] == []
    assert "허용" in payload["embeds"][0]["footer"]["text"]
    # Nothing is left holding the request's selections.
    assert bridge.posted_requests() == []


# --- turn summaries -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_finished_turn_is_reported():
    registry = GateRegistry()
    bridge, api = _bridge(registry)
    session = registry.register("t", lambda _: asyncio.sleep(0))
    session.workspace_name = "demo"

    await bridge.turn_ended(session, "다 했다")

    assert "다 했다" in api.created[0][1]["embeds"][0]["description"]


@pytest.mark.asyncio
async def test_turn_summaries_can_be_switched_off():
    registry = GateRegistry()
    bridge, api = _bridge(registry, turn_summaries=False)
    session = registry.register("t", lambda _: asyncio.sleep(0))

    await bridge.turn_ended(session, "다 했다")

    assert api.created == []


# --- configuration ------------------------------------------------------------


def test_no_token_means_no_bridge():
    assert DiscordConfig.from_env({}) is None
    assert DiscordConfig.from_env({"ANA_DISCORD_BOT_TOKEN": "t"}) is None


def test_an_empty_allowlist_means_nobody_rather_than_everybody():
    config = DiscordConfig.from_env(
        {"ANA_DISCORD_BOT_TOKEN": "t", "ANA_DISCORD_CHANNEL_ID": CHANNEL}
    )

    assert config is not None
    assert config.allowed_user_ids == frozenset()
    assert config.may_answer(ALLOWED) is False


def test_the_singular_spelling_of_the_allowlist_works_too():
    """One person answering their own agent writes "…_USER_ID"."""
    config = DiscordConfig.from_env(
        {
            "ANA_DISCORD_BOT_TOKEN": "t",
            "ANA_DISCORD_CHANNEL_ID": CHANNEL,
            "ANA_DISCORD_ALLOWED_USER_ID": ALLOWED,
        }
    )

    assert config.may_answer(ALLOWED)
    assert config.may_answer(STRANGER) is False


def test_the_allowlist_is_read_as_a_comma_separated_list():
    config = DiscordConfig.from_env(
        {
            "ANA_DISCORD_BOT_TOKEN": "t",
            "ANA_DISCORD_CHANNEL_ID": CHANNEL,
            "ANA_DISCORD_ALLOWED_USER_IDS": " 42, 43 ",
        }
    )

    assert config.may_answer("42") and config.may_answer("43")
    assert config.may_answer(STRANGER) is False
