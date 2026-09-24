"""The gateway protocol, without a socket.

The connection loop is thin on purpose; what is worth pinning down is the
framing -- what ANA sends to identify and resume -- and what it does with each
frame that comes back.
"""
import pytest

from packages.remote import discord_client as client


def _gateway(seen):
    async def on_interaction(data):
        seen.append(data)

    return client.DiscordGateway("bot-token", on_interaction)


def test_identify_asks_for_no_intents():
    payload = client.identify_payload("bot-token")

    assert payload["op"] == client.OP_IDENTIFY
    # Interactions arrive regardless of intents, and ANA reads nothing else in
    # the channel, so it asks for nothing.
    assert payload["d"]["intents"] == 0
    assert payload["d"]["token"] == "bot-token"


def test_a_resume_carries_the_session_and_how_far_it_got():
    payload = client.resume_payload("bot-token", "sess-1", 42)

    assert payload["op"] == client.OP_RESUME
    assert payload["d"] == {"token": "bot-token", "session_id": "sess-1", "seq": 42}


@pytest.mark.asyncio
async def test_an_interaction_reaches_the_bridge():
    seen = []
    gateway = _gateway(seen)

    await gateway.handle_frame(
        {"op": client.OP_DISPATCH, "s": 1, "t": "INTERACTION_CREATE", "d": {"id": "i-1"}}
    )

    assert seen == [{"id": "i-1"}]


@pytest.mark.asyncio
async def test_other_events_are_ignored():
    seen = []
    gateway = _gateway(seen)

    await gateway.handle_frame(
        {"op": client.OP_DISPATCH, "s": 2, "t": "MESSAGE_CREATE", "d": {"content": "hi"}}
    )

    assert seen == []


@pytest.mark.asyncio
async def test_the_sequence_is_tracked_so_a_resume_picks_up_where_it_left_off():
    gateway = _gateway([])

    await gateway.handle_frame({"op": client.OP_DISPATCH, "s": 7, "t": "MESSAGE_CREATE", "d": {}})

    assert gateway._sequence == 7


@pytest.mark.asyncio
async def test_ready_remembers_what_a_resume_needs():
    gateway = _gateway([])

    await gateway.handle_frame(
        {
            "op": client.OP_DISPATCH,
            "s": 1,
            "t": "READY",
            "d": {"session_id": "sess-1", "resume_gateway_url": "wss://resume.example"},
        }
    )

    assert gateway._session_id == "sess-1"
    assert gateway._resume_url.startswith("wss://resume.example")


@pytest.mark.asyncio
async def test_a_reconnect_request_drops_the_socket():
    gateway = _gateway([])

    with pytest.raises(ConnectionError):
        await gateway.handle_frame({"op": client.OP_RECONNECT})


@pytest.mark.asyncio
async def test_an_invalidated_session_is_not_resumed():
    gateway = _gateway([])
    await gateway.handle_frame(
        {"op": client.OP_DISPATCH, "s": 1, "t": "READY",
         "d": {"session_id": "sess-1", "resume_gateway_url": "wss://resume.example"}}
    )

    with pytest.raises(ConnectionError):
        await gateway.handle_frame({"op": client.OP_INVALID_SESSION, "d": False})

    # The next connection starts fresh rather than resuming into a session
    # Discord has already thrown away.
    assert gateway._session_id == ""
    assert gateway._resume_url == ""
