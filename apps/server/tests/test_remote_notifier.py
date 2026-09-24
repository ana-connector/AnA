"""The second door to a pending gate request.

The browser is no longer the only thing that can answer, so the registry has to
fan a request out to whatever else is listening, let that thing answer by
request id alone, and close the request on both sides whatever ended it.
"""
import asyncio

import pytest

from packages.governance.session import GateRegistry


class RecordingNotifier:
    """Stands in for the Discord bot: records what it was told, and can be made
    to fail the way a network does."""

    def __init__(self, fail: bool = False):
        self.posted: list[tuple[str, str, dict]] = []
        self.closed: list[tuple[str, str, str]] = []
        self.turns: list[str] = []
        self.fail = fail

    async def approval_posted(self, request_id, session, request):
        if self.fail:
            raise ConnectionError("discord is down")
        self.posted.append(("approval", request_id, request))

    async def question_posted(self, request_id, session, request):
        if self.fail:
            raise ConnectionError("discord is down")
        self.posted.append(("question", request_id, request))

    async def request_closed(self, request_id, session, outcome, by):
        if self.fail:
            raise ConnectionError("discord is down")
        self.closed.append((request_id, outcome, by))

    async def turn_ended(self, session, text):
        self.turns.append(text)


def _registry(notifier, timeout: float = 5.0) -> GateRegistry:
    return GateRegistry(approval_timeout=timeout, question_timeout=timeout, notifier=notifier)


async def _sent_request_id(sent: list) -> str:
    for _ in range(50):
        if sent:
            return sent[0]["request_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was sent to the browser")


@pytest.mark.asyncio
async def test_an_approval_is_posted_to_the_notifier_as_well_as_the_browser():
    notifier = RecordingNotifier()
    registry = _registry(notifier)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {"tool_name": "Bash"}))
    request_id = await _sent_request_id(sent)
    await asyncio.sleep(0)

    kind, posted_id, request = notifier.posted[0]
    assert (kind, posted_id) == ("approval", request_id)
    assert request["tool_name"] == "Bash"

    registry.resolve_approval(session, request_id, "allow")
    await task


@pytest.mark.asyncio
async def test_the_notifier_is_told_how_the_request_ended():
    notifier = RecordingNotifier()
    registry = _registry(notifier)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    request_id = await _sent_request_id(sent)
    registry.resolve_approval(session, request_id, "allow")
    await task

    assert notifier.closed == [(request_id, "allow", "user")]


@pytest.mark.asyncio
async def test_a_request_nobody_answered_is_closed_as_a_timeout():
    notifier = RecordingNotifier()
    registry = _registry(notifier, timeout=0.15)

    async def send(_):
        return None

    session = registry.register("t", send)
    await registry.request_approval(session, {})

    assert [outcome for _, outcome, _ in notifier.closed] == ["timeout"]


@pytest.mark.asyncio
async def test_a_closed_connection_closes_the_remote_card_too():
    notifier = RecordingNotifier()
    registry = _registry(notifier, timeout=30)
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    await _sent_request_id(sent)
    registry.unregister(session.token)
    await task

    assert [outcome for _, outcome, _ in notifier.closed] == ["closed"]


@pytest.mark.asyncio
async def test_discord_being_down_never_turns_an_approval_into_a_denial():
    registry = _registry(RecordingNotifier(fail=True))
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    request_id = await _sent_request_id(sent)
    registry.resolve_approval(session, request_id, "allow")

    assert (await task).value == "allow"


# --- answering from somewhere other than the browser --------------------------


@pytest.mark.asyncio
async def test_an_approval_can_be_answered_by_request_id_alone():
    """A Discord interaction carries no gate token -- only what was packed into
    the button's custom id."""
    registry = _registry(RecordingNotifier())
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    request_id = await _sent_request_id(sent)

    assert registry.resolve_remote_approval(request_id, "allow", "discord:42") is True

    answer = await task
    assert answer.value == "allow"
    assert answer.by == "discord:42"


@pytest.mark.asyncio
async def test_a_question_can_be_answered_by_request_id_alone():
    registry = _registry(RecordingNotifier())
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(
        registry.request_answer(session, {"questions": [{"question": "어느 쪽?"}]})
    )
    request_id = await _sent_request_id(sent)

    assert registry.resolve_remote_answer(request_id, ["왼쪽"], "discord:42") is True

    answer = await task
    assert answer.value == ["왼쪽"]
    assert answer.by == "discord:42"


@pytest.mark.asyncio
async def test_an_unknown_or_finished_request_id_resolves_nothing():
    registry = _registry(RecordingNotifier())
    sent = []

    async def send(payload):
        sent.append(payload)

    session = registry.register("t", send)
    task = asyncio.create_task(registry.request_approval(session, {}))
    request_id = await _sent_request_id(sent)

    assert registry.resolve_remote_approval("not-a-request", "allow", "discord:42") is False
    assert registry.resolve_remote_approval(request_id, "allow", "discord:42") is True
    await task
    # The phone is looking at a card for a decision that is already made.
    assert registry.resolve_remote_approval(request_id, "deny", "discord:42") is False


@pytest.mark.asyncio
async def test_answering_from_discord_takes_the_card_off_the_browser():
    registry = _registry(RecordingNotifier())
    sent = []
    resolved = []

    async def send(payload):
        sent.append(payload)

    async def on_resolved(request_id, by):
        resolved.append((request_id, by))

    session = registry.register("t", send)
    session.on_resolved = on_resolved
    task = asyncio.create_task(registry.request_approval(session, {}))
    request_id = await _sent_request_id(sent)
    registry.resolve_remote_approval(request_id, "deny", "discord:42")
    await task

    assert resolved == [(request_id, "discord:42")]
